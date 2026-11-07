"""Read-only directory adapter. No Google content is written to the catalogue.

One request per search, a fixed field mask, no retries/pagination or background fetch.
Transport injection is for offline contract tests, never a user-selected upstream URL.
"""
from collections import deque
from datetime import datetime
import json
import math
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

from .models import CASABLANCA, PharmacyError, distance_km

GOOGLE_ROOT = 'https://places.googleapis.com/v1/places:'
FIELD_MASK = ','.join('places.'+key for key in (
    'id', 'displayName', 'formattedAddress', 'addressComponents', 'location',
    'businessStatus', 'types', 'attributions'))
MAX_RESPONSE_BYTES = 1_000_000


def valid_place_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{5,255}', value))


def https_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        url = urlsplit(value)
        return value if url.scheme == 'https' and url.hostname and not url.username and not url.password else None
    except ValueError:
        return None


class ProviderUnavailable(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class RequestBudget:
    """Process-local circuit limit, shared by chat and direct search. No GPS logs.

    Resets on restart; use provider-side quotas for a durable deployment-wide cap.
    Failed paid attempts consume budget too. No sleeping or automatic retry.
    """
    def __init__(self, per_minute, per_day, clock=time.time):
        self.per_minute, self.per_day, self.clock = per_minute, per_day, clock
        self.recent, self.day, self.daily_count = deque(), None, 0
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            now = self.clock()
            day = datetime.fromtimestamp(now, CASABLANCA).date()
            if day != self.day:
                self.day, self.daily_count = day, 0
            while self.recent and now-self.recent[0] >= 60:
                self.recent.popleft()
            if len(self.recent) >= self.per_minute or self.daily_count >= self.per_day:
                raise ProviderUnavailable('local_limit', 'La limite de recherches externes du prototype est atteinte. Réessaie plus tard.')
            self.recent.append(now)
            self.daily_count += 1


class GooglePlacesClient:
    def __init__(self, settings, *, transport=None, budget=None):
        self.settings, self.transport = settings, transport
        self.budget = budget or RequestBudget(settings.google_places_max_requests_per_minute,
                                             settings.google_places_max_requests_per_day)

    def search(self, query):
        if query.radius_km > 50:
            raise PharmacyError(422, 'La recherche Google accepte un rayon maximal de 50 km.')
        if not self.settings.google_places_api_key.strip():
            raise ProviderUnavailable('not_configured', 'La recherche Google Maps n’est pas encore configurée.')
        cap = min(query.limit, 20)
        payload = {'languageCode': 'fr', 'regionCode': 'MA'}
        has_position = query.latitude is not None
        circle = {'center': {'latitude': query.latitude, 'longitude': query.longitude},
                  'radius': query.radius_km*1000} if has_position else None
        # If a city/district was explicitly given, keep it in the text query.
        if has_position and not query.city and not query.district:
            endpoint = 'searchNearby'
            payload.update(includedTypes=['pharmacy'], maxResultCount=cap,
                           rankPreference='DISTANCE', locationRestriction={'circle': circle})
            mask = FIELD_MASK
        else:
            endpoint = 'searchText'
            payload.update(textQuery='pharmacie '+', '.join(x for x in (query.district, query.city, 'Maroc') if x),
                           includedType='pharmacy', strictTypeFiltering=True, pageSize=cap)
            if circle:
                payload['locationBias'] = {'circle': circle}
            mask = FIELD_MASK+',nextPageToken'
        self.budget.take()
        try:
            with httpx.Client(timeout=self.settings.google_places_timeout_seconds,
                              follow_redirects=False, transport=self.transport) as client:
                with client.stream('POST', GOOGLE_ROOT+endpoint, json=payload, headers={
                        'X-Goog-Api-Key': self.settings.google_places_api_key,
                        'X-Goog-FieldMask': mask, 'Accept': 'application/json'}) as response:
                    if response.status_code in (401, 403):
                        raise ProviderUnavailable('access_denied', 'Google Maps refuse l’accès. Vérifie la clé, les restrictions, l’API activée et la facturation côté serveur.')
                    if response.status_code == 429:
                        raise ProviderUnavailable('upstream_limit', 'Le quota Google Maps est atteint. Réessaie plus tard.')
                    if response.status_code != 200:
                        raise ProviderUnavailable('upstream_unavailable', 'La recherche Google Maps est momentanément indisponible.')
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise ValueError('oversized response')
                    data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get('places', []), list) or 'error' in data:
                raise ValueError('invalid response')
            places = data.get('places', [])
            items, seen = [], set()
            skipped = 0
            for raw_place in places[:cap]:
                item = self._place(raw_place, query)
                if item is None:
                    skipped += 1
                elif item['external_id'] not in seen:
                    seen.add(item['external_id'])
                    items.append(item)
            return {'results': items, 'more_results_possible': bool(data.get('nextPageToken')) or len(places) >= cap,
                    'discarded_results': skipped}
        except ProviderUnavailable:
            raise
        except httpx.TimeoutException:
            raise ProviderUnavailable('timeout', 'Google Maps ne répond pas dans le délai prévu. Réessaie dans un instant.') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            # Never echo upstream response bodies, headers or exception URLs.
            raise ProviderUnavailable('invalid_response', 'Impossible de lire les résultats Google Maps pour le moment.') from None

    @staticmethod
    def _place(raw, query):
        if not isinstance(raw, dict) or not valid_place_id(raw.get('id')):
            return None
        if 'pharmacy' not in (raw.get('types') or []) or raw.get('businessStatus') in {
                'CLOSED_PERMANENTLY', 'CLOSED_TEMPORARILY', 'FUTURE_OPENING'}:
            return None
        components = raw.get('addressComponents')
        if not isinstance(components, list):
            return None
        def component(kind, short=False):
            return next((c.get('shortText' if short else 'longText', '') for c in components
                         if isinstance(c, dict) and kind in (c.get('types') or [])), '')
        # regionCode controls formatting; it is not a Morocco-only filter.
        if component('country', True) != 'MA':
            return None
        point, title = raw.get('location'), raw.get('displayName')
        if not isinstance(point, dict) or not isinstance(title, dict):
            return None
        lat, lon, name = point.get('latitude'), point.get('longitude'), title.get('text')
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lat, lon))
                or not -90 <= lat <= 90 or not -180 <= lon <= 180 or not isinstance(name, str) or not name.strip()
                or not isinstance(raw.get('formattedAddress'), str)):
            return None
        distance = distance_km(query.latitude, query.longitude, lat, lon) if query.latitude is not None else None
        if distance is not None and distance > query.radius_km:
            return None
        attributions = []
        for a in raw.get('attributions') or []:
            if isinstance(a, dict) and isinstance(a.get('provider'), str):
                attributions.append({'label': a['provider'], 'url': https_url(a.get('providerUri'))})
        return dict(id=None, external_id=raw['id'], source='google_places', source_label='Google Maps',
            name=name, address=raw['formattedAddress'], city=component('locality'),
            district=component('sublocality_level_1') or component('sublocality'),
            latitude=lat, longitude=lon, phone=None, partner=False, is_demo=False,
            accepting_requests=False, duty_status='unconfirmed', duty=None,
            duty_note='Aucun planning de garde actuel vérifié pour cette fiche.',
            opening_status='unknown', opening_confirmed_at=None, opening_valid_until=None,
            stock_status='unknown', distance_km=round(distance, 2) if distance is not None else None,
            attributions=attributions, maps_url='https://www.google.com/maps/dir/?'+urlencode({
                'api':'1', 'destination':f'{lat},{lon}', 'destination_place_id':raw['id']}))
