"""All records and amounts are fictional. No real patient or cloud service."""
import json

from fastapi.testclient import TestClient
import pytest

from conftest import ScriptedModel, Speech, answer, calls
from pharmacy_next.catalogue import Catalogue
from pharmacy_next.chat import unsupported_financial_values, financial_fallback
from pharmacy_next.data import medication, MED_COLUMNS, SqlRepository
from pharmacy_next.main import create_app
from pharmacy_next.reimbursement import estimate_cnss
from pharmacy_next.sessions import Context
from pharmacy_next.tools import ToolRunner

C12 = "0000000000012"


def test_sql_select_includes_the_exact_confirmed_base_column(monkeypatch, settings):
    assert 'cnss_ppv_br_source' in MED_COLUMNS
    repository = SqlRepository(settings)
    sent = []
    monkeypatch.setattr(repository, '_read', lambda sql: sent.append(sql) or [])
    repository.medications()
    assert '[cnss_ppv_br_source]' in sent[0]


@pytest.mark.parametrize('base,public,expected', [
    ('80','100','56.00'), ('37,40','50','26.18'), ('9.95','9.95','6.97'), ('0','10','0.00'),
])
def test_standard_calculation_uses_cnss_base_not_ppv_or_cnops(repository, base, public, expected):
    med = medication(repository.rows[0] | {'cnss_ppv_br_source':base, 'prix_public':public,
                                          'cnops_pbr_source':'999'})
    result = estimate_cnss(med)
    assert result['montant_rembourse_estime'] == expected
    assert result['taux_applique'] == '70' and result['organism'] == 'CNSS'
    assert result['droits_verifies'] is False and result['quantite_boites'] == 1
    assert result['rule']['base_source_column'] == 'cnss_ppv_br_source'
    assert result['scenario'] == 'standard_ambulatoire'


@pytest.mark.parametrize('updates,care_context,reason', [
    ({'cnss_remboursable':'0'}, None, 'non_remboursable'),
    ({'present_cnss':'0'}, None, 'statut_inconnu'),
    ({'cnss_remboursable':None}, None, 'statut_inconnu'),
    ({'cnss_ppv_br_source':None}, None, 'base_cnss_manquante'),
    ({'cnss_ppv_br_source':'-1'}, None, 'base_cnss_manquante'),
    ({'cnss_ppv_br_source':'nan'}, None, 'base_cnss_manquante'),
    ({'cnss_taux_remboursement':'100'}, None, 'taux_catalogue_non_standard'),
    ({}, 'hospitalization', 'contexte_de_soins_non_couvert_par_cette_regle'),
    ({}, 'unknown', 'contexte_de_soins_non_couvert_par_cette_regle'),
])
def test_missing_ineligible_or_nonstandard_does_not_invent_an_amount(repository, updates, care_context, reason):
    med = medication(repository.rows[0] | updates)
    result = estimate_cnss(med, care_context)
    assert result['status'] == 'unavailable' and result['reason'] == reason
    assert result['montant_rembourse_estime'] is None
    if med['remboursement']['CNSS']['remboursable'] is not True:
        assert med['remboursement']['CNSS']['base_remboursement'] is None


def test_missing_catalogue_rate_uses_the_sourced_standard_rule(repository):
    result = estimate_cnss(medication(repository.rows[0] | {'cnss_taux_remboursement':None}))
    assert result['taux_catalogue'] is None
    assert result['taux_applique'] == '70' and result['montant_rembourse_estime'] == '5.60'


def test_both_funds_can_be_compared_without_erasing_each_others_calculation(repository):
    context = Context()
    runner = ToolRunner(Catalogue(repository), context)
    assert runner.execute('estimate_cnss_reimbursement', json.dumps({'medication_code':C12}))['error'] == 'medication_not_retrieved'
    runner.execute('search_medications', json.dumps({'queries':['medicament demo']}))
    args = json.dumps({'medication_code':C12})
    runner.execute('estimate_cnops_reimbursement', args)
    runner.execute('estimate_cnss_reimbursement', args)
    assert context.cnops_estimates[C12]['montant_rembourse_estime'] == '6.30'
    assert context.cnss_estimates[C12]['montant_rembourse_estime'] == '5.60'
    assert unsupported_financial_values('CNOPS : 6,30 DH ; CNSS : 5,60 DH à 70 %.', context) == []
    assert '5.60 DH' in financial_fallback(context)
    # Same data can be reread; changed source data invalidates both estimates.
    context.remember_records([medication(repository.rows[0] | {'cnss_ppv_br_source':'7'})])
    assert C12 not in context.cnss_estimates and C12 not in context.cnops_estimates
    assert unsupported_financial_values('5,60 DH', context)


def test_tool_refuses_an_invented_individual_rate(repository):
    runner = ToolRunner(Catalogue(repository), Context())
    result = runner.execute('estimate_cnss_reimbursement', json.dumps({'medication_code':C12, 'rate':100}))
    assert result['error'] == 'invalid_arguments'


def test_price_then_cnss_estimation_in_same_conversation(settings, repository):
    model = ScriptedModel([
        calls(('search_medications', {'queries':['medicament demo']}),
              ('remember_context', {'selected_codes':[C12]})),
        answer('La boîte de 12 coûte 10,5 DH.'),
        calls(('remember_context', {'organism':'CNSS'})),
        calls(('estimate_cnss_reimbursement', {'medication_code':C12})),
        answer('Au taux standard CNSS de 70 % de la base de 8 DH, estimation de 5,60 DH par boîte.'),
    ])
    app = create_app(settings, repository, model, Speech())
    with TestClient(app) as client:
        first = client.post('/api/v1/chat', json={'message':'prix medicament demo boite de 12 ?'}).json()
        second = client.post('/api/v1/chat', json={'session_id':first['session_id'],'message':'wach kaytremboursa f cnss ?'})
        assert second.status_code == 200, second.text
        result = second.json()
        assert '5,60 DH' in result['answer']
        assert result['diagnostics']['financial_retry'] is False
        assert result['context'] == {'selected_codes':[C12],'organism':'CNSS'}
        assert 'estimate_cnss_reimbursement' in result['diagnostics']['tools_used']
        context = app.state.chat_service.sessions.sessions[first['session_id']].context
        assert context.cnss_estimates[C12]['base_remboursement'] == '8'
        assert any(s['type']=='coverage_rule' for s in result['sources'])
