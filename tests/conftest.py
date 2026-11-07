"""Toutes les données ci-dessous sont fictives et réservées aux tests."""
import copy
from types import SimpleNamespace

import pytest

from pharmacy_next.config import Settings


class Repository:
    def __init__(self):
        common = dict(nom="MEDICAMENT DEMO", forme="COMPRIME", dci="SUBSTANCE DEMO",
                      dosage="500", unite_dosage="MG", prix_hospitalier=None,
                      present_cnops="1", present_cnss="1", cnops_remboursable="1",
                      cnss_remboursable="1", cnss_taux_remboursement="70", cnops_pbr_source="9",
                      cnss_ppv_br_source="8")
        self.rows = [dict(common, code="0000000000012", presentation="BOITE DE 12 COMPRIMES", prix_public="10.5"),
                     dict(common, code="0000000000024", presentation="BOITE DE 24 COMPRIMES", prix_public="18")]
        self.notices = [{"id": 10, "nom": "MEDICAMENT DEMO", "dci": "SUBSTANCE DEMO",
                         "forme": "Comprimé", "dosage": "500 mg par comprimé", "titre": "Notice fictive",
                         "source": "Asafar", "url_source": "https://asafar.net/fiche/test-fixture/",
                         "indications": "Texte fictif réservé aux tests.", "effets_indesirables": "aucune info"}]

    def medications(self):
        return copy.deepcopy(self.rows)

    def notice_metadata(self):
        return [{k: v for k, v in row.items() if k not in {"indications", "effets_indesirables"}}
                for row in self.notices]

    def notice(self, notice_id):
        return next((copy.deepcopy(r) for r in self.notices if r["id"] == notice_id), None)


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    async def complete(self, messages, tools):
        self.inputs.append(copy.deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def close(self):
        pass


class Speech:
    async def transcribe(self, payload, filename, content_type):
        return {"text": "wach kaytremboursa f CNSS ?", "language": "Arabic"}

    async def close(self):
        pass


@pytest.fixture
def repository():
    return Repository()


@pytest.fixture
def settings():
    return Settings(llm_api_key="test-key-never-sent", catalogue_cache_seconds=300)


def answer(value):
    return {"role": "assistant", "content": value}


def calls(*functions):
    import json
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(functions)
    ]}
