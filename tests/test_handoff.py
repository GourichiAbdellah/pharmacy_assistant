"""Human handoff isolation and truthful dashboard metrics, with local fixtures."""
from dataclasses import replace
import concurrent.futures
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from conftest import ScriptedModel, Speech, answer
from test_pharmacies import env, auth, draft, action
from pharmacy_next.main import create_app
from pharmacy_next.pharmacies.handoff import HumanMessage
from pharmacy_next.pharmacies.models import PharmacyError
from pharmacy_next.pharmacies.store import PharmacyStore


def start(e,patient=0):
    d=draft(e).json();action(e,d['id'],'confirm',{'confirmed':True})
    r=e['client'].post('/api/v1/availability/requests/'+d['id']+'/handoff',headers=auth(e['patients'][patient]),json={'confirmed':True})
    assert r.status_code==201,r.text
    return r.json()


def human(e,hid,path='',*,pharmacist=False,other=False,body=None,method='post'):
    prefix='pharmacist' if pharmacist else 'availability'
    token=e['tokens' if pharmacist else 'patients'][int(other)]
    return getattr(e['client'],method)(f'/api/v1/{prefix}/handoffs/{hid}'+path,headers=auth(token),**({'json':body} if body is not None else {}))


def test_relay_requires_explicit_consent_and_confirmed_product(env):
    e=env;d=draft(e).json();path='/api/v1/availability/requests/'+d['id']+'/handoff'
    for body in ({},{'confirmed':False},{'confirmed':1},{'confirmed':'true'}):
        assert e['client'].post(path,headers=auth(e['patients'][0]),json=body).status_code==422
    assert e['client'].post(path,headers=auth(e['patients'][0]),json={'confirmed':True}).status_code==409
    action(e,d['id'],'confirm',{'confirmed':True})
    assert e['client'].post(path,headers=auth(e['patients'][1]),json={'confirmed':True}).status_code==404
    h=e['client'].post(path,headers=auth(e['patients'][0]),json={'confirmed':True}).json()
    assert h['status']=='waiting' and h['medication']['code']==d['medication_code']
    assert not {'owner_id','organism','history','token'} & h.keys()
    assert e['client'].post(path,headers=auth(e['patients'][0]),json={'confirmed':True}).json()['id']==h['id']


def test_takeover_and_messages_are_scoped_and_plain_data(env):
    e=env;h=start(e);hid=h['id']
    assert human(e,hid,method='get',other=True).status_code==404
    assert human(e,hid,'/accept',pharmacist=True,other=True).status_code==404
    assert human(e,hid,'/accept').status_code==404 # no patient accept endpoint
    message={'text':'Bonjour <img src=x onerror=alert(1)> واش كاين؟','client_message_id':str(uuid4())}
    assert human(e,hid,'/messages',pharmacist=True,body=message).status_code==409
    assert human(e,hid,'/messages',body={**message,'sender_role':'pharmacist'}).status_code==422
    assert human(e,hid,'/messages',body=message).status_code==200
    assert human(e,hid,'/accept',pharmacist=True).json()['status']=='active'
    assert human(e,hid,'/accept',pharmacist=True).json()['status']=='active'
    response={'text':'Oui la boite de 12 est disponible.','client_message_id':str(uuid4())}
    assert human(e,hid,'/messages',pharmacist=True,body=response).status_code==200
    data=human(e,hid,method='get').json()
    assert [m['sender_role'] for m in data['messages']]==['system','patient','system','pharmacist']
    assert data['messages'][1]['body']==message['text']
    assert data['next_after']==4 and not data['has_more']
    assert human(e,hid+'?after=2',method='get').json()['messages'][0]['sequence_number']==3
    assert e['model'].inputs==[]


def test_message_retry_and_parallel_deduplication(env):
    e=env;h=start(e);account=e['store'].authenticate(e['patients'][0],'patient')
    data=HumanMessage(text='Une seule fois',client_message_id=uuid4())
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _:e['service'].handoffs.change(account,h['id'],'message',data),range(4)))
    messages=human(e,h['id'],method='get').json()['messages']
    assert [m['body'] for m in messages].count('Une seule fois')==1
    assert human(e,h['id'],'/messages',body={'text':'Différent','client_message_id':str(data.client_message_id)}).status_code==409


def test_one_relay_per_patient_access_and_close_reopen(env):
    e=env;h=start(e)
    d=draft(e,pharmacy_id=e['ids'][1]).json();action(e,d['id'],'confirm',{'confirmed':True})
    path='/api/v1/availability/requests/'+d['id']+'/handoff'
    assert e['client'].post(path,headers=auth(e['patients'][0]),json={'confirmed':True}).status_code==409
    assert human(e,h['id'],'/close').json()['status']=='closed'
    assert human(e,h['id'],'/close').json()['status']=='closed'
    assert human(e,h['id'],'/messages',body={'text':'Trop tard','client_message_id':str(uuid4())}).status_code==409
    new=e['client'].post(path,headers=auth(e['patients'][0]),json={'confirmed':True})
    assert new.status_code==201
    assert len(e['client'].get('/api/v1/availability/handoffs',headers=auth(e['patients'][0])).json()['handoffs'])==1


def test_direct_llm_calls_paused_and_human_messages_not_sent_to_model(env):
    e=env;h=start(e)
    assert e['client'].post('/api/v1/chat',headers=auth(e['patients'][0]),json={'message':'Prix ?'}).status_code==409
    assert e['model'].inputs==[]
    secret='MESSAGE HUMAIN NE PAS TRANSMETTRE AU MODELE'
    human(e,h['id'],'/messages',body={'text':secret,'client_message_id':str(uuid4())})
    human(e,h['id'],'/close')
    assert e['client'].post('/api/v1/chat',headers=auth(e['patients'][0]),json={'message':'Bonjour'}).status_code==200
    assert secret not in json.dumps(e['model'].inputs)


def test_waiting_expiry_and_partner_deactivation(env):
    e=env;h=start(e);e['clock'][0]+=1800
    assert human(e,h['id'],method='get').json()['handoff']['status']=='expired'
    assert human(e,h['id'],'/accept',pharmacist=True).status_code==409


def test_active_relay_expires_after_one_hour_and_cancellation_stops_it(env):
    e=env;h=start(e);human(e,h['id'],'/accept',pharmacist=True)
    e['clock'][0]+=1801 # stock request expiry does not silently end an accepted conversation
    assert human(e,h['id'],method='get').json()['handoff']['status']=='active'
    e['clock'][0]+=1800
    assert human(e,h['id'],method='get').json()['handoff']['status']=='expired'


def test_cancel_or_disabled_partner_ends_relay(env):
    e=env;h=start(e)
    action(e,h['request_id'],'cancel')
    assert human(e,h['id'],method='get').json()['handoff']['status']=='expired'
    d=draft(e,pharmacy_id=e['ids'][1]).json();action(e,d['id'],'confirm',{'confirmed':True})
    h=e['client'].post('/api/v1/availability/requests/'+d['id']+'/handoff',headers=auth(e['patients'][0]),json={'confirmed':True}).json()
    with e['store'].transaction(write=True) as tx:tx.execute('UPDATE assist_pharmacies SET active=0 WHERE id=?',(e['ids'][1],))
    assert human(e,h['id'],method='get').json()['handoff']['status']=='expired'


def test_dashboard_uses_own_confirmed_requests_and_first_human_message(env):
    e=env;c=e['client'];path='/api/v1/pharmacist/dashboard'
    assert c.get(path).status_code==401
    assert c.get(path,headers=auth(e['patients'][0])).status_code==401
    assert c.get(path,headers=auth(e['tokens'][0])).json()['requests_7d']==0
    h=start(e);e['clock'][0]+=30
    human(e,h['id'],'/accept',pharmacist=True);e['clock'][0]+=90
    human(e,h['id'],'/messages',pharmacist=True,body={'text':'Bonjour','client_message_id':str(uuid4())})
    d=c.get(path,headers=auth(e['tokens'][0])).json()
    assert d['requests_7d']==1 and d['active']==1 and d['waiting']==0
    assert sum(x['count'] for x in d['daily_requests'])==1
    assert d['mean_first_reply_seconds']==120 and d['first_reply_sample_count']==1
    assert d['top_medications'][0]['count']==1 and d['request_statuses']=={'pending':1}
    other=c.get(path,headers=auth(e['tokens'][1])).json()
    assert other['requests_7d']==0 and other['active']==0 and other['mean_first_reply_seconds'] is None


def test_persistence_role_scope_rate_limit_and_pagination(env):
    e=env;h=start(e);account=e['store'].authenticate(e['patients'][0],'patient')
    # Produce enough messages to cross one history page, within the accepted relay TTL.
    human(e,h['id'],'/accept',pharmacist=True)
    for i in range(202):
        if i%15==0:e['clock'][0]+=61
        e['service'].handoffs.change(account,h['id'],'message',HumanMessage(text=str(i),client_message_id=uuid4()))
    first=human(e,h['id'],method='get').json();second=human(e,h['id']+'?after='+str(first['next_after']),method='get').json()
    assert len(first['messages'])==200 and first['has_more'] and not second['has_more']
    assert not {m['id'] for m in first['messages']} & {m['id'] for m in second['messages']}
    restored=PharmacyStore(e['settings'],clock=lambda:e['clock'][0])
    app=create_app(e['settings'],e['repository'],ScriptedModel([answer('ok')]),Speech(),pharmacy_store=restored)
    with TestClient(app) as c:
        assert c.get('/api/v1/availability/handoffs',headers=auth(e['patients'][0])).json()['handoffs'][0]['id']==h['id']
    for i in range(20):
        result=human(e,h['id'],'/messages',body={'text':'test','client_message_id':str(uuid4())})
        if result.status_code==429:break
    assert result.status_code==429


def test_pharmacist_page_reports_disabled_module_instead_of_404(settings,repository):
    app=create_app(settings,repository,ScriptedModel([answer('ok')]),Speech())
    with TestClient(app) as c:
        assert c.get('/pharmacien').status_code==200
        assert c.get('/api/v1/pharmacies/config').json()['enabled'] is False
        assert c.get('/api/v1/pharmacist/dashboard').status_code==404


def test_handoff_started_during_generation_prevents_a_second_speaker(env):
    e=env;d=draft(e).json();action(e,d['id'],'confirm',{'confirmed':True})
    account=e['store'].authenticate(e['patients'][0],'patient')
    class ConcurrentModel:
        async def complete(self,messages,tools):
            e['service'].handoffs.start(account,d['id'])
            return answer('Réponse devenue obsolète.')
        async def close(self):pass
    app=create_app(e['settings'],e['repository'],ConcurrentModel(),Speech(),pharmacy_store=e['store'])
    with TestClient(app) as c:
        result=c.post('/api/v1/chat',headers=auth(e['patients'][0]),json={'message':'Bonjour'})
        assert result.status_code==409 and 'obsolète' not in result.text
