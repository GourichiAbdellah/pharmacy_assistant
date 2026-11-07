from fastapi.testclient import TestClient

from conftest import ScriptedModel, Speech
from pharmacy_next.main import create_app


def test_markdown_assets_are_served_without_external_services(settings, repository):
    app = create_app(settings, repository, ScriptedModel([]), Speech())
    with TestClient(app) as client:
        page = client.get('/')
        assert page.status_code == 200
        for asset in ('markdown.js', 'markdown.css', 'vendor/marked.umd.js', 'vendor/purify.min.js'):
            response = client.get('/static/' + asset)
            assert response.status_code == 200, asset
            assert response.content
        assert client.get('/static/../config.py').status_code == 404
        assert client.get('/static/%2e%2e/config.py').status_code == 404


def test_page_and_scripts_share_a_cache_busted_revision(settings,repository):
    from pharmacy_next.config import UI_VERSION
    app=create_app(settings,repository,ScriptedModel([]),Speech())
    with TestClient(app) as c:
        for path in ('/','/pharmacien','/static/pharmacist.js','/static/pharmacist-nav.js','/static/pharmacy-common.js'):
            response=c.get(path)
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
        for path in ('/','/pharmacien'):
            html=c.get(path).text
            assert '/static/pharmacy-common.js?v='+UI_VERSION in html
            assert '/static/human-relay.js?v='+UI_VERSION in html
        assert c.get('/health').json()['ui_version']==UI_VERSION
