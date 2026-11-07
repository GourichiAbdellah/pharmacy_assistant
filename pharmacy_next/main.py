from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID
import asyncio

from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .audio import MoulSotClient, TranscriptionError
from .catalogue import Catalogue
from .chat import ChatService
from .config import Settings, UI_VERSION
from .data import CsvRepository, SqlRepository, DataUnavailable
from .llm import ModelClient, ModelUnavailable
from .sessions import SessionError, SessionStore
from .vision import AzureVisionClient, VisionError, MAX_IMAGE_BYTES
from .pharmacies.models import PharmacyError, Position
from .pharmacies.store import PharmacyStore
from .pharmacies.service import PharmacyService
from .pharmacies.routes import build_router, bearer


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=5000)
    session_id: UUID | None = None
    location: Position | None = None


def create_app(settings=None, repository=None, model=None, speech=None, vision=None, pharmacy_store=None):
    settings = settings or Settings.from_env()
    repository = repository or (SqlRepository(settings) if settings.data_source == "azure_sql" else CsvRepository(settings))
    model = model or ModelClient(settings)
    speech = speech or MoulSotClient(settings)
    vision = vision or AzureVisionClient(settings)
    sessions = SessionStore(settings)
    catalogue = Catalogue(repository, settings.catalogue_cache_seconds)
    pharmacy_service = PharmacyService(pharmacy_store or PharmacyStore(settings), catalogue) if settings.pharmacy_backend != 'disabled' else None
    chat_service = ChatService(settings, model, catalogue, sessions, pharmacies=pharmacy_service)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await model.close()
        await speech.close()
        await vision.close()

    app = FastAPI(title="Assistant médicaments — nouveau prototype", version="0.1.0", lifespan=lifespan)
    app.state.chat_service = chat_service
    app.state.pharmacies = pharmacy_service
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    if pharmacy_service:
        app.include_router(build_router(pharmacy_service))
    else:
        @app.get('/api/v1/pharmacies/config')
        def disabled_pharmacies():
            return {'enabled':False,'message':'L’espace pharmacien n’est pas activé sur ce serveur. Contactez l’administrateur.'}

    @app.get('/pharmacien', include_in_schema=False)
    def pharmacist_page():
        return FileResponse(Path(__file__).parent / 'static' / 'pharmacist.html')

    @app.middleware('http')
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(('/api/v1/','/static/')) or request.url.path in {'/','/pharmacien'}:
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(PharmacyError)
    async def pharmacy_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'detail': exc.message})

    @app.exception_handler(DataUnavailable)
    async def data_error(request, exc):
        return JSONResponse(status_code=503, content={'detail': str(exc)})

    async def patient_account(request):
        header = request.headers.get('Authorization')
        if pharmacy_service and header:
            return await asyncio.to_thread(pharmacy_service.store.authenticate, bearer(header), 'patient')
        return None

    def parse_location(raw):
        if not raw:
            return None
        try:
            return Position.model_validate_json(raw).model_dump()
        except ValidationError:
            raise HTTPException(422, 'Position invalide.') from None

    @app.exception_handler(SessionError)
    async def session_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.exception_handler(ModelUnavailable)
    async def model_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(TranscriptionError)
    async def audio_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.exception_handler(VisionError)
    async def image_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    async def health():
        return {"status": "running", "llm_configured": bool(settings.llm_api_key),
                "ui_version": UI_VERSION,
                "vision_configured": bool(settings.azure_vision_endpoint and settings.azure_vision_key),
                "pharmacies_enabled": pharmacy_service is not None,
                "data_source": settings.data_source, "note": "État du processus, pas un test des services externes."}

    @app.post("/api/v1/chat")
    async def chat(body: ChatRequest, request: Request):
        return await chat_service.chat(body.message, str(body.session_id) if body.session_id else None,
                                       patient=await patient_account(request),
                                       location=body.location.model_dump() if body.location else None)

    @app.post("/api/v1/chat/audio")
    async def audio(request: Request, file: UploadFile = File(...), session_id: UUID | None = Form(None),
                    location: str = Form('', max_length=200)):
        account = await patient_account(request)
        position = parse_location(location)
        try:
            payload = await file.read(20 * 1024 * 1024 + 1)
            if not payload or len(payload) > 20 * 1024 * 1024:
                raise TranscriptionError(413, "Envoie un fichier audio non vide de moins de 20 Mo.")
            transcription = await speech.transcribe(payload, file.filename, file.content_type)
        finally:
            await file.close()
        result = await chat_service.chat(transcription["text"], str(session_id) if session_id else None, patient=account, location=position)
        result["transcription"] = transcription
        return result

    @app.delete("/api/v1/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: UUID, request: Request):
        account = await patient_account(request)
        existing = sessions.sessions.get(str(session_id))
        if existing and existing.patient_owner and existing.patient_owner != (account or {}).get('owner_id'):
            raise PharmacyError(403, 'Cette conversation appartient à un autre accès.')
        sessions.delete(str(session_id))

    @app.post("/api/v1/chat/image")
    async def image(request: Request, file: UploadFile = File(...), session_id: UUID | None = Form(None),
                    message: str = Form("", max_length=5000), location: str = Form('', max_length=200)):
        account = await patient_account(request)
        position = parse_location(location)
        try:
            payload = await file.read(MAX_IMAGE_BYTES + 1)
            if not payload:
                raise VisionError(422, "La photo est vide. Choisis une photo de la boîte.")
            if len(payload) > MAX_IMAGE_BYTES:
                raise VisionError(413, "La photo dépasse 10 Mo. Envoie une photo JPG ou PNG plus légère.")
        finally:
            await file.close()
        # OCR runs under the conversation lock, after checking the session exists.
        return await chat_service.chat(
            message.strip() or "Peux-tu m'aider à identifier le médicament sur cette photo ?",
            str(session_id) if session_id else None,
            image_reader=lambda: vision.read(payload),
            patient=account, location=position,
        )

    return app


app = create_app()
