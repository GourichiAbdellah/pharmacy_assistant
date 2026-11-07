import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import time
from uuid import uuid4


class SessionError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


@dataclass
class Context:
    selected_codes: list[str] = field(default_factory=list)
    organism: str | None = None
    known_medications: dict = field(default_factory=dict)
    recent_notice_ids: list[str] = field(default_factory=list)
    cnops_coverage_by_code: dict = field(default_factory=dict)
    cnops_estimates: dict = field(default_factory=dict)
    cnss_estimates: dict = field(default_factory=dict)

    def remember_records(self, records):
        for row in records:
            code = row["code"]
            previous = self.known_medications.pop(code, None)
            self.known_medications[code] = row
            if previous != row:
                self.cnops_estimates.pop(code, None)
                self.cnss_estimates.pop(code, None)
        for code in list(self.known_medications):
            if len(self.known_medications) <= 100:
                break
            if code not in self.selected_codes:
                self.known_medications.pop(code)
                self.cnops_coverage_by_code.pop(code, None)
                self.cnops_estimates.pop(code, None)
                self.cnss_estimates.pop(code, None)


@dataclass
class Session:
    patient_owner: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    turns: list = field(default_factory=list)
    context: Context = field(default_factory=Context)
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """Prototype : mémoire par processus, un worker. Pas de conservation disque."""
    def __init__(self, settings):
        self.settings = settings
        self.sessions = {}

    def _prune(self):
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if not session.lock.locked() and now - session.touched > self.settings.session_ttl_seconds:
                del self.sessions[key]

    @asynccontextmanager
    async def use(self, session_id):
        self._prune()
        if session_id:
            session = self.sessions.get(session_id)
            if session is None:
                raise SessionError(410, "Cette conversation a expiré. Démarre une nouvelle discussion.")
        else:
            if len(self.sessions) >= self.settings.session_max_count:
                raise SessionError(503, "Trop de conversations actives. Réessaie plus tard.")
            session = Session()
            self.sessions[session.id] = session
        if session.lock.locked():
            raise SessionError(409, "Une réponse est déjà en préparation dans cette conversation.")
        async with session.lock:
            try:
                yield session
            except Exception:
                if not session_id and not session.turns:
                    self.sessions.pop(session.id, None)
                raise
            finally:
                session.touched = time.monotonic()

    def delete(self, session_id):
        session = self.sessions.get(session_id)
        if session and session.lock.locked():
            raise SessionError(409, "Attends la fin de la réponse avant de supprimer la conversation.")
        self.sessions.pop(session_id, None)
