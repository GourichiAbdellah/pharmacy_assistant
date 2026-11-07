"""Azure Vision Read OCR. An OCR score never confirms a medication identity."""
import asyncio
from io import BytesIO
import math
import warnings

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_OCR_CHARS = 8000
WORD_REVIEW_THRESHOLD = 0.80  # Prototype heuristic, to calibrate on real box photos.


class VisionError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def prepare_image(payload):
    """Validate actual bytes, apply phone orientation, discard EXIF, bound memory."""
    if not payload:
        raise VisionError(422, "La photo est vide. Choisis une photo de la boîte.")
    if len(payload) > MAX_IMAGE_BYTES:
        raise VisionError(413, "La photo dépasse 10 Mo. Envoie une photo JPG ou PNG plus légère.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as original:
                if original.format not in {"JPEG", "PNG"} or getattr(original, "n_frames", 1) != 1:
                    raise VisionError(415, "Envoie une photo JPG ou PNG fixe de la boîte.")
                width, height = original.size
                if min(width, height) < 50 or max(width, height) > 10000 or width * height > MAX_PIXELS:
                    raise VisionError(422, "Dimensions non prises en charge. Envoie une photo entre 50 et 10 000 pixels par côté et de 20 mégapixels maximum.")
                original.load()
                oriented = ImageOps.exif_transpose(original)
                # White background for transparency; only pixel data enters the new image.
                rgba = oriented.convert("RGBA")
                clean = Image.new("RGB", rgba.size, "white")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                clean.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
                output = BytesIO()
                clean.save(output, format="JPEG", quality=92)
                encoded = output.getvalue()
                if len(encoded) > 4 * 1024 * 1024:
                    raise VisionError(413, "La photo reste trop volumineuse. Cadre une seule boîte et reprends la photo.")
                return encoded, {"width": clean.width, "height": clean.height, "format": "JPEG"}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise VisionError(422, "Cette photo est illisible. Essaie une autre photo JPG ou PNG.") from None


def parse_read_result(payload, image_metadata):
    """Keep words/confidences for the assistant; never interpret a drug or dosage here."""
    try:
        blocks = payload["readResult"]["blocks"]
        if not isinstance(blocks, list):
            raise ValueError
        lines, scores, review = [], [], False
        for block in blocks:
            for line in block["lines"]:
                text = line["text"]
                if not isinstance(text, str):
                    raise ValueError
                if not text.strip():
                    continue
                words = []
                for word in line.get("words", []):
                    content, confidence = word["text"], word.get("confidence")
                    if not isinstance(content, str):
                        raise ValueError
                    if confidence is not None and (
                        type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1
                    ):
                        raise ValueError
                    review |= confidence is None or confidence < WORD_REVIEW_THRESHOLD
                    if confidence is not None:
                        scores.append(confidence)
                    words.append({"text": content, "confidence": confidence})
                review |= not words
                lines.append({"text": text.strip(), "words": words})
                if len(lines) > 100 or sum(len(item["text"]) for item in lines) > MAX_OCR_CHARS:
                    raise VisionError(422, "Trop de texte dans cette image. Cadre une seule boîte de médicament.")
        text = "\n".join(line["text"] for line in lines)
        if len(text) > MAX_OCR_CHARS or sum(len(line["words"]) for line in lines) > 1500:
            raise VisionError(422, "Trop de texte dans cette image. Cadre une seule boîte de médicament.")
        return {
            "provider": "azure_vision_read", "text": text, "lines": lines,
            "reading_status": "no_text" if not text else "uncertain" if review else "read",
            "requires_confirmation": not text or review,
            "min_word_confidence": min(scores) if scores else None,
            "image": image_metadata,
            "note": "Texte OCR potentiellement erroné. Les scores concernent la lecture, pas l'identité du médicament. Les champs absents restent inconnus.",
        }
    except (KeyError, ValueError, TypeError, AttributeError):
        raise VisionError(502, "Azure OCR n'a pas renvoyé une lecture exploitable. Réessaie avec une autre photo.") from None


class AzureVisionClient:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.azure_vision_timeout_seconds, trust_env=False)

    async def read(self, payload):
        image, metadata = await asyncio.to_thread(prepare_image, payload)
        if not self.settings.azure_vision_endpoint or not self.settings.azure_vision_key:
            raise VisionError(503, "La lecture des photos n'est pas configurée. Tu peux écrire le nom du médicament ou envoyer un vocal.")
        try:
            response = await self.client.post(
                self.settings.azure_vision_endpoint.rstrip("/") + "/computervision/imageanalysis:analyze",
                params={"api-version": "2024-02-01", "features": "read"},
                headers={"Ocp-Apim-Subscription-Key": self.settings.azure_vision_key,
                         "Content-Type": "application/octet-stream"},
                content=image,
            )
        except httpx.TimeoutException:
            raise VisionError(504, "La lecture de la photo prend trop de temps. Réessaie ou écris le nom du médicament.") from None
        except httpx.HTTPError:
            raise VisionError(503, "Le service de lecture des photos est inaccessible. Tu peux continuer par écrit.") from None
        if response.status_code in {401, 403, 404}:
            raise VisionError(503, "Azure OCR refuse l'accès. Vérifie la clé, l'endpoint Vision et les restrictions réseau du service.")
        if response.status_code == 429:
            raise VisionError(503, "Le service photo est momentanément occupé. Réessaie dans un instant.")
        if response.status_code in {400, 413, 415, 422}:
            raise VisionError(422, "Azure OCR ne peut pas lire cette photo. Cadre une seule boîte avec le nom et le dosage visibles.")
        if response.status_code != 200:
            raise VisionError(503, "Le service de lecture des photos est temporairement indisponible.")
        try:
            body = response.json()
        except ValueError:
            raise VisionError(502, "Réponse illisible du service photo. Réessaie dans un instant.") from None
        return parse_read_result(body, metadata)

    async def close(self):
        await self.client.aclose()
