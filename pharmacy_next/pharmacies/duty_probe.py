"""Diagnostic only for the documented Le Matin endpoint.

The public documentation supplies a route, not a response schema or exact night
hours. A successful HTTP response is NOT proof that a pharmacy is on duty now.
Never called by a patient request. No raw response/secret is saved or printed.
"""
from datetime import date
import json
from urllib.error import URLError, HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .models import PharmacyError
from .providers import MAX_RESPONSE_BYTES


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def shape(value, depth=0):
    """Describe JSON structure only: never include string values from upstream."""
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k)[:80]: shape(v, depth+1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return {'type': 'array', 'length': len(value), 'first_item_schema': shape(value[0], depth+1) if value else None}
    return type(value).__name__


def probe_lematin(settings, requested_date, garde, *, opener=None):
    try:
        day = date.fromisoformat(requested_date).isoformat()
    except (ValueError, TypeError):
        raise PharmacyError(422, 'La date doit être au format YYYY-MM-DD.') from None
    if garde not in {'jour', 'nuit', '24'}:
        raise PharmacyError(422, 'Garde attendue : jour, nuit ou 24.')
    if not settings.lematin_api_key.strip():
        raise PharmacyError(503, 'Configure LEMATIN_API_KEY avec un accès autorisé fourni par Le Matin.')
    url = 'https://in.lematin.ma/api/v1/'+quote(settings.lematin_api_key, safe='')+'/pharmacies?'+urlencode({'garde':garde, 'date':day})
    headers = {'Accept': 'application/json'}
    if settings.lematin_oauth_token:
        headers['Authorization'] = 'Bearer '+settings.lematin_oauth_token
    # urllib avoids HTTPX info logs containing the API key embedded in this URL.
    # No redirects: neither the path key nor an OAuth header may be forwarded.
    opener = opener or build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(Request(url, headers=headers), timeout=10) as response:
            if response.status != 200:
                raise ValueError('unexpected status')
            raw = response.read(MAX_RESPONSE_BYTES+1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('oversized response')
        data = json.loads(raw)
        if not isinstance(data, (dict, list)):
            raise ValueError('unexpected payload')
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise PharmacyError(503, 'Accès Le Matin refusé. Fais valider la clé et le mécanisme d’authentification par le fournisseur.') from None
        raise PharmacyError(503, 'Le Matin n’a pas accepté la requête de diagnostic.') from None
    except (URLError, OSError, ValueError):
        raise PharmacyError(503, 'Réponse Le Matin inaccessible ou non JSON. Aucun planning importé.') from None
    return {'provider': 'Le Matin', 'http_ok': True, 'date_requested': day, 'garde_requested': garde,
            'payload_schema': shape(data), 'contract_verified': False, 'imported_duties': 0,
            'next_step': 'Faire valider le schéma, les identifiants, les dates et les heures locales par le fournisseur avant de développer le mapping. Ce diagnostic ne confirme aucune garde.'}
