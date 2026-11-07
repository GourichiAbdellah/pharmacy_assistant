import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pharmacy_next.audio import MoulSotClient, TranscriptionError
from pharmacy_next.data import SqlRepository, DataUnavailable
from pharmacy_next.llm import ModelClient, ModelUnavailable


@pytest.mark.parametrize('provider', ['deepseek', 'azure'])
def test_endpoint_model_key_and_tools_sent_to_matching_provider(monkeypatch, settings, provider):
    import pharmacy_next.llm as llm
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[
        SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='Bonjour', tool_calls=None))]))
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm, 'AsyncOpenAI', factory)
    endpoint = 'https://api.deepseek.com' if provider == 'deepseek' else 'https://example.openai.azure.com/openai/v1/'
    config = replace(settings, llm_provider=provider, llm_base_url=endpoint, llm_model='test-deployment')
    result = asyncio.run(ModelClient(config).complete([{'role':'user','content':'salam'}], []))
    assert result['content'] == 'Bonjour'
    assert factory.call_args.kwargs['base_url'] == endpoint
    assert factory.call_args.kwargs['api_key'] == 'test-key-never-sent'
    sent = client.chat.completions.create.call_args.kwargs
    assert sent['model'] == 'test-deployment' and sent['tools'] == []
    if provider == 'deepseek':
        assert sent['extra_body']['thinking']['type'] == 'disabled'
    else:
        assert 'extra_body' not in sent


def test_llm_errors_do_not_leak_keys(monkeypatch, settings):
    import pharmacy_next.llm as llm
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError('Authorization Bearer TOP_SECRET'))
    monkeypatch.setattr(llm, 'AsyncOpenAI', MagicMock(return_value=client))
    with pytest.raises(ModelUnavailable) as error:
        asyncio.run(ModelClient(settings).complete([], []))
    assert 'TOP_SECRET' not in str(error.value)


@pytest.mark.parametrize('status,payload,expected', [
    (200, {'text':'salam', 'language':'Arabic'}, None),
    (200, {'text':''}, 502), (200, [], 502), (503, {'detail':'private gpu error'}, 503),
    (422, {'detail':'bad audio'}, 422),
])
def test_moulsot_http_contract(settings, status, payload, expected):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=payload)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            speech = MoulSotClient(settings, client=http)
            if expected:
                with pytest.raises(TranscriptionError) as error:
                    await speech.transcribe(b'audio', 'vocal.ogg', 'audio/ogg')
                assert error.value.status == expected
            else:
                assert (await speech.transcribe(b'audio', 'vocal.ogg', 'audio/ogg'))['text'] == 'salam'
    asyncio.run(run())
    assert requests[0].url.path == '/transcribe'
    assert b'name="file"' in requests[0].content


def test_sql_is_parameterized_and_connections_close(monkeypatch, settings):
    import pharmacy_next.data as data
    driver, connection, cursor = MagicMock(), MagicMock(), MagicMock()
    driver.connect.return_value = connection
    connection.cursor.return_value = cursor
    cursor.description = [('id',)]
    cursor.fetchall.return_value = [(10,)]
    monkeypatch.setattr(data, 'sql_driver', lambda: driver)
    config = replace(settings, azure_sql_password='semi;brace}password')
    assert SqlRepository(config).notice(10) == {'id':10}
    sql, parameters = cursor.execute.call_args.args
    assert 'WHERE [id]=?' in sql and parameters == (10,)
    connection_string = driver.connect.call_args.args[0]
    assert 'PWD={semi;brace}}password}' in connection_string
    assert 'Connection Timeout' not in connection_string
    assert driver.connect.call_args.kwargs['timeout'] == 30
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_sql_failure_is_sanitized(monkeypatch, settings):
    import pharmacy_next.data as data
    driver = MagicMock()
    driver.connect.side_effect = RuntimeError('PWD=TOP_SECRET')
    monkeypatch.setattr(data, 'sql_driver', lambda: driver)
    with pytest.raises(DataUnavailable) as error:
        SqlRepository(replace(settings, azure_sql_password='TOP_SECRET')).medications()
    assert 'TOP_SECRET' not in str(error.value)
