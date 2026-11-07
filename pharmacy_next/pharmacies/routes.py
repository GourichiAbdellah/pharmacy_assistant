from collections import defaultdict
import hashlib
import threading
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from .models import (PharmacyError, NearbyInput, DraftInput, ConfirmInput,
                     ResponseInput, ClarificationInput, DutyInput, OpeningInput, local_iso)
from .handoff import HumanMessage
from fastapi import Query


def bearer(value):
    if not value or not value.startswith('Bearer '):
        raise PharmacyError(401, 'Un accès est nécessaire pour cette action.')
    return value[7:]


class IdentityLimiter:
    """Local pilot rate limit; an ingress limit is required for public multi-instance use."""
    def __init__(self):
        self.hits = defaultdict(list)
        self.lock = threading.Lock()

    def check(self, address):
        now = time.monotonic()
        key = hashlib.sha256(address.encode()).hexdigest()
        with self.lock:
            for old in list(self.hits):
                self.hits[old] = [t for t in self.hits[old] if now-t < 3600]
                if not self.hits[old]:
                    del self.hits[old]
            if len(self.hits) >= 2000 and key not in self.hits:
                raise PharmacyError(429, 'Trop de connexions. Réessaie plus tard.')
            if len(self.hits[key]) >= 30:
                raise PharmacyError(429, 'Trop de nouveaux accès. Réessaie plus tard.')
            self.hits[key].append(now)


def build_router(service):
    router = APIRouter()
    limiter = IdentityLimiter()

    def patient(authorization: str | None = Header(None)):
        return service.store.authenticate(bearer(authorization), 'patient')

    def pharmacist(authorization: str | None = Header(None)):
        account = service.store.authenticate(bearer(authorization), 'pharmacist')
        service.active_pharmacist(account)
        return account

    @router.get('/api/v1/pharmacies/config')
    def configuration():
        return {'enabled': True, 'is_demo': service.is_demo, 'timezone': 'Africa/Casablanca',
                'directory_provider': service.store.settings.pharmacy_directory_provider,
                'duty_api_status': 'not_connected',
                'coverage': 'Annuaire du prototype, pas une couverture nationale exhaustive.'}

    @router.post('/api/v1/pharmacies/search')
    def search(body: NearbyInput):
        return service.search(body)

    @router.post('/api/v1/availability/identity', status_code=201)
    def identity(request: Request):
        limiter.check(request.client.host if request.client else 'unknown')
        result = service.store.issue_access('patient')
        return {'token': result['token'], 'expires_at': local_iso(result['expires_at'])}

    @router.get('/api/v1/availability/requests')
    def patient_requests(account=Depends(patient)):
        return {'requests': service.list_requests(account), 'is_demo': service.is_demo}

    @router.post('/api/v1/availability/drafts', status_code=201)
    def draft(body: DraftInput, account=Depends(patient)):
        return service.draft(account, body)

    @router.get('/api/v1/availability/requests/{request_id}')
    def patient_request(request_id: UUID, account=Depends(patient)):
        return service.list_requests(account, request_id)[0]

    @router.post('/api/v1/availability/requests/{request_id}/confirm')
    def confirm(request_id: UUID, body: ConfirmInput, account=Depends(patient)):
        return service.change_request(account, request_id, 'confirm')

    @router.post('/api/v1/availability/requests/{request_id}/cancel')
    def cancel(request_id: UUID, account=Depends(patient)):
        return service.change_request(account, request_id, 'cancel')

    @router.post('/api/v1/availability/requests/{request_id}/clarify')
    def clarify(request_id: UUID, body: ClarificationInput, account=Depends(patient)):
        return service.change_request(account, request_id, 'clarify', body.text)

    @router.get('/api/v1/pharmacist/profile')
    def profile(account=Depends(pharmacist)):
        return service.pharmacist_profile(account)

    @router.post('/api/v1/availability/requests/{request_id}/handoff', status_code=201)
    def start_handoff(request_id: UUID, body: ConfirmInput, account=Depends(patient)):
        return service.handoffs.start(account,request_id)

    @router.get('/api/v1/availability/handoffs')
    def patient_handoffs(account=Depends(patient)):
        return {'handoffs':service.handoffs.list(account,active_only=True)}

    @router.get('/api/v1/availability/handoffs/{handoff_id}')
    def patient_thread(handoff_id: UUID, after: int=Query(0,ge=0), account=Depends(patient)):
        return service.handoffs.read(account,handoff_id,after)

    @router.post('/api/v1/availability/handoffs/{handoff_id}/messages')
    def patient_message(handoff_id: UUID, body: HumanMessage, account=Depends(patient)):
        return service.handoffs.change(account,handoff_id,'message',body)

    @router.post('/api/v1/availability/handoffs/{handoff_id}/close')
    def patient_close(handoff_id: UUID, account=Depends(patient)):
        return service.handoffs.change(account,handoff_id,'close')

    @router.get('/api/v1/pharmacist/dashboard')
    def dashboard(account=Depends(pharmacist)):
        return service.handoffs.dashboard(account)

    @router.get('/api/v1/pharmacist/handoffs')
    def pharmacist_handoffs(account=Depends(pharmacist)):
        return {'handoffs':service.handoffs.list(account)}

    @router.get('/api/v1/pharmacist/handoffs/{handoff_id}')
    def pharmacist_thread(handoff_id: UUID, after: int=Query(0,ge=0), account=Depends(pharmacist)):
        return service.handoffs.read(account,handoff_id,after)

    @router.post('/api/v1/pharmacist/handoffs/{handoff_id}/accept')
    def pharmacist_accept(handoff_id: UUID, account=Depends(pharmacist)):
        return service.handoffs.change(account,handoff_id,'accept')

    @router.post('/api/v1/pharmacist/handoffs/{handoff_id}/messages')
    def pharmacist_message(handoff_id: UUID, body: HumanMessage, account=Depends(pharmacist)):
        return service.handoffs.change(account,handoff_id,'message',body)

    @router.post('/api/v1/pharmacist/handoffs/{handoff_id}/close')
    def pharmacist_close(handoff_id: UUID, account=Depends(pharmacist)):
        return service.handoffs.change(account,handoff_id,'close')

    @router.get('/api/v1/pharmacist/requests')
    def inbox(account=Depends(pharmacist)):
        return {'requests': service.list_requests(account), 'is_demo': service.is_demo}

    @router.post('/api/v1/pharmacist/requests/{request_id}/response')
    def respond(request_id: UUID, body: ResponseInput, account=Depends(pharmacist)):
        return service.change_request(account, request_id, body.status, body.comment)

    @router.post('/api/v1/pharmacist/opening')
    def opening(body: OpeningInput, account=Depends(pharmacist)):
        service.store.opening(account['pharmacy_id'], body.is_open)
        return service.pharmacist_profile(account)

    @router.post('/api/v1/pharmacist/duties', status_code=201)
    def add_duty(body: DutyInput, account=Depends(pharmacist)):
        return service.add_duty(account, body)

    @router.delete('/api/v1/pharmacist/duties/{duty_id}', status_code=204)
    def cancel_duty(duty_id: UUID, account=Depends(pharmacist)):
        service.store.cancel_duty(account['pharmacy_id'], duty_id)

    return router
