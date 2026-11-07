from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

UI_VERSION = '20261025-availability-v2'


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_api_key: str = field(default="", repr=False)
    llm_timeout_seconds: int = 60
    chat_max_tool_rounds: int = 6
    data_source: str = "azure_sql"
    azure_sql_server: str = "pharmacy-admin.database.windows.net"
    azure_sql_database: str = "free-sql-db-0919104"
    azure_sql_username: str = "pharmacy"
    azure_sql_password: str = field(default="", repr=False)
    sql_login_timeout_seconds: int = 30
    sql_query_timeout_seconds: int = 45
    catalogue_cache_seconds: int = 300
    medications_csv: str = ""
    notices_csv: str = ""
    moulsot_base_url: str = "http://127.0.0.1:8001"
    moulsot_api_key: str = field(default="", repr=False)
    moulsot_timeout_seconds: int = 120
    azure_vision_endpoint: str = ""
    azure_vision_key: str = field(default="", repr=False)
    azure_vision_timeout_seconds: int = 30
    session_ttl_seconds: int = 3600
    session_max_turns: int = 16
    session_max_count: int = 100
    pharmacy_backend: str = "disabled"
    pharmacy_environment: str = "demo"
    pharmacy_sqlite_path: str = "./var/pharmacies.sqlite3"
    pharmacy_sql_server: str = ""
    pharmacy_sql_database: str = ""
    pharmacy_sql_username: str = ""
    pharmacy_sql_password: str = field(default="", repr=False)
    pharmacy_duty_max_age_hours: int = 72
    pharmacy_directory_provider: str = "local"
    google_places_api_key: str = field(default="", repr=False)
    google_places_timeout_seconds: int = 10
    google_places_max_requests_per_minute: int = 20
    google_places_max_requests_per_day: int = 200
    lematin_api_key: str = field(default="", repr=False)
    lematin_oauth_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
        values = {}
        for name, spec in cls.__dataclass_fields__.items():
            if name.upper() in os.environ:
                raw = os.environ[name.upper()]
                values[name] = int(raw) if isinstance(spec.default, int) else raw
        result = cls(**values)
        if result.llm_provider not in {"deepseek", "azure", "compatible"}:
            raise ValueError("LLM_PROVIDER doit être deepseek, azure ou compatible.")
        if result.data_source not in {"azure_sql", "csv"}:
            raise ValueError("DATA_SOURCE doit être azure_sql ou csv.")
        if result.pharmacy_backend not in {"disabled", "sqlite", "azure_sql"}:
            raise ValueError("PHARMACY_BACKEND doit être disabled, sqlite ou azure_sql.")
        if result.pharmacy_environment not in {"demo", "live"}:
            raise ValueError("PHARMACY_ENVIRONMENT doit être demo ou live.")
        if result.pharmacy_directory_provider not in {"local", "google_places"}:
            raise ValueError("PHARMACY_DIRECTORY_PROVIDER doit être local ou google_places.")
        if result.pharmacy_directory_provider == "google_places" and (
                result.pharmacy_environment != "live" or result.pharmacy_backend == "disabled"):
            raise ValueError("Google Places nécessite le module pharmacies activé en mode live.")
        for name in ("llm_base_url", "moulsot_base_url"):
            parsed = urlparse(getattr(result, name))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
                raise ValueError(f"URL invalide : {name}")
        for name in cls.__dataclass_fields__:
            value = getattr(result, name)
            if isinstance(value, int) and value < 1:
                raise ValueError(f"{name} doit être positif.")
        if result.azure_vision_endpoint:
            parsed = urlparse(result.azure_vision_endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
                raise ValueError("AZURE_VISION_ENDPOINT doit être l'URL HTTPS racine de la ressource Vision.")
        if result.chat_max_tool_rounds > 10:
            raise ValueError("CHAT_MAX_TOOL_ROUNDS doit être inférieur ou égal à 10.")
        return result
