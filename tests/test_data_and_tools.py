import json

from pharmacy_next.catalogue import Catalogue, RecordIndex, notice_compatibility
from pharmacy_next.chat import unsupported_financial_values
from pharmacy_next.data import medication
from pharmacy_next.sessions import Context
from pharmacy_next.tools import ToolRunner


def test_non_reimbursable_does_not_expose_raw_pbr_as_usable_base(repository):
    row = repository.rows[0] | {"cnops_remboursable": "0", "cnops_pbr_source": "90"}
    info = medication(row)['remboursement']['CNOPS']
    assert info['remboursable'] is False
    assert info['base_remboursement'] is None
    assert info['taux_pourcent'] is None


def test_absence_from_fund_is_unknown_and_confirmed_cnss_base_is_exposed(repository):
    row = repository.rows[0] | {"present_cnops": "0", "cnss_ppv_br_source": "200"}
    data = medication(row)
    assert data['remboursement']['CNOPS']['remboursable'] is None
    assert data['remboursement']['CNSS']['base_remboursement'] == '200'
    assert data['remboursement']['CNOPS']['base_remboursement'] is None


def test_exact_search_compares_variants_without_forcing_selection(repository):
    catalogue = Catalogue(repository)
    result = catalogue.search_medications(['medicament demo'])
    assert len(result['results']) == 2
    assert {r['prix_public'] for r in result['results']} == {'10.5', '18'}
    assert result['results'][0]['code'].startswith('000')


def test_misspelling_is_a_candidate_not_an_identity(repository):
    result = Catalogue(repository).search_medications(['medikament demo'])
    assert result['results'] and result['strategy'] == 'spelling_candidates_not_confirmed'


def test_unknown_query_can_return_no_results(repository):
    assert Catalogue(repository).search_medications(['zzzxqvv'])['results'] == []


def test_shared_suffix_does_not_outweigh_the_complete_name():
    index = RecordIndex([
        {'code': str(i), 'nom': name}
        for i, name in enumerate(['PLUSO PLUS', 'BIOTIC PLUS', 'INOPRIL PLUS',
                                  'CLARADOL PLUS', 'BIOTIC PLUS 1G /125 MG'])
    ], 'code')
    result = index.search(['byotik plus'])
    assert result['results'][0]['nom'] == 'BIOTIC PLUS'
    assert result['strategy'] == 'spelling_candidates_not_confirmed'


def test_duplicate_codes_are_not_silently_dropped(repository):
    import pytest
    from pharmacy_next.data import DataUnavailable
    repository.rows.append(repository.rows[0].copy())
    with pytest.raises(DataUnavailable):
        Catalogue(repository).search_medications(['demo'])


def test_pack_size_and_dosage_numbers_do_not_trigger_money_guard(repository):
    context = Context()
    context.remember_records([medication(repository.rows[0])])
    assert unsupported_financial_values('12 comprimés de 500 mg à 10,5 DH.', context) == []
    assert unsupported_financial_values('Prix 999 DH.', context)
    assert unsupported_financial_values('Remboursé à 100 %.', context)


def test_tool_validation_denies_arbitrary_sql_and_invented_selection(repository):
    runner = ToolRunner(Catalogue(repository), Context())
    assert runner.execute('execute_sql', '{"sql":"DROP TABLE medicaments"}')['error'] == 'unknown_tool'
    assert runner.execute('search_medications', '{"queries":["demo"],"sql":"bad"}')['error'] == 'invalid_arguments'
    assert runner.execute('remember_context', '{"selected_codes":["invented"]}')['error'] == 'unknown_medication_code'


def test_money_grouping_is_supported_and_negative_amount_is_rejected(repository):
    context = Context()
    context.remember_records([medication(repository.rows[0] | {'prix_public': '1234.5'})])
    assert unsupported_financial_values('Prix : 1 234,5 DH.', context) == []
    assert unsupported_financial_values('Prix : 1\u202f234,5 DH.', context) == []
    assert unsupported_financial_values('Prix : -1234,5 DH.', context)


def test_absent_clinical_section_is_not_no_risk(repository):
    runner = ToolRunner(Catalogue(repository), Context())
    runner.execute('search_notices', '{"queries":["medicament demo"]}')
    result = runner.execute('read_notice', '{"notice_id":10,"sections":["effets_indesirables"]}')
    assert result['sections']['effets_indesirables']['available'] is False
    assert result['sections']['effets_indesirables']['text'] == 'aucune info'


def test_notice_dose_conflict_is_blocked_even_if_model_omits_medication_code(repository):
    context = Context(selected_codes=['0000000000012'])
    context.remember_records([medication(repository.rows[0])])
    repository.notices[0]['dosage'] = '1000 mg par comprimé'
    runner = ToolRunner(Catalogue(repository), context)
    runner.execute('search_notices', '{"queries":["medicament demo"]}')
    result = runner.execute('read_notice', '{"notice_id":10,"sections":["indications"]}')
    assert result['linkage']['status'] == 'conflict'
    assert result['sections'] == {}


def test_compatible_notice_ignores_pack_quantity_but_preserves_dose(repository):
    med = medication(repository.rows[1])
    notice = repository.notices[0] | {'presentation': 'BOITE DE 12'}
    assert notice_compatibility(med, notice)['status'] == 'compatible_fields'
