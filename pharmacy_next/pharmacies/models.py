from datetime import datetime, timezone, timedelta
import math
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator, field_validator

CASABLANCA = ZoneInfo("Africa/Casablanca")


class PharmacyError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def epoch(value: str) -> int:
    """Offset dates are absolute; naive form values are Casablanca wall time.

    Refuse ambiguous/non-existent wall times instead of silently picking an offset.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            candidates = []
            for fold in (0, 1):
                aware = dt.replace(tzinfo=CASABLANCA, fold=fold)
                if aware.astimezone(timezone.utc).astimezone(CASABLANCA).replace(tzinfo=None) == dt:
                    candidates.append(aware.timestamp())
            if len(set(candidates)) != 1:
                raise ValueError("Heure locale ambiguë ou inexistante ; fournir un décalage UTC explicite.")
            return int(candidates[0])
        return int(dt.timestamp())
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Date ISO invalide ou heure locale ambiguë/inexistante à Casablanca.") from exc


def local_iso(value):
    return datetime.fromtimestamp(value, CASABLANCA).isoformat() if value is not None else None


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class RegularHours(Input):
    open_24h: StrictBool = False
    opens_at: str | None = Field(default=None,pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    closes_at: str | None = Field(default=None,pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    days_of_week: list[StrictInt] = Field(default_factory=lambda:list(range(7)),min_length=1,max_length=7)

    @model_validator(mode='after')
    def valid_hours(self):
        if any(isinstance(i,bool) or i not in range(7) for i in self.days_of_week) or len(set(self.days_of_week))!=len(self.days_of_week):
            raise ValueError('Jours : 0=lundi à 6=dimanche, sans doublon.')
        if self.open_24h:
            if set(self.days_of_week)!=set(range(7)) or self.opens_at or self.closes_at:
                raise ValueError('24h/24 permanent : 7j/7, sans heures de début/fin.')
        elif not self.opens_at or not self.closes_at or self.opens_at>=self.closes_at:
            raise ValueError('Horaires habituels : ouverture avant fermeture, par exemple 09:00–19:30.')
        return self


class PharmacyInput(Input):
    id: UUID
    name: str = Field(min_length=3, max_length=160)
    city: str = Field(min_length=2, max_length=100)
    district: str = Field(default="", max_length=100)
    address: str = Field(min_length=3, max_length=300)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9 ()-]{6,25}$")
    partner: StrictBool = False
    active: StrictBool = True
    is_demo: StrictBool = False
    regular_hours: RegularHours | None = None


class DutyInput(Input):
    starts_at: str = Field(max_length=40)
    ends_at: str = Field(max_length=40)
    duty_type: Literal["day", "night", "24h"]

    @model_validator(mode="after")
    def interval(self):
        start, end = epoch(self.starts_at), epoch(self.ends_at)
        if not 0 < end - start <= 31 * 86400:
            raise ValueError("Fin après début ; un créneau dure au maximum 31 jours.")
        if self.duty_type=='24h':
            first=datetime.fromtimestamp(start,CASABLANCA)
            last=datetime.fromtimestamp(end,CASABLANCA)
            if last.date()!=first.date()+timedelta(days=1) or last.timetz().replace(tzinfo=None)!=first.timetz().replace(tzinfo=None):
                raise ValueError('Une garde 24h doit couvrir une journée complète, du début au lendemain à la même heure locale.')
        return self


class ImportedDuty(DutyInput):
    id: UUID
    pharmacy_id: UUID
    source_label: str = Field(min_length=3, max_length=200)
    source_url: str | None = Field(default=None, max_length=1000)
    source_checked_at: str = Field(max_length=40)
    cancelled: StrictBool = False

    @field_validator("source_url")
    @classmethod
    def source_link(cls, value):
        if value is not None:
            from urllib.parse import urlsplit
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Source : URL HTTPS sans identifiants.")
        return value

    @field_validator("source_checked_at")
    @classmethod
    def checked_date(cls, value):
        epoch(value)
        return value


class DirectoryImport(Input):
    pharmacies: list[PharmacyInput] = Field(max_length=5000)
    duties: list[ImportedDuty] = Field(default_factory=list, max_length=10000)


class NearbyInput(Input):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    radius_km: float = Field(default=10, gt=0, le=100)
    on_duty_only: bool = False
    open_confirmed_only: bool = False
    limit: int = Field(default=10, ge=1, le=30)

    @model_validator(mode="after")
    def position(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Latitude et longitude doivent être fournies ensemble.")
        if self.latitude is None and not self.city:
            raise ValueError("Indique une ville ou utilise ta position.")
        return self


class DraftInput(Input):
    pharmacy_id: UUID
    medication_code: str = Field(min_length=1, max_length=50)
    boxes: int = Field(default=1, ge=1, le=10, strict=True)


class ConfirmInput(Input):
    confirmed: StrictBool

    @field_validator('confirmed')
    @classmethod
    def must_confirm(cls, value):
        if not value:
            raise ValueError('La confirmation explicite est requise.')
        return value


class Position(Input):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ResponseInput(Input):
    status: Literal["available", "unavailable", "needs_details"]
    comment: str = Field(default="", max_length=500)

    @model_validator(mode='after')
    def clarification_needed(self):
        if self.status == 'needs_details' and not self.comment:
            raise ValueError('Indique la précision à demander.')
        return self


class ClarificationInput(Input):
    text: str = Field(min_length=1, max_length=500)


class OpeningInput(Input):
    is_open: StrictBool


def distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    x = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, x))))
