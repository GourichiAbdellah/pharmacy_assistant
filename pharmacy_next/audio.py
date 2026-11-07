import httpx


class TranscriptionError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class MoulSotClient:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.moulsot_timeout_seconds, trust_env=False)

    async def transcribe(self, payload, filename, content_type):
        headers = {}
        if self.settings.moulsot_api_key:
            headers["Authorization"] = "Bearer " + self.settings.moulsot_api_key
        try:
            response = await self.client.post(
                self.settings.moulsot_base_url.rstrip("/") + "/transcribe",
                files={"file": (filename or "audio.webm", payload, content_type or "application/octet-stream")},
                headers=headers,
            )
        except httpx.TimeoutException:
            raise TranscriptionError(504, "La transcription prend trop de temps. Tu peux poursuivre par écrit.") from None
        except httpx.HTTPError:
            raise TranscriptionError(503, "MoulSot est inaccessible. Vérifie son terminal ; le chat écrit reste disponible.") from None
        if response.status_code in {413, 422}:
            raise TranscriptionError(422, "Ce vocal n'a pas pu être transcrit. Essaie un fichier lisible de moins de 30 secondes.")
        if response.status_code >= 400:
            raise TranscriptionError(503, "MoulSot est occupé ou indisponible. Réessaie ou écris ton message.")
        try:
            result = response.json()
            transcript = result.get("text")
            if not isinstance(transcript, str) or not 1 <= len(transcript.strip()) <= 5000:
                raise ValueError
            return {"text": transcript.strip(), "language": result.get("language")}
        except (ValueError, TypeError, AttributeError):
            raise TranscriptionError(502, "MoulSot n'a pas renvoyé une transcription exploitable.") from None

    async def close(self):
        await self.client.aclose()
