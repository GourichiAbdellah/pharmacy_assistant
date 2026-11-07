import json

from fastapi.testclient import TestClient
import pytest

from conftest import ScriptedModel, Speech, answer, calls
from pharmacy_next.catalogue import Catalogue
from pharmacy_next.chat import unsupported_financial_values, financial_fallback
from pharmacy_next.data import medication
from pharmacy_next.main import create_app
from pharmacy_next.reimbursement import estimate_cnops
from pharmacy_next.sessions import Context
from pharmacy_next.tools import ToolRunner

C12, C24 = "0000000000012", "0000000000024"
COMPLETE_ETM = dict(ald_alc_declared=True, etm_valid=True, treatment_related=True)


@pytest.mark.parametrize('coverage,rate,amount', [
    ({}, '70', '38.57'),
    ({'ald_alc_declared': True}, '70', '38.57'),
    ({'etm_valid': True}, '70', '38.57'),
    (COMPLETE_ETM, '100', '55.10'),
    (COMPLETE_ETM | {'ald_alc_declared': False}, '70', '38.57'),
    (COMPLETE_ETM | {'etm_valid': False}, '70', '38.57'),
    (COMPLETE_ETM | {'treatment_related': False}, '70', '38.57'),
])
def test_rate_depends_on_all_three_declared_conditions(repository, coverage, rate, amount):
    med = medication(repository.rows[0] | {'cnops_pbr_source': '55.10', 'prix_public': '80'})
    result = estimate_cnops(med, coverage)
    assert result['taux_applique'] == rate
    assert result['montant_rembourse_estime'] == amount  # Uses PBR, not 80 DH PPV.
    assert result['droits_verifies'] is False and result['hors_complement_mutuelle'] is True
    assert result['rule']['source_url'].startswith('https://www.cnops.org.ma/')
    assert med['remboursement']['CNOPS']['taux_pourcent'] is None  # Source preserved.


@pytest.mark.parametrize('updates,coverage,reason', [
    ({'cnops_remboursable': '0'}, COMPLETE_ETM, 'non_remboursable'),
    ({'present_cnops': '0'}, COMPLETE_ETM, 'statut_inconnu'),
    ({'cnops_pbr_source': None}, COMPLETE_ETM, 'pbr_manquant'),
    ({}, COMPLETE_ETM | {'care_context': 'hospitalization'}, 'contexte_de_soins_non_couvert_par_cette_regle'),
])
def test_ineligible_missing_or_out_of_scope_does_not_produce_amount(repository, updates, coverage, reason):
    result = estimate_cnops(medication(repository.rows[0] | updates), coverage)
    assert result['status'] == 'unavailable' and result['reason'] == reason
    assert result['montant_rembourse_estime'] is None


def test_rounds_half_up_and_can_use_pbr_equal_to_public_price(repository):
    med = medication(repository.rows[0] | {'cnops_pbr_source': '9.95', 'prix_public': '9.95'})
    assert estimate_cnops(med, {})['montant_rembourse_estime'] == '6.97'


def test_declarations_are_scoped_to_code_and_can_be_reset(repository):
    context = Context()
    runner = ToolRunner(Catalogue(repository), context)
    runner.execute('search_medications', json.dumps({'queries': ['medicament demo']}))
    def estimate(code, **fields):
        return runner.execute('estimate_cnops_reimbursement', json.dumps({'medication_code': code, **fields}))
    assert estimate(C12, **COMPLETE_ETM)['taux_applique'] == '100'
    assert estimate(C12)['taux_applique'] == '100'
    assert estimate(C24)['taux_applique'] == '70'
    assert estimate(C12, etm_valid=None)['taux_applique'] == '70'
    assert estimate(C12, **COMPLETE_ETM)['taux_applique'] == '100'
    assert estimate(C12, reset_context=True)['taux_applique'] == '70'
    assert context.cnops_coverage_by_code[C12] == {}
    assert context.known_medications[C12]['remboursement']['CNSS']['base_remboursement'] == '8'


def test_calculated_amount_allowed_and_stale_calculation_removed(repository):
    context = Context()
    runner = ToolRunner(Catalogue(repository), context)
    runner.execute('search_medications', json.dumps({'queries': ['medicament demo']}))
    assert unsupported_financial_values('6,30 DH', context)
    runner.execute('estimate_cnops_reimbursement', json.dumps({'medication_code': C12}))
    assert unsupported_financial_values('Estimation : 6,30 DH à 70 %.', context) == []
    assert '6.30 DH' in financial_fallback(context)
    runner.execute('estimate_cnops_reimbursement', json.dumps({'medication_code': C12, **COMPLETE_ETM}))
    assert unsupported_financial_values('6,30 DH', context)
    assert unsupported_financial_values('9 DH à 100 % du PBR selon tes déclarations.', context) == []


def test_standard_estimate_then_etm_then_expired_agreement_in_same_chat(settings, repository):
    model = ScriptedModel([
        calls(('search_medications', {'queries': ['medicament demo']}),
              ('remember_context', {'selected_codes': [C12], 'organism': 'CNOPS'})),
        calls(('estimate_cnops_reimbursement', {'medication_code': C12})),
        answer('Au taux standard de 70 %, estimation de 6,30 DH par boîte, hors mutuelle.'),
        calls(('estimate_cnops_reimbursement', {'medication_code': C12, **COMPLETE_ETM})),
        answer('Selon tes déclarations, estimation à 100 % du PBR : 9 DH pour ce traitement.'),
        calls(('estimate_cnops_reimbursement', {'medication_code': C12, 'etm_valid': False})),
        answer('Avec cet accord expiré, estimation au taux standard de 70 % : 6,30 DH.'),
    ])
    app = create_app(settings, repository, model, Speech())
    with TestClient(app) as client:
        first = client.post('/api/v1/chat', json={'message': 'Remboursement CNOPS pour ma boîte de 12 ?'})
        assert first.status_code == 200
        sid = first.json()['session_id']
        assert not first.json()['diagnostics']['financial_retry']
        second = client.post('/api/v1/chat', json={'session_id': sid, 'message': 'Mon ALD est déclarée et mon accord ETM est valide pour ce traitement.'})
        assert second.status_code == 200 and '100 %' in second.json()['answer']
        assert not second.json()['diagnostics']['financial_retry']
        third = client.post('/api/v1/chat', json={'session_id': sid, 'message': 'Je me corrige : mon accord a expiré.'})
        assert third.status_code == 200 and '6,30 DH' in third.json()['answer']
        context = app.state.chat_service.sessions.sessions[sid].context
        assert context.cnops_coverage_by_code[C12]['etm_valid'] is False
        assert context.cnops_estimates[C12]['taux_applique'] == '70'
