"""End-to-end API tests with fictitious pharmacies, local SQLite and no external calls."""
import concurrent.futures
from dataclasses import replace
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from pharmacy_next.catalogue import Catalogue
from pharmacy_next.main import create_app
from pharmacy_next.pharmacies.cli import demo_document
from pharmacy_next.pharmacies.models import (DirectoryImport, NearbyInput, PharmacyError, epoch,
    local_iso, DraftInput, DutyInput, ResponseInput)
from pharmacy_next.pharmacies.service import PharmacyService
from pharmacy_next.pharmacies.store import PharmacyStore, Transaction
from pharmacy_next.sessions import Context
from pharmacy_next.tools import ToolRunner, PHARMACY_TOOLS
from conftest import ScriptedModel, Speech, answer, calls


CODE = '0000000000012'


@pytest.fixture
def env(tmp_path, settings, repository):
    settings = replace(settings, pharmacy_backend='sqlite', pharmacy_sqlite_path=str(tmp_path/'directory.sqlite3'))
    clock = [epoch('2026-10-23T23:30:00+01:00')]
    store = PharmacyStore(settings, clock=lambda:clock[0]); store.initialize()
    service = PharmacyService(store, Catalogue(repository, 300))
    document = demo_document(clock[0]); service.import_directory(document)
    ids = [str(p.id) for p in document.pharmacies]
    tokens = [store.issue_access('pharmacist', p)['token'] for p in ids[:2]]
    model = ScriptedModel([answer('Réponse de test.')]*10)
    app = create_app(settings, repository, model, Speech(), pharmacy_store=store)
    with TestClient(app) as client:
        patients = [client.post('/api/v1/availability/identity').json()['token'] for _ in range(2)]
        yield dict(settings=settings, clock=clock, store=store, service=service, ids=ids,
            tokens=tokens, patients=patients, client=client, app=app, repository=repository, model=model)


def auth(token): return {'Authorization':'Bearer '+token}

def draft(e, **kwargs):
    return e['client'].post('/api/v1/availability/drafts', headers=auth(e['patients'][0]),
        json={**dict(pharmacy_id=e['ids'][0], medication_code=CODE, boxes=1), **kwargs})

def action(e, rid, name, body=None, pharmacist=False, other=False):
    token=e['tokens' if pharmacist else 'patients'][int(other)]
    return e['client'].post(f'/api/v1/{"pharmacist" if pharmacist else "availability"}/requests/{rid}/{name}',
        headers=auth(token), json=body)


def test_directory_garde_is_not_opening_or_stock(env):
    r=env['client'].post('/api/v1/pharmacies/search',json={'city':'rabat','on_duty_only':True})
    assert r.status_code==200
    rows=r.json()['results']; assert len(rows)==1
    p=rows[0]; assert p['duty_status']=='scheduled_now'
    assert p['opening_status']=='unknown' and p['stock_status']=='unknown'
    assert p['is_demo'] and p['maps_url'] is None
    assert p['duty']['ends_at']>p['duty']['starts_at']
    assert r.headers['cache-control']=='no-store'


def test_radius_city_and_opening_filters(env):
    c=env['client']
    rows=c.post('/api/v1/pharmacies/search',json={'latitude':34.0209,'longitude':-6.8416,'radius_km':.1}).json()['results']
    assert [r['id'] for r in rows]==[env['ids'][0]]
    assert c.post('/api/v1/pharmacies/search',json={'city':'Paris'}).json()['results']==[]
    assert c.post('/api/v1/pharmacies/search',json={'city':'Rabat','open_confirmed_only':True}).json()['results']==[]
    c.post('/api/v1/pharmacist/opening',headers=auth(env['tokens'][0]),json={'is_open':True})
    assert len(c.post('/api/v1/pharmacies/search',json={'city':'Rabat','open_confirmed_only':True}).json()['results'])==1
    env['clock'][0]+=3600
    assert c.post('/api/v1/pharmacies/search',json={'city':'Rabat','open_confirmed_only':True}).json()['results']==[]


@pytest.mark.parametrize('query',[{}, {'latitude':10}, {'city':'Rabat','latitude':'nan','longitude':0}, {'city':'Rabat','radius_km':0}, {'longitude':181,'latitude':0}])
def test_bad_location_rejected(env,query):
    assert env['client'].post('/api/v1/pharmacies/search',json=query).status_code==422


def test_stale_schedule_and_half_open_boundary(env):
    with env['store'].transaction(write=True) as tx:
        tx.execute('UPDATE assist_duties SET source_checked_at=? WHERE pharmacy_id=?',(env['clock'][0]-73*3600,env['ids'][0]))
    rows=env['service'].search(NearbyInput(city='Rabat'))['results']
    p=next(p for p in rows if p['id']==env['ids'][0]);assert p['duty_status']=='unconfirmed'
    assert 'ancien' in p['duty_note']
    env['clock'][0]+=23*3600
    assert env['service'].search(NearbyInput(city='Rabat',on_duty_only=True))['results']==[]


@pytest.mark.parametrize('value',['2026-02-15T02:30:00','2026-03-22T02:30:00'])
def test_casablanca_ambiguous_and_nonexistent_dates_rejected(value):
    with pytest.raises(ValueError): epoch(value)


def test_local_midnight_and_explicit_offset():
    assert epoch('2026-10-23T23:00:00')==epoch('2026-10-23T22:00:00Z')
    assert epoch('2026-10-24T01:00:00')-epoch('2026-10-23T23:00:00')==7200
    assert epoch('2026-02-15T02:30:00+01:00')!=epoch('2026-02-15T02:30:00+00:00')


def test_draft_confirmation_scoped_inbox_and_patient_reply(env):
    c=env['client']; r=draft(env);assert r.status_code==201
    data=r.json();rid=data['id'];assert data['status']=='draft'
    assert set(data['medication'])=={'code','nom','dci','dosage','unite_dosage','forme','presentation'}
    assert 'owner_id' not in data
    assert c.get('/api/v1/pharmacist/requests',headers=auth(env['tokens'][0])).json()['requests']==[]
    assert action(env,rid,'response',{'status':'available'},pharmacist=True).status_code==404
    assert action(env,rid,'confirm',{'confirmed':True}).json()['status']=='pending'
    assert len(c.get('/api/v1/pharmacist/requests',headers=auth(env['tokens'][0])).json()['requests'])==1
    assert c.get('/api/v1/pharmacist/requests',headers=auth(env['tokens'][1])).json()['requests']==[]
    assert action(env,rid,'response',{'status':'available'},pharmacist=True,other=True).status_code==404
    response=action(env,rid,'response',{'status':'available','comment':'Vérifié au comptoir.'},pharmacist=True)
    assert response.status_code==200 and response.json()['response_at']
    assert response.json()['reservation'] is False
    patient=c.get('/api/v1/availability/requests/'+rid,headers=auth(env['patients'][0])).json()
    assert patient['status']=='available'
    assert action(env,rid,'confirm',{'confirmed':True}).json()['status']=='available'


@pytest.mark.parametrize('body',[{}, {'confirmed':False}, {'confirmed':1}, {'confirmed':'true'}, {'confirmed':True,'medication_code':'other'}])
def test_confirmation_cannot_be_implicit_or_mutate_product(env,body):
    rid=draft(env).json()['id'];assert action(env,rid,'confirm',body).status_code==422


def test_ownership_roles_and_capability_hashes(env):
    c=env['client'];rid=draft(env).json()['id']
    assert c.get('/api/v1/availability/requests').status_code==401
    assert c.get('/api/v1/availability/requests',headers=auth(env['tokens'][0])).status_code==401
    assert c.get('/api/v1/pharmacist/profile',headers=auth(env['patients'][0])).status_code==401
    assert c.get('/api/v1/availability/requests/'+rid,headers=auth(env['patients'][1])).status_code==404
    assert action(env,rid,'confirm',{'confirmed':True},other=True).status_code==404
    raw=open(env['settings'].pharmacy_sqlite_path,'rb').read()
    for token in env['tokens']+env['patients']:assert token.encode() not in raw


def test_rotation_revokes_previous_pharmacist(env):
    new=env['store'].issue_access('pharmacist',env['ids'][0])['token']
    assert env['client'].get('/api/v1/pharmacist/profile',headers=auth(env['tokens'][0])).status_code==401
    assert env['client'].get('/api/v1/pharmacist/profile',headers=auth(new)).status_code==200


def test_deduplication_parallel_and_restart(env):
    account=env['store'].authenticate(env['patients'][0],'patient')
    data=DraftInput(pharmacy_id=env['ids'][0],medication_code=CODE)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        ids=list(pool.map(lambda _:env['service'].draft(account,data)['id'],range(4)))
    assert len(set(ids))==1
    store=PharmacyStore(env['settings'],clock=lambda:env['clock'][0])
    restored=PharmacyService(store,env['service'].catalogue)
    assert restored.list_requests(store.authenticate(env['patients'][0],'patient'))[0]['id']==ids[0]


def test_pending_timeout_and_draft_timeout(env):
    rid=draft(env).json()['id'];env['clock'][0]+=600
    assert action(env,rid,'confirm',{'confirmed':True}).status_code==409
    rid2=draft(env).json()['id'];assert rid2!=rid
    action(env,rid2,'confirm',{'confirmed':True});env['clock'][0]+=1800
    assert action(env,rid2,'response',{'status':'available'},pharmacist=True).status_code==409
    assert env['client'].get('/api/v1/availability/requests/'+rid2,headers=auth(env['patients'][0])).json()['status']=='expired'


def test_clarification_and_cancel_prevent_late_response(env):
    rid=draft(env).json()['id'];action(env,rid,'confirm',{'confirmed':True})
    assert action(env,rid,'response',{'status':'needs_details'},pharmacist=True).status_code==422
    assert action(env,rid,'response',{'status':'needs_details','comment':'Combien de boîtes ?'},pharmacist=True).json()['status']=='needs_details'
    assert action(env,rid,'clarify',{'text':'Une boîte.'}).json()['status']=='pending'
    assert action(env,rid,'cancel').json()['status']=='cancelled'
    assert action(env,rid,'response',{'status':'available'},pharmacist=True).status_code==409
    assert action(env,rid,'confirm',{'confirmed':True}).json()['status']=='cancelled'


def test_nonpartner_unknown_medication_and_invalid_quantity(env):
    c=env['client'];h=auth(env['patients'][0])
    for pid,code,boxes,expected in [(env['ids'][2],CODE,1,409),(env['ids'][0],'invented',1,404),(env['ids'][0],CODE,0,422),(env['ids'][0],CODE,1.5,422)]:
        assert c.post('/api/v1/availability/drafts',headers=h,json=dict(pharmacy_id=pid,medication_code=code,boxes=boxes)).status_code==expected


def test_partner_disable_between_preview_and_confirmation(env):
    rid=draft(env).json()['id']
    with env['store'].transaction(write=True) as tx:tx.execute('UPDATE assist_pharmacies SET active=0 WHERE id=?',(env['ids'][0],))
    assert action(env,rid,'confirm',{'confirmed':True}).status_code==409
    assert env['client'].get('/api/v1/pharmacist/profile',headers=auth(env['tokens'][0])).status_code==403


def test_live_demo_isolation(env):
    rid=draft(env).json()['id'];account=env['store'].authenticate(env['patients'][0],'patient')
    live=PharmacyService(PharmacyStore(replace(env['settings'],pharmacy_environment='live'),clock=lambda:env['clock'][0]),env['service'].catalogue)
    assert live.search(NearbyInput(city='Rabat'))['results']==[]
    assert live.list_requests(account)==[]
    with pytest.raises(PharmacyError):live.change_request(account,rid,'confirm')
    doc=demo_document(env['clock'][0]).model_dump(mode='json');doc['pharmacies'][0]['is_demo']=False
    with pytest.raises(PharmacyError):env['service'].import_directory(DirectoryImport.model_validate(doc))


def test_duties_overlap_cancel_and_foreign_scope(env):
    c=env['client'];start=env['clock'][0]+2*86400
    payload=dict(starts_at=local_iso(start),ends_at=local_iso(start+10*3600),duty_type='night')
    response=c.post('/api/v1/pharmacist/duties',headers=auth(env['tokens'][0]),json=payload);assert response.status_code==201
    did=response.json()['id']
    assert c.post('/api/v1/pharmacist/duties',headers=auth(env['tokens'][0]),json=payload).status_code==409
    assert c.delete('/api/v1/pharmacist/duties/'+did,headers=auth(env['tokens'][1])).status_code==404
    assert c.delete('/api/v1/pharmacist/duties/'+did,headers=auth(env['tokens'][0])).status_code==204
    assert c.post('/api/v1/pharmacist/duties',headers=auth(env['tokens'][0]),json=payload).status_code==201


def test_import_is_atomic_and_no_future_verification(env):
    doc=demo_document(env['clock'][0]).model_dump(mode='json')
    doc['pharmacies'][0]['name']='Nom altéré'
    doc['duties'][0]['source_checked_at']=local_iso(env['clock'][0]+10)
    with pytest.raises(PharmacyError):env['service'].import_directory(DirectoryImport.model_validate(doc))
    assert env['store'].pharmacy(env['ids'][0])['name']!='Nom altéré'


def test_tool_prepares_only_and_protects_uncertain_photo(env):
    context=Context();account=env['store'].authenticate(env['patients'][0],'patient')
    runner=ToolRunner(env['service'].catalogue,context,pharmacies=env['service'],patient=account)
    args=json.dumps(dict(pharmacy_id=env['ids'][0],medication_code=CODE))
    assert runner.execute('prepare_availability_request',args)['error']=='medication_not_retrieved'
    runner.execute('get_medication_details',json.dumps({'codes':[CODE]}))
    runner.selection_requires_confirmation=True
    assert runner.execute('prepare_availability_request',args)['error']=='photo_needs_confirmation'
    runner.selection_requires_confirmation=False
    assert runner.execute('prepare_availability_request',args)['status']=='draft'
    assert runner.execute('confirm_availability_request','{}')['error']=='unknown_tool'
    assert all(r['status']=='draft' for r in env['service'].list_requests(account))
    runner.patient=None
    assert runner.execute('get_availability_requests','{}')['error']=='patient_access_needed'


def test_tool_requires_city_or_trusted_location_and_disabled_schema(env):
    plain=ToolRunner(env['service'].catalogue,Context())
    assert not ({s['function']['name'] for s in plain.schemas}&PHARMACY_TOOLS)
    runner=ToolRunner(env['service'].catalogue,Context(),pharmacies=env['service'])
    assert runner.execute('search_pharmacies','{}')['error']=='location_needed'
    assert runner.execute('search_pharmacies','{"latitude":34,"longitude":-6}')['error']=='invalid_arguments'
    runner.location={'latitude':34.0209,'longitude':-6.8416}
    assert runner.execute('search_pharmacies','{}')['results']


def test_chat_actions_and_session_ownership(env):
    model=ScriptedModel([calls(('get_medication_details',{'codes':[CODE]})),calls(('remember_context',{'selected_codes':[CODE]})),calls(('prepare_availability_request',{'pharmacy_id':env['ids'][0],'medication_code':CODE})),answer('Vérifie le récapitulatif puis confirme pour l’envoyer.')])
    env['app'].state.chat_service.model=model
    c=env['client'];r=c.post('/api/v1/chat',headers=auth(env['patients'][0]),json={'message':'Prépare la demande pour une boîte de 12.'})
    assert r.status_code==200
    data=r.json();assert data['pharmacy_actions'][0]['data']['status']=='draft'
    assert data['selected_presentations'][0]['code']==CODE
    sid=data['session_id']
    assert c.post('/api/v1/chat',json={'message':'la suite','session_id':sid}).status_code==403
    assert c.delete('/api/v1/sessions/'+sid,headers=auth(env['patients'][1])).status_code==403
    assert c.delete('/api/v1/sessions/'+sid,headers=auth(env['patients'][0])).status_code==204
    assert not any(env['patients'][0] in str(m) for m in model.inputs)


def test_sqlserver_lock_and_parameters():
    class Cursor:
        description=[('owner_id',)]; rowcount=1
        def __init__(self):self.executed=[]
        def execute(self,*args):self.executed.append(args)
        def fetchall(self):return [('id',)]
    cursor=Cursor();tx=Transaction(cursor,'azure_sql');tx.lock_patient("x' OR 1=1")
    sql,args=cursor.executed[-1];assert 'UPDLOCK' in sql and 'HOLDLOCK' in sql
    assert "x' OR 1=1" not in sql and args==("x' OR 1=1",)


def test_request_quota_and_identity_limit(env):
    for i in range(1,11):assert draft(env, boxes=i).status_code==201
    r=env['client'].post('/api/v1/availability/drafts',headers=auth(env['patients'][0]),json=dict(pharmacy_id=env['ids'][1],medication_code=CODE,boxes=1))
    assert r.status_code==429
    for _ in range(28):assert env['client'].post('/api/v1/availability/identity').status_code==201
    assert env['client'].post('/api/v1/availability/identity').status_code==429
