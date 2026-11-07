"""Availability preparation from chat, with no product form or pharmacy UUID entry."""
from datetime import datetime, timedelta
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from test_pharmacies import env, auth, CODE
from pharmacy_next.pharmacies.cli import demo_document
from pharmacy_next.pharmacies.models import CASABLANCA, DirectoryImport, DutyInput, RegularHours, epoch
from pharmacy_next.sessions import Context
from pharmacy_next.tools import ToolRunner


def runner(e,location=None,selected=True,uncertain=False):
    context=Context();rows=e['service'].catalogue.get_medications([CODE])['results'];context.remember_records(rows)
    if selected:context.selected_codes=[CODE]
    patient=e['store'].authenticate(e['patients'][0],'patient')
    return ToolRunner(e['service'].catalogue,context,pharmacies=e['service'],patient=patient,location=location,selection_requires_confirmation=uncertain)


def test_assistant_prepares_nearest_without_a_user_form(env):
    e=env;r=runner(e,{'latitude':34.0015,'longitude':-6.8537})
    result=r.execute('prepare_nearest_availability_request','{}')
    assert result['request']['pharmacy_id']==e['ids'][1] and result['distance_km']==0
    assert result['request']['medication']['code']==CODE and result['request']['boxes']==1
    assert result['request']['status']=='draft' and result['selection_basis']=='nearest_eligible_partner'
    assert r.ui_actions[0]['type']=='draft'
    retry=r.execute('prepare_availability_request',json.dumps({'medication_code':CODE,'pharmacy_id':e['ids'][0]}))
    assert retry['id']==result['request']['id'] and len(r.ui_actions)==1
    assert e['client'].get('/api/v1/pharmacist/requests',headers=auth(e['tokens'][1])).json()['requests']==[]


def test_gps_ranking_covers_all_partners_not_a_limited_api_page(env):
    e=env;doc=demo_document(e['clock'][0]).model_dump(mode='json');base=doc['pharmacies'][0]
    extras=[]
    for i in range(15):
        p={**base,'id':str(uuid4()),'name':'FICTIVE non partenaire '+str(i),'partner':False,
           'latitude':34.0015+i*.000001,'longitude':-6.8537}
        extras.append(p)
    e['service'].import_directory(DirectoryImport.model_validate({'pharmacies':extras}))
    r=runner(e,{'latitude':34.0015,'longitude':-6.8537})
    result=r.execute('prepare_nearest_availability_request','{}')
    assert result['request']['pharmacy_id']==e['ids'][1]


def test_location_or_natural_clarification_instead_of_a_form(env):
    r=runner(env)
    result=r.execute('prepare_nearest_availability_request',json.dumps({'city':'Rabat'}))
    assert result['error']=='location_needed' and r.ui_actions==[{'type':'location_request'}]
    result=r.execute('prepare_nearest_availability_request',json.dumps({'city':'Rabat','district':'Agdal'}))
    assert result['request']['pharmacy_id']==env['ids'][1]
    assert result['selection_basis']=='only_eligible_partner_in_area' and result['distance_km'] is None
    r=runner(env,selected=False)
    assert r.execute('prepare_nearest_availability_request','{}')['error']=='presentation_needed'
    r=runner(env,{'latitude':34.0015,'longitude':-6.8537},uncertain=True)
    assert r.execute('prepare_nearest_availability_request','{}')['error']=='photo_needs_confirmation'


def test_closed_or_nonaccepting_partner_is_not_selected(env):
    e=env
    e['store'].opening(e['ids'][1],False)
    result=runner(e,{'latitude':34.0015,'longitude':-6.8537}).execute('prepare_nearest_availability_request','{}')
    assert result['request']['pharmacy_id']==e['ids'][0]
    with e['store'].transaction(write=True) as tx:tx.execute('UPDATE assist_access SET active=0 WHERE pharmacy_id=?',(e['ids'][0],))
    result=runner(e,{'latitude':34.0015,'longitude':-6.8537}).execute('prepare_nearest_availability_request','{}')
    assert result['error']=='no_partner'


@pytest.mark.parametrize('value',['2026-10-25T14:48:00+01:00','2026-10-25T23:55:00+01:00','2026-02-15T12:00:00+00:00'])
def test_demo_schedules_are_calendar_based_not_relative_to_startup(value):
    doc=demo_document(epoch(value))
    d=doc.duties[0]
    first=datetime.fromisoformat(d.starts_at);last=datetime.fromisoformat(d.ends_at)
    assert first.strftime('%H:%M')==last.strftime('%H:%M')=='00:00'
    assert last.date()==first.date()+timedelta(days=1)
    assert doc.pharmacies[1].regular_hours.open_24h
    assert doc.pharmacies[0].regular_hours.opens_at=='09:00'
    assert doc.pharmacies[0].regular_hours.closes_at=='19:30'


def test_hours_and_twenty_four_hour_duty_validation():
    with pytest.raises(ValidationError):RegularHours(opens_at='19:30',closes_at='09:00')
    with pytest.raises(ValidationError):RegularHours(open_24h=True,opens_at='09:00')
    with pytest.raises(ValidationError):DutyInput(starts_at='2026-10-25T14:48:00+01:00',ends_at='2026-10-25T14:48:00+01:00',duty_type='24h')
    with pytest.raises(ValidationError):DutyInput(starts_at='2026-10-25T09:00:00+01:00',ends_at='2026-10-25T19:30:00+01:00',duty_type='24h')
    DutyInput(starts_at='2026-02-15T00:00:00+01:00',ends_at='2026-02-16T00:00:00+00:00',duty_type='24h')


def test_regular_schedule_is_not_a_verified_garde(env):
    rows=env['client'].post('/api/v1/pharmacies/search',json={'city':'Rabat'}).json()['results']
    jardin=next(p for p in rows if p['id']==env['ids'][1])
    assert jardin['regular_hours']['open_24h'] is True
    assert jardin['duty_status']=='unconfirmed' and jardin['stock_status']=='unknown'
    assert next(p for p in rows if p['id']==env['ids'][0])['regular_hours']['closes_at']=='19:30'


def test_refresh_demo_keeps_existing_partner_access(env,monkeypatch,capsys):
    from pharmacy_next.pharmacies.cli import main
    import sys
    e=env
    monkeypatch.setenv('PHARMACY_BACKEND','sqlite');monkeypatch.setenv('PHARMACY_ENVIRONMENT','demo')
    monkeypatch.setenv('PHARMACY_DIRECTORY_PROVIDER','local');monkeypatch.setenv('PHARMACY_SQLITE_PATH',e['settings'].pharmacy_sqlite_path)
    monkeypatch.setattr(sys,'argv',['cli','refresh-demo'])
    main()
    assert 'Code privé' not in capsys.readouterr().out
    assert e['store'].authenticate(e['tokens'][0],'pharmacist')['pharmacy_id']==e['ids'][0]
    assert e['store'].schedules()[e['ids'][1]]['open_24h'] is True
