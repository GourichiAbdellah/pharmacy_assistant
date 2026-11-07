import asyncio
import json

from fastapi.testclient import TestClient

from pharmacy_next.main import create_app
from pharmacy_next.llm import ModelUnavailable
from conftest import ScriptedModel, Speech, answer, calls

C12, C24 = "0000000000012", "0000000000024"


def test_prices_then_natural_selection_then_reimbursement_then_vocabulary(settings, repository):
    model = ScriptedModel([
        calls(("search_medications", {"queries": ["medicament demo"]})),
        answer("La boîte de 12 coûte 10,5 DH et celle de 24 coûte 18 DH."),
        calls(("remember_context", {"selected_codes": [C12]})),
        answer("D'accord, tu as la boîte de 12 comprimés."),
        calls(("remember_context", {"organism": "CNSS"}), ("get_medication_details", {"codes": [C12]})),
        answer("Oui, elle est remboursable à la CNSS. Le taux indiqué est 70 % et la base est 8 DH."),
        answer("Un comprimé est une forme solide de médicament."),
    ])
    app = create_app(settings, repository, model, Speech())
    with TestClient(app) as client:
        first = client.post('/api/v1/chat', json={"message": "prix medicament demo ?"})
        assert first.status_code == 200, first.text
        sid = first.json()["session_id"]
        assert "10,5 DH" in first.json()["answer"] and "18 DH" in first.json()["answer"]
        assert first.json()["context"]["selected_codes"] == []
        assert "maps" not in first.json()["answer"].lower()
        second = client.post('/api/v1/chat', json={"message": "j'ai celui avec 12 comprimés", "session_id": sid})
        assert second.json()["context"]["selected_codes"] == [C12]
        third = client.post('/api/v1/chat', json={"message": "wach kaytremboursa f cnss ?", "session_id": sid})
        assert third.json()["context"] == {"selected_codes": [C12], "organism": "CNSS"}
        assert "70 %" in third.json()["answer"]
        fourth = client.post('/api/v1/chat', json={"message": "achnahia comprimé ?", "session_id": sid})
        assert fourth.status_code == 200
        assert fourth.json()["diagnostics"]["tools_used"] == []
        assert fourth.json()["context"]["selected_codes"] == [C12]
        history = model.inputs[-1]
        assert any(m.get('content') == "j'ai celui avec 12 comprimés" for m in history)


def test_financial_retry_does_not_ask_user_to_rephrase(settings, repository):
    model = ScriptedModel([
        calls(("search_medications", {"queries": ["medicament demo"]})),
        answer("La boîte de 12 coûte 999 DH."), answer("La boîte de 12 coûte 10,5 DH."),
    ])
    with TestClient(create_app(settings, repository, model, Speech())) as client:
        result = client.post('/api/v1/chat', json={"message": "le prix ?"}).json()
        assert result["diagnostics"]["financial_retry"] is True
        assert result["answer"] == "La boîte de 12 coûte 10,5 DH."


def test_second_unfounded_number_falls_back_to_source_values(settings, repository):
    model = ScriptedModel([
        calls(("search_medications", {"queries": ["medicament demo"]})),
        answer("999 DH"), answer("888 DH"),
    ])
    with TestClient(create_app(settings, repository, model, Speech())) as client:
        result = client.post('/api/v1/chat', json={"message": "prix"})
        assert result.status_code == 200
        assert '999' not in result.json()['answer'] and '888' not in result.json()['answer']
        assert '10.5 DH' in result.json()['answer']


def test_failed_turn_keeps_preceding_context(settings, repository):
    model = ScriptedModel([
        calls(("search_medications", {"queries": ["medicament demo"]})), answer("Deux boîtes existent."),
        calls(("remember_context", {"selected_codes": [C12]})), ModelUnavailable("Panne simulée"),
    ])
    app = create_app(settings, repository, model, Speech())
    with TestClient(app) as client:
        sid = client.post('/api/v1/chat', json={"message": "prix"}).json()['session_id']
        response = client.post('/api/v1/chat', json={"message": "12 comprimés", "session_id": sid})
        assert response.status_code == 503
        session = app.state.chat_service.sessions.sessions[sid]
        assert session.context.selected_codes == []
        assert len(session.turns) == 1


def test_audio_uses_the_same_session_and_raw_transcription(settings, repository):
    model = ScriptedModel([answer("Bonjour."), answer("Tu dépends donc de la CNSS ?")])
    with TestClient(create_app(settings, repository, model, Speech())) as client:
        sid = client.post('/api/v1/chat', json={"message": "salam"}).json()['session_id']
        response = client.post('/api/v1/chat/audio', data={"session_id": sid}, files={"file": ("voice.ogg", b"test-audio", "audio/ogg")})
        assert response.status_code == 200
        assert response.json()['session_id'] == sid
        assert response.json()['transcription']['text'] == 'wach kaytremboursa f CNSS ?'
        assert model.inputs[-1][-1]['content'] == 'wach kaytremboursa f CNSS ?'
        assert any(m.get('content') == 'salam' for m in model.inputs[-1])


def test_expired_session_is_not_silently_recreated(settings, repository):
    model = ScriptedModel([answer("Bonjour.")])
    app = create_app(settings, repository, model, Speech())
    with TestClient(app) as client:
        sid = client.post('/api/v1/chat', json={"message": "salam"}).json()['session_id']
        app.state.chat_service.sessions.sessions[sid].touched -= settings.session_ttl_seconds + 1
        result = client.post('/api/v1/chat', json={"message": "celui de 12", "session_id": sid})
        assert result.status_code == 410


def test_text_works_when_speech_service_is_down(settings, repository):
    from pharmacy_next.audio import TranscriptionError
    class BrokenSpeech(Speech):
        async def transcribe(self, *args):
            raise TranscriptionError(503, "MoulSot inaccessible")
    with TestClient(create_app(settings, repository, ScriptedModel([answer("Bonjour.")]), BrokenSpeech())) as client:
        assert client.post('/api/v1/chat/audio', files={"file": ("voice.wav", b"x")}).status_code == 503
        assert client.post('/api/v1/chat', json={"message": "salam"}).status_code == 200


def test_ui_and_empty_input(settings, repository):
    with TestClient(create_app(settings, repository, ScriptedModel([]), Speech())) as client:
        page = client.get('/')
        assert page.status_code == 200 and 'Enregistrer' in page.text
        assert '<select' not in page.text
        assert client.post('/api/v1/chat', json={"message": "   "}).status_code == 422
        assert client.post('/api/v1/chat/audio', files={"file": ('empty.wav', b'')}).status_code == 413
