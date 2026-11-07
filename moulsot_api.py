"""Local MoulSot API for a single NVIDIA GPU.

Run: python -m uvicorn moulsot_api:app --host 127.0.0.1 --port 8001 --workers 1
Requirements: torch (CUDA), qwen-asr==0.0.6, fastapi, uvicorn,
python-multipart, numpy and the ffmpeg executable.

This service returns raw ASR text; it does not correct medication names.
Keep it on localhost. Public/cloud exposure requires authentication separately.
"""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile

MODEL_ID = "atlasia/moulsot.v0.3"
SAMPLE_RATE = 16000
MAX_SECONDS = 30
MAX_BYTES = 20 * 1024 * 1024
log = logging.getLogger("moulsot")


def decode_audio(payload: bytes) -> np.ndarray:
    """Decode at most 31 seconds; reject long input instead of truncating it."""
    with tempfile.TemporaryDirectory(prefix="moulsot_") as directory:
        source = Path(directory) / "upload.bin"
        source.write_bytes(payload)
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-protocol_whitelist", "file,pipe", "-i", str(source),
                    "-map", "0:a:0", "-vn", "-t", str(MAX_SECONDS + 1),
                    "-ac", "1", "-ar", str(SAMPLE_RATE),
                    "-f", "f32le", "pipe:1",
                ],
                capture_output=True, check=True, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(422, "Décodage trop long. Essaie un autre fichier.") from exc
        except subprocess.CalledProcessError as exc:
            raise HTTPException(422, "Fichier audio illisible ou sans piste audio.") from exc

    samples = np.frombuffer(result.stdout, dtype="<f4").copy()
    if samples.size == 0 or not np.isfinite(samples).all():
        raise HTTPException(422, "Audio vide ou invalide.")
    if samples.size > MAX_SECONDS * SAMPLE_RATE:
        raise HTTPException(422, f"Vocal trop long : maximum {MAX_SECONDS} secondes.")
    return samples


@asynccontextmanager
async def lifespan(app: FastAPI):
    import torch
    from qwen_asr import Qwen3ASRModel

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg absent. Sous Ubuntu : sudo apt install ffmpeg")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA indisponible. Vérifie le pilote NVIDIA et PyTorch CUDA.")

    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    log.info("Chargement de %s sur %s", MODEL_ID, torch.cuda.get_device_name(0))
    app.state.model = Qwen3ASRModel.from_pretrained(
        MODEL_ID,
        dtype=dtype,
        device_map="cuda:0",
        max_inference_batch_size=1,
        max_new_tokens=512,
    )
    app.state.gpu_lock = threading.Lock()
    app.state.torch = torch
    app.state.device = torch.cuda.get_device_name(0)
    try:
        yield
    finally:
        app.state.model = None
        torch.cuda.empty_cache()


app = FastAPI(title="MoulSot local", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ready", "model": MODEL_ID,
        "device": app.state.device, "max_audio_seconds": MAX_SECONDS,
    }


@app.post("/transcribe")
def transcribe(file: UploadFile = File(...)):
    # A sync route uses FastAPI's thread pool; health checks remain responsive.
    if not app.state.gpu_lock.acquire(blocking=False):
        file.file.close()
        raise HTTPException(503, "Transcription déjà en cours.", headers={"Retry-After": "2"})
    started = time.perf_counter()
    try:
        payload = file.file.read(MAX_BYTES + 1)
        if not payload:
            raise HTTPException(422, "Fichier vide.")
        if len(payload) > MAX_BYTES:
            raise HTTPException(413, "Fichier trop volumineux : maximum 20 Mo.")

        samples = decode_audio(payload)
        torch = app.state.torch
        try:
            with torch.inference_mode():
                results = app.state.model.transcribe(
                    audio=(samples, SAMPLE_RATE),
                    language=None,
                )
        except torch.cuda.OutOfMemoryError as exc:
            torch.cuda.empty_cache()
            raise HTTPException(
                503, "VRAM insuffisante. Décharge les autres modèles et réessaie."
            ) from exc

        if not results or not results[0].text.strip():
            raise HTTPException(422, "Aucune transcription. Réenregistre le message.")

        return {
            "text": results[0].text.strip(),
            "language": results[0].language,
            "model": MODEL_ID,
            "audio_seconds": round(samples.size / SAMPLE_RATE, 2),
            "processing_seconds": round(time.perf_counter() - started, 2),
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Échec de transcription")
        raise HTTPException(500, "Échec de transcription. Consulte le terminal du service.") from exc
    finally:
        file.file.close()
        app.state.gpu_lock.release()
