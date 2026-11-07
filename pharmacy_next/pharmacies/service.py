import json
from datetime import datetime
from urllib.parse import urlencode
from uuid import uuid4

from ..catalogue import normalize
from .models import PharmacyError, NearbyInput, epoch, local_iso, distance_km, CASABLANCA
from .providers import GooglePlacesClient, ProviderUnavailable
from .handoff import HandoffService


class PharmacyService:
    def __init__(self, store, catalogue, *, directory_provider=None):
        self.store, self.catalogue = store, catalogue
        self.handoffs = HandoffService(store)
        self.external = None
        if store.settings.pharmacy_directory_provider == 'google_places':
            if self.is_demo:
                raise ValueError('Google Places nécessite PHARMACY_ENVIRONMENT=live.')
            self.external = directory_provider or GooglePlacesClient(store.settings)

    @property
    def is_demo(self):
        return self.store.settings.pharmacy_environment == 'demo'

    def pharmacy_view(self, row, duties=(), accepting=False, schedule=None):
        now = self.store.now()
        intervals = [d for d in duties if d['pharmacy_id'] == row['id'] and not d['cancelled']
                     and d['starts_at'] <= now < d['ends_at']]
        fresh = [d for d in intervals if 0 <= now-d['source_checked_at'] <= self.store.settings.pharmacy_duty_max_age_hours*3600]
        fresh.sort(key=lambda x: x['source_checked_at'], reverse=True)
        duty = fresh[0] if fresh else None
        opening = 'unknown'
        if row.get('opening_valid_until') and row['opening_valid_until'] > now and row['open_flag'] is not None:
            opening = 'confirmed_open' if row['open_flag'] else 'confirmed_closed'
        result = {k: row[k] for k in ('id','name','city','district','address','latitude','longitude','phone')}
        result.update({
            'source': 'local', 'source_label': 'Annuaire vérifié du prototype',
            'regular_hours':schedule,
            'partner': bool(row['partner']), 'is_demo': bool(row['is_demo']),
            'accepting_requests': bool(accepting and row['partner'] and row['active']),
            'duty_status': 'scheduled_now' if duty else 'unconfirmed',
            'duty': self.duty_view(duty) if duty else None,
            'duty_note': 'Planning trop ancien : garde non confirmée.' if intervals and not duty else
                         'Garde annoncée par la source ; ouverture et stock à confirmer.' if duty else
                         'Aucun planning de garde actuel vérifié dans notre annuaire.',
            'opening_status': opening,
            'opening_confirmed_at': local_iso(row.get('opening_confirmed_at')),
            'opening_valid_until': local_iso(row.get('opening_valid_until')),
            'stock_status': 'unknown', 'distance_km': None,
            'maps_url': None if row['is_demo'] else 'https://www.google.com/maps/dir/?'+urlencode({
                'api': '1', 'destination': f"{row['latitude']},{row['longitude']}"}),
        })
        return result

    @staticmethod
    def duty_view(row):
        return {**{k: row[k] for k in ('id','pharmacy_id','duty_type','source_label','source_url')},
                **{k: local_iso(row[k]) for k in ('starts_at','ends_at','source_checked_at')},
                'cancelled': bool(row['cancelled']), 'timezone': 'Africa/Casablanca'}

    def search(self, query: NearbyInput):
        local_warning = None
        try:
            rows, duties, accepting = self.store.directory()
            schedules=self.store.schedules()
            links = self.store.external_links() if self.external else {}
        except PharmacyError:
            if not self.external:
                raise
            rows, duties, accepting, links, schedules = [], [], set(), {}, {}
            local_warning = 'Le registre des partenaires est inaccessible. Gardes et demandes en ligne non vérifiables.'
        result = []
        for row in rows:
            if query.city and normalize(query.city) != row['city_key']:
                continue
            if query.district and normalize(query.district) not in row['district_key']:
                continue
            item = self.pharmacy_view(row, duties, row['id'] in accepting,schedules.get(row['id']))
            if query.latitude is not None:
                distance = distance_km(query.latitude, query.longitude, row['latitude'], row['longitude'])
                if distance > query.radius_km:
                    continue
                item['distance_km'] = round(distance, 2)
            if query.on_duty_only and item['duty_status'] != 'scheduled_now':
                continue
            if query.open_confirmed_only and item['opening_status'] != 'confirmed_open':
                continue
            result.append(item)
        local_total = len(result)
        provider_status = {'provider': 'local', 'status': 'ok'}
        more = False
        if self.external:
            provider_status = {'provider': 'google_places', 'status': 'not_requested'}
            if query.on_duty_only or query.open_confirmed_only:
                provider_status['message'] = 'Ces filtres utilisent uniquement nos plannings vérifiés et les déclarations des partenaires. Google ne confirme pas une garde.'
            else:
                try:
                    external = self.external.search(query)
                    provider_status['status'] = 'ok'
                    provider_status['discarded_results'] = external['discarded_results']
                    more = external['more_results_possible']
                    by_id = {p['id']: p for p in rows}
                    replaced = set()
                    for item in external['results']:
                        pharmacy_id = links.get(item['external_id'])
                        row = by_id.get(pharmacy_id)
                        if row:
                            owned = self.pharmacy_view(row, duties, pharmacy_id in accepting,schedules.get(pharmacy_id))
                            for key in ('id', 'partner', 'accepting_requests', 'duty_status', 'duty', 'duty_note',
                                        'opening_status', 'opening_confirmed_at', 'opening_valid_until', 'phone','regular_hours'):
                                item[key] = owned[key]
                            item['registered_name'] = owned['name']
                            replaced.add(pharmacy_id)
                        result.append(item)
                    # Only an administrator-verified Place ID can merge two records.
                    result = [p for p in result if p['source'] != 'local' or p['id'] not in replaced]
                except ProviderUnavailable as exc:
                    provider_status.update(status='unavailable', code=exc.code, message=exc.message)
        if query.latitude is not None:
            result.sort(key=lambda x: (x['distance_km'] if x['distance_km'] is not None else float('inf'), x['name']))
        elif not self.external:
            result.sort(key=lambda x: x['name'])
        coverage = ('Annuaire de démonstration : pharmacies fictives, aucun contact réel.' if self.is_demo else
                    'Résultats limités à notre annuaire. Aucun résultat ne signifie pas absence de pharmacie de garde dans la ville.')
        if self.external:
            coverage = ('Annuaire de partenaires et résultats Google Maps au Maroc. Liste non exhaustive ; '
                        'la présence sur Google ne permet pas de recevoir des demandes sur cette application.')
            if provider_status['status'] != 'ok':
                coverage = 'Seul notre annuaire vérifié est affiché. La liste n’est pas exhaustive.'
        return {'results': result[:query.limit], 'total_in_directory': local_total,
                'local_results_count': local_total, 'returned_count': min(len(result), query.limit),
                'truncated': len(result) > query.limit, 'more_results_possible': more,
                'provider_status': provider_status, 'local_warning': local_warning,
                'partial': bool(local_warning or provider_status['status'] == 'unavailable'),
                'is_demo': self.is_demo,
                'checked_at': local_iso(self.store.now()), 'timezone': 'Africa/Casablanca',
                'coverage_note': coverage,
                'duty_api_status': 'not_connected',
                'duty_coverage_note': 'Gardes limitées aux plannings datés vérifiés de notre annuaire. Aucun flux externe de garde validé actuellement.',
                'distance_note': 'Distance à vol d’oiseau, pas un temps de trajet.' if query.latitude is not None else
                    'Recherche par ville/quartier ; proximité non calculée sans position.'}

    @staticmethod
    def conversation_result(result):
        """Cards go to the UI. Do not retain Google descriptions in LLM history.

        Ordinals and registered partner identities preserve conversational follow-up.
        Own directory data remains available to the assistant in full.
        """
        if result.get('provider_status', {}).get('provider') != 'google_places':
            return result
        rows = []
        for index, p in enumerate(result['results'], 1):
            if p['source'] == 'local':
                rows.append({'card_number': index, **p})
            else:
                rows.append({'card_number': index, 'id': p['id'], 'name': p.get('registered_name'),
                    'accepting_requests': p['accepting_requests'], 'partner': p['partner'],
                    'duty_status': p['duty_status'], 'duty': p['duty'],
                    'opening_status': p['opening_status'], 'stock_status': 'unknown'})
        return {**{k: result[k] for k in ('provider_status', 'coverage_note', 'local_warning', 'partial',
                    'checked_at', 'duty_coverage_note', 'more_results_possible', 'truncated')},
                'results': rows,
                'instruction': 'Les fiches sont affichées dans l’interface. Réponds naturellement dans la langue du patient. '
                    'Utilise leur numéro pour les désigner. Seuls les partenaires accepting_requests=true peuvent recevoir '
                    'un brouillon. Les noms Google ne sont pas conservés dans la conversation : ne les invente pas.'}

    def active_pharmacist(self, account):
        row = self.store.pharmacy(account['pharmacy_id'])
        if not row or not row['active'] or not row['partner'] or bool(row['is_demo']) != self.is_demo:
            raise PharmacyError(403, 'Accès pharmacien désactivé pour cet environnement.')
        return row

    def prepare_nearest(self, account, medication_code, boxes=1, *, location=None, city=None, district=None, radius_km=10):
        """Rank the entire eligible partner registry, never the first page of a directory API."""
        rows,duties,accepting=self.store.directory()
        schedules=self.store.schedules()
        candidates=[]
        for row in rows:
            if row['id'] not in accepting or not row['partner'] or not row['active']:
                continue
            if city and normalize(city)!=row['city_key']:continue
            if district and normalize(district) not in row['district_key']:continue
            view=self.pharmacy_view(row,duties,True,schedules.get(row['id']))
            if view['opening_status']=='confirmed_closed':continue
            schedule=view['regular_hours']
            if schedule and not schedule['open_24h'] and view['opening_status']!='confirmed_open' and not view['duty']:
                now=datetime.fromtimestamp(self.store.now(),CASABLANCA)
                if now.weekday() not in schedule['days_of_week'] or not schedule['opens_at']<=now.strftime('%H:%M')<schedule['closes_at']:
                    continue
            distance=distance_km(location['latitude'],location['longitude'],row['latitude'],row['longitude']) if location else None
            if distance is not None and distance>radius_km:continue
            candidates.append((distance,view))
        if not candidates:
            return {'error':'no_partner','message':'Aucune pharmacie partenaire joignable n’a été trouvée pour cette zone et ces horaires. Aucune demande n’a été envoyée.'}
        if not location and (not city or len(candidates)>1):
            return {'error':'location_needed','message':'Demande la position ou un quartier plus précis pour choisir la pharmacie partenaire. Ne prétends pas connaître la plus proche sans position.'}
        candidates.sort(key=lambda pair:(pair[0] if pair[0] is not None else 0,pair[1]['id']))
        distance,p=candidates[0]
        from .models import DraftInput
        draft=self.draft(account,DraftInput(pharmacy_id=p['id'],medication_code=medication_code,boxes=boxes))
        return {'request':draft,'pharmacy':p,'distance_km':round(distance,2) if distance is not None else None,
            'selection_basis':'nearest_eligible_partner' if location else 'only_eligible_partner_in_area',
            'note':'Distance à vol d’oiseau parmi les partenaires acceptant des demandes, pas une disponibilité de stock ni une garantie de réponse.'}

    def import_directory(self, document):
        pharmacies, duties, schedules = [], [], []
        if len({str(p.id) for p in document.pharmacies}) != len(document.pharmacies):
            raise PharmacyError(422, 'Identifiant de pharmacie dupliqué dans le fichier.')
        if len({str(d.id) for d in document.duties}) != len(document.duties):
            raise PharmacyError(422, 'Identifiant de créneau dupliqué dans le fichier.')
        for pharmacy in document.pharmacies:
            row = pharmacy.model_dump(mode='json')
            hours=row.pop('regular_hours',None)
            if 'regular_hours' in pharmacy.model_fields_set:
                schedules.append({'pharmacy_id':row['id'],'hours':hours})
            if row['is_demo'] != self.is_demo:
                raise PharmacyError(422, 'Le fichier doit correspondre au mode demo/live configuré.')
            previous = self.store.pharmacy(row['id'])
            if previous and bool(previous['is_demo']) != row['is_demo']:
                raise PharmacyError(422, 'Une pharmacie fictive ne peut pas devenir réelle avec le même identifiant.')
            row['city_key'], row['district_key'] = normalize(row['city']), normalize(row['district'])
            for flag in ('partner','active','is_demo'):
                row[flag] = int(row[flag])
            pharmacies.append(row)
        for duty in document.duties:
            row = duty.model_dump(mode='json')
            for key in ('starts_at','ends_at','source_checked_at'):
                row[key] = epoch(row[key])
            if row['source_checked_at'] > self.store.now():
                raise PharmacyError(422, 'La date de vérification de la source ne peut pas être future.')
            row['cancelled'] = int(row['cancelled'])
            target = next((p for p in pharmacies if p['id'] == row['pharmacy_id']), None) or self.store.pharmacy(row['pharmacy_id'])
            if not target or bool(target['is_demo']) != self.is_demo:
                raise PharmacyError(422, 'Le créneau référence une pharmacie absente de cet environnement.')
            duties.append(row)
        self.store.import_directory(pharmacies, duties, schedules)
        return {'pharmacies': len(pharmacies), 'duty_periods': len(duties)}

    def draft(self, account, data):
        records = self.catalogue.get_medications([data.medication_code])['results']
        if not records:
            raise PharmacyError(404, 'Cette présentation ne figure pas dans le catalogue.')
        # Send only the requested product identity. No clinical history or insurance data.
        medication = {key: records[0].get(key) for key in ('code','nom','dci','dosage','unite_dosage','forme','presentation')}
        row = self.store.draft(account['owner_id'], data.pharmacy_id, medication, data.boxes)
        return self.request_view(row)

    def request_view(self, row, *, pharmacist=False):
        pharmacy = self.store.pharmacy(row['pharmacy_id'])
        result = {k: row[k] for k in ('id','pharmacy_id','medication_code','boxes','status','comment','patient_note')}
        result['medication'] = json.loads(row['medication_json'])
        for key in ('created_at','confirmed_at','updated_at','expires_at','response_at'):
            result[key] = local_iso(row[key])
        result.update(pharmacy_name=pharmacy['name'] if pharmacy else 'Pharmacie',
                      is_demo=bool(pharmacy and pharmacy['is_demo']),
                      requires_confirmation=row['status'] == 'draft',
                      stock_guaranteed=False, reservation=False)
        # owner_id, access tokens and complete chat history are never exposed.
        return result

    def list_requests(self, account, request_id=None):
        if account['role'] == 'pharmacist':
            self.active_pharmacist(account)
        return [self.request_view(r, pharmacist=account['role'] == 'pharmacist') for r in self.store.requests(account, request_id)]

    def change_request(self, account, request_id, action, comment=''):
        if account['role'] == 'pharmacist':
            self.active_pharmacist(account)
        return self.request_view(self.store.change_request(account, request_id, action, comment=comment))

    def pharmacist_profile(self, account):
        self.active_pharmacist(account)
        rows, duties, accepting = self.store.directory()
        row = next(p for p in rows if p['id'] == account['pharmacy_id'])
        return {'pharmacy': self.pharmacy_view(row, duties, row['id'] in accepting,self.store.schedules().get(row['id'])),
                'duties': [self.duty_view(d) for d in self.store.partner_duties(row['id'])]}

    def add_duty(self, account, data):
        self.active_pharmacist(account)
        start, end = epoch(data.starts_at), epoch(data.ends_at)
        if end <= self.store.now() or start > self.store.now()+90*86400:
            raise PharmacyError(422, 'Choisis un créneau à venir dans les 90 prochains jours.')
        row = dict(id=str(uuid4()), pharmacy_id=account['pharmacy_id'], starts_at=start, ends_at=end,
                   duty_type=data.duty_type, source_label='Déclaration de la pharmacie partenaire',
                   source_url=None, source_checked_at=self.store.now(), cancelled=0)
        self.store.add_partner_duty(account['pharmacy_id'], row)
        return self.duty_view(row)
