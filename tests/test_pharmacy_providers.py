"""Offline contract tests: synthetic provider payloads, never actual pharmacies."""
from dataclasses import replace
import json
from pathlib import Path
from urllib.error import HTTPError
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import ScriptedModel, Speech, calls, answer
from pharmacy_next.catalogue import Catalogue
from pharmacy_next.main import create_app
from pharmacy_next.pharmacies.cli import demo_document
from pharmacy_next.pharmacies.duty_probe import probe_lematin
from pharmacy_next.pharmacies.models import DirectoryImport, NearbyInput, PharmacyError, epoch
from pharmacy_next.pharmacies.providers import GooglePlacesClient, ProviderUnavailable, RequestBudget
from pharmacy_next.pharmacies.service import PharmacyService
from pharmacy_next.pharmacies.store import PharmacyStore
from pharmacy_next.sessions import Context
from pharmacy_next.tools import ToolRunner


def place(identifier='ChIJ_test_only_1', **changes):
    return {**dict(id=identifier, displayName={'text':'GOOGLE SYNTHETIC NAME'},
        formattedAddress='SYNTHETIC ADDRESS NEVER STORE', location={'latitude':34.0209,'longitude':-6.8416},
        types=['pharmacy','health'], businessStatus='OPERATIONAL',
        addressComponents=[{'types':['country'],'shortText':'MA','longText':'Maroc'},
                           {'types':['locality'],'shortText':'Rabat','longText':'Rabat'}],
        attributions=[{'provider':'Synthetic provider','providerUri':'https://example.org/source'},
                      {'provider':'Unsafe URL','providerUri':'javascript:alert(1)'}]), **changes}


def client(settings, response=None, handler=None):
    requests = []
    def transport(req):
        requests.append(req)
        return handler(req) if handler else httpx.Response(200,json=response if response is not None else {'places':[place()]})
    return GooglePlacesClient(replace(settings,google_places_api_key='secret-test-never-real'),
                              transport=httpx.MockTransport(transport)), requests


@pytest.fixture
def live(tmp_path, settings, repository):
    s = replace(settings, pharmacy_backend='sqlite', pharmacy_environment='live',
                pharmacy_directory_provider='google_places', google_places_api_key='secret-test-never-real',
                pharmacy_sqlite_path=str(tmp_path/'live.sqlite3'))
    now = epoch('2026-10-25T23:30:00+01:00')
    store = PharmacyStore(s,clock=lambda:now);store.initialize()
    google, requests = client(s)
    service = PharmacyService(store,Catalogue(repository,300),directory_provider=google)
    document = demo_document(now).model_dump(mode='json')
    for p in document['pharmacies']:
        p.update(id=str(uuid4()),is_demo=False,name='Enrolled synthetic pharmacy')
    document['duties'][0]['pharmacy_id']=document['pharmacies'][0]['id']
    document['duties'][1]['pharmacy_id']=document['pharmacies'][1]['id']
    service.import_directory(DirectoryImport.model_validate(document))
    ids = [p['id'] for p in document['pharmacies']]
    token=store.issue_access('pharmacist',ids[0])['token']
    return dict(settings=s,store=store,service=service,ids=ids,token=token,requests=requests,repository=repository)


def test_nearby_contract_minimal_fields_country_and_no_medical_data(settings):
    google, requests = client(settings)
    result=google.search(NearbyInput(latitude=34.0209,longitude=-6.8416))
    assert len(requests)==1
    req=requests[0];body=json.loads(req.content)
    assert str(req.url)=='https://places.googleapis.com/v1/places:searchNearby'
    assert body['includedTypes']==['pharmacy'] and body['rankPreference']=='DISTANCE'
    assert body['locationRestriction']['circle']['radius']==10000
    assert req.headers['x-goog-api-key']=='secret-test-never-real'
    assert '*' not in req.headers['x-goog-fieldmask']
    assert 'currentOpeningHours' not in req.headers['x-goog-fieldmask']
    assert not {'medication','message','patient','organism'} & body.keys()
    p=result['results'][0]
    assert p['id'] is None and not p['accepting_requests'] and not p['partner']
    assert p['duty_status']=='unconfirmed' and p['opening_status']=='unknown' and p['stock_status']=='unknown'
    assert p['distance_km']==0 and p['attributions'][1]['url'] is None
    assert 'destination_place_id=ChIJ_test_only_1' in p['maps_url']


def test_text_search_arabic_and_pagination_bound(settings):
    google, requests = client(settings,{'places':[place()],'nextPageToken':'do-not-follow'})
    result=google.search(NearbyInput(city='الرباط',district='أكدال',limit=30))
    body=json.loads(requests[0].content)
    assert str(requests[0].url).endswith('places:searchText')
    assert body['textQuery']=='pharmacie أكدال, الرباط, Maroc'
    assert body['pageSize']==20 and body['strictTypeFiltering'] is True
    assert result['more_results_possible'] and len(requests)==1
    assert result['results'][0]['distance_km'] is None


def test_text_position_bias_does_not_bypass_radius_filter(settings):
    google, reqs=client(settings,{'places':[place(),place('ChIJ_far',location={'latitude':33.57,'longitude':-7.59})]})
    result=google.search(NearbyInput(city='Rabat',latitude=34.0209,longitude=-6.8416,radius_km=1))
    assert 'locationBias' in json.loads(reqs[0].content)
    assert len(result['results'])==1 and result['discarded_results']==1


@pytest.mark.parametrize('change',[
    {'businessStatus':'CLOSED_PERMANENTLY'}, {'businessStatus':'CLOSED_TEMPORARILY'},
    {'types':['doctor']}, {'addressComponents':[]},
    {'addressComponents':[{'types':['country'],'shortText':'FR'}]},
    {'location':{'latitude':True,'longitude':0}}, {'location':{'latitude':91,'longitude':0}},
    {'id':'../../path'}, {'displayName':{}}, {'formattedAddress':None},
])
def test_unusable_or_wrong_country_results_rejected(settings,change):
    google,_=client(settings,{'places':[place(**change)]})
    assert google.search(NearbyInput(city='Rabat'))['results']==[]


@pytest.mark.parametrize('status,code',[(401,'access_denied'),(403,'access_denied'),(429,'upstream_limit'),(500,'upstream_unavailable'),(302,'upstream_unavailable')])
def test_http_errors_sanitized_no_redirect_no_retry(settings,status,code):
    google,requests=client(settings,handler=lambda req:httpx.Response(status,headers={'Location':'https://example.org'},text='secret-test-never-real'))
    with pytest.raises(ProviderUnavailable) as err:google.search(NearbyInput(city='Rabat'))
    assert err.value.code==code and len(requests)==1
    assert 'secret-test' not in str(err.value)


def test_timeout_malformed_empty_and_oversized_response(settings):
    def timeout(req):raise httpx.ReadTimeout('secret-test-never-real',request=req)
    google,_=client(settings,handler=timeout)
    with pytest.raises(ProviderUnavailable,match='délai'):google.search(NearbyInput(city='Rabat'))
    for response in [httpx.Response(200,text='<html>error</html>'),httpx.Response(200,text='x'*1_000_001),httpx.Response(200,json={'places':None})]:
        google,_=client(settings,handler=lambda req:response)
        with pytest.raises(ProviderUnavailable):google.search(NearbyInput(city='Rabat'))
    google,_=client(settings,{})
    assert google.search(NearbyInput(city='Rabat'))['results']==[]


def test_no_key_no_network_and_bounded_radius(settings):
    google=GooglePlacesClient(settings,transport=httpx.MockTransport(lambda _:pytest.fail('network')))
    with pytest.raises(ProviderUnavailable) as err:google.search(NearbyInput(city='Rabat'))
    assert err.value.code=='not_configured'
    with pytest.raises(PharmacyError):google.search(NearbyInput(latitude=34,longitude=-6,radius_km=51))


def test_process_budget_minute_day_boundary():
    now=[epoch('2026-10-25T23:00:00+01:00')]
    b=RequestBudget(1,2,lambda:now[0]);b.take()
    with pytest.raises(ProviderUnavailable):b.take()
    now[0]+=60;b.take();now[0]+=60
    with pytest.raises(ProviderUnavailable):b.take()
    now[0]+=86400;b.take()


def test_external_match_never_enrolls_and_never_copies_google_content(live):
    e=live; s=e['service']
    rows=s.search(NearbyInput(city='Rabat'))['results']
    external=next(p for p in rows if p['source']=='google_places')
    assert external['id'] is None and not external['accepting_requests']
    e['store'].link_google(e['ids'][0],'ChIJ_test_only_1')
    # init is also an idempotent migration from the previous demo module.
    e['store'].initialize()
    result=s.search(NearbyInput(city='Rabat'));rows=result['results']
    external=next(p for p in rows if p['source']=='google_places')
    assert external['id']==e['ids'][0] and external['accepting_requests']
    assert external['duty_status']=='scheduled_now' and external['opening_status']=='unknown'
    assert len([p for p in rows if p['id']==e['ids'][0]])==1
    with e['store'].transaction() as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM assist_pharmacies')['n']==3
    raw=Path(e['settings'].pharmacy_sqlite_path).read_bytes()
    assert b'GOOGLE SYNTHETIC NAME' not in raw and b'SYNTHETIC ADDRESS NEVER STORE' not in raw
    assert b'ChIJ_test_only_1' in raw
    e['store'].unlink_google(e['ids'][0]);assert not e['store'].external_links()


def test_mapping_conflicts_inactive_nonpartner_and_demo(live,settings,tmp_path):
    e=live;e['store'].link_google(e['ids'][0],'ChIJ_test_only_1')
    with pytest.raises(PharmacyError,match='autre'):e['store'].link_google(e['ids'][1],'ChIJ_test_only_1')
    with pytest.raises(PharmacyError):e['store'].link_google(uuid4(),'ChIJ_new')
    e['store'].unlink_google(e['ids'][0]);e['store'].link_google(e['ids'][2],'ChIJ_test_only_1')
    p=next(p for p in e['service'].search(NearbyInput(city='Rabat'))['results'] if p['source']=='google_places')
    assert not p['partner'] and not p['accepting_requests']
    with e['store'].transaction(write=True) as tx:tx.execute('UPDATE assist_pharmacies SET active=0 WHERE id=?',(e['ids'][2],))
    assert not e['store'].external_links()
    demo=PharmacyStore(replace(settings,pharmacy_backend='sqlite',pharmacy_directory_provider='google_places',pharmacy_sqlite_path=str(tmp_path/'demo.db')))
    with pytest.raises(ValueError):PharmacyService(demo,None)


def test_garde_and_opening_filter_only_verified_registry_no_paid_search(live):
    s=live['service']
    for query,count in [(NearbyInput(city='Rabat',on_duty_only=True),1),(NearbyInput(city='Rabat',open_confirmed_only=True),0)]:
        result=s.search(query)
        assert len(result['results'])==count
        assert result['provider_status']['status']=='not_requested'
        assert result['duty_api_status']=='not_connected'
    assert live['requests']==[]


def test_provider_outage_returns_honest_partial_local_results(live):
    live['service'].external,_=client(live['settings'],handler=lambda req:httpx.Response(403,text='private-key'))
    result=live['service'].search(NearbyInput(city='Rabat'))
    assert result['partial'] and result['provider_status']['code']=='access_denied'
    assert len(result['results'])==3 and all(p['source']=='local' for p in result['results'])
    assert 'private-key' not in json.dumps(result)


def test_local_failure_does_not_make_google_results_partners(live,monkeypatch):
    def fail():raise PharmacyError(503,'database unavailable')
    monkeypatch.setattr(live['store'],'directory',fail)
    result=live['service'].search(NearbyInput(city='Rabat'))
    assert result['partial'] and result['local_warning']
    assert len(result['results'])==1 and result['results'][0]['id'] is None


def test_chat_api_cards_do_not_persist_google_payload_or_keys(live):
    e=live;e['store'].link_google(e['ids'][0],'ChIJ_test_only_1')
    model=ScriptedModel([calls(('search_pharmacies',{'city':'Rabat'})),answer('Les fiches sont affichées ci-dessous.'),answer('Je peux préparer une demande auprès du partenaire.')])
    app=create_app(e['settings'],e['repository'],model,Speech(),pharmacy_store=e['store'])
    app.state.pharmacies.external=e['service'].external
    with TestClient(app) as c:
        result=c.post('/api/v1/chat',json={'message':'Une pharmacie proche de Rabat ?'})
        assert result.status_code==200
        data=result.json()
        assert 'GOOGLE SYNTHETIC NAME' in json.dumps(data['pharmacy_actions'])
        assert 'secret-test' not in json.dumps(data)
        c.post('/api/v1/chat',json={'message':'La dernière fiche','session_id':data['session_id']})
        history=json.dumps(model.inputs)
        assert 'GOOGLE SYNTHETIC NAME' not in history and 'SYNTHETIC ADDRESS NEVER STORE' not in history
        assert 'ChIJ_test_only_1' not in history and 'Enrolled synthetic pharmacy' in history
        assert result.headers['cache-control']=='no-store'


def test_tool_only_returns_partner_identity_and_no_automatic_send(live):
    e=live;e['store'].link_google(e['ids'][0],'ChIJ_test_only_1')
    runner=ToolRunner(e['service'].catalogue,Context(),pharmacies=e['service'])
    result=runner.execute('search_pharmacies',json.dumps({'city':'Rabat'}))
    assert any(r['id']==e['ids'][0] and r['accepting_requests'] for r in result['results'])
    assert 'GOOGLE SYNTHETIC NAME' not in json.dumps(result)
    with e['store'].transaction() as tx:assert tx.one('SELECT COUNT(*) AS n FROM assist_requests')['n']==0


class ProbeResponse:
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,limit):return json.dumps({'pharmacies':[{'nom':'PRIVATE NAME','debut':'2026-10-25'}]}).encode()[:limit]


def test_lematin_probe_is_only_a_schema_diagnostic(settings):
    settings=replace(settings,lematin_api_key='secret/key',lematin_oauth_token='private-oauth')
    requests=[]
    class Opener:
        def open(self,request,timeout):requests.append(request);return ProbeResponse()
    result=probe_lematin(settings,'2026-10-25','nuit',opener=Opener())
    assert '/secret%2Fkey/pharmacies?garde=nuit&date=2026-10-25' in requests[0].full_url
    assert requests[0].get_header('Authorization')=='Bearer private-oauth'
    assert not result['contract_verified'] and result['imported_duties']==0
    assert 'PRIVATE NAME' not in json.dumps(result) and 'secret' not in json.dumps(result)
    class Denied:
        def open(self,request,timeout):raise HTTPError(request.full_url,403,'secret/key',{},None)
    with pytest.raises(PharmacyError) as err:probe_lematin(settings,'2026-10-25','nuit',opener=Denied())
    assert 'secret/key' not in str(err.value)


def test_missing_lematin_key_never_probes(settings):
    with pytest.raises(PharmacyError,match='LEMATIN'):
        probe_lematin(settings,'2026-10-25','nuit')
