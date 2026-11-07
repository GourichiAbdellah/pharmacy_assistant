"""Disposable local UI fixture. No Azure/LLM/pharmacy traffic."""
import json
from pathlib import Path
import socket
import sys
import tempfile
from dataclasses import replace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from conftest import Repository, Speech, calls, answer
from pharmacy_next.config import Settings
from pharmacy_next.main import create_app
from pharmacy_next.pharmacies.cli import demo_document
from pharmacy_next.pharmacies.service import PharmacyService
from pharmacy_next.pharmacies.store import PharmacyStore
from pharmacy_next.catalogue import Catalogue

CODE='0000000000012'
class Model:
    async def complete(self,messages,tools):
        start=max(i for i,m in enumerate(messages) if m['role']=='user')
        names=[c['function']['name'] for m in messages[start:] for c in m.get('tool_calls',[])]
        if '--google' in sys.argv and 'pharmacie' in messages[start]['content'].lower():
            if 'search_pharmacies' not in names:return calls(('search_pharmacies',{'city':'Rabat'}))
            return answer('Voici les fiches trouvées. Seule une pharmacie partenaire peut recevoir ta demande.')
        if 'get_medication_details' not in names:return calls(('get_medication_details',{'codes':[CODE]}))
        if 'remember_context' not in names:return calls(('remember_context',{'selected_codes':[CODE]}))
        if any(x in messages[start]['content'].lower() for x in ['disponibil','voici ma position']) and 'prepare_nearest_availability_request' not in names:
            return calls(('prepare_nearest_availability_request',{}))
        if 'demande' in messages[start]['content'].lower() and not {'prepare_availability_request','prepare_nearest_availability_request'} & set(names):
            return calls(('prepare_availability_request',{'pharmacy_id':pharmacy_id,'medication_code':CODE}))
        return answer('Voici le **médicament fictif**, boîte de 12. Le récapitulatif doit être confirmé avant tout envoi.')
    async def close(self):pass

scratch=tempfile.TemporaryDirectory(prefix='pharmacy-ui-')
settings=Settings(pharmacy_backend='sqlite',pharmacy_sqlite_path=str(Path(scratch.name)/'pharmacies.sqlite3'))
if '--google' in sys.argv:
    settings=replace(settings,pharmacy_environment='live',pharmacy_directory_provider='google_places',google_places_api_key='UI-FAKE-KEY-NEVER-SENT')
store=PharmacyStore(settings);store.initialize()
service=PharmacyService(store,Catalogue(Repository(),300));document=demo_document(store.now())
if '--google' in sys.argv:
    for p in document.pharmacies:p.is_demo=False
service.import_directory(document)
pharmacy_id=str(document.pharmacies[0].id);token=store.issue_access('pharmacist',pharmacy_id)['token']
agdal_token=store.issue_access('pharmacist',str(document.pharmacies[1].id))['token']
app=create_app(settings,Repository(),Model(),Speech(),pharmacy_store=store)
if '--google' in sys.argv:
    import httpx
    from test_pharmacy_providers import place
    from pharmacy_next.pharmacies.providers import GooglePlacesClient
    store.link_google(pharmacy_id,'ChIJ_test_only_1')
    def transport(req):
        if 'Quota' in req.content.decode():return httpx.Response(429,text='UI-FAKE-KEY-NEVER-SENT')
        return httpx.Response(200,json={'places':[
            place(displayName={'text':'Pharmacie API SYNTHETIQUE PARTENAIRE'}),
            place('ChIJ_ui_external',displayName={'text':'Pharmacie API SYNTHETIQUE EXTERNE <img src=x onerror=alert(1)>'})]})
    app.state.pharmacies.external=GooglePlacesClient(settings,transport=httpx.MockTransport(transport))
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
print(json.dumps(dict(origin=f'http://127.0.0.1:{port}',token=token,token_agdal=agdal_token)),flush=True)
server=uvicorn.Server(uvicorn.Config(app,log_level='warning'))
try:server.run(sockets=[sock])
finally:scratch.cleanup()
