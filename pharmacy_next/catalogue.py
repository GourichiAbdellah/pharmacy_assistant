"""Candidate retrieval. Similarity is never a medication identity decision."""
from collections import defaultdict
from difflib import SequenceMatcher
import re
import threading
import time
import unicodedata

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from .data import DataUnavailable, medication, text


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


class RecordIndex:
    def __init__(self, rows, key):
        self.rows = rows
        self.key = key
        self.by_id = {str(row[key]): row for row in rows}
        if len(self.by_id) != len(rows):
            raise DataUnavailable("Identifiants dupliqués dans la source : chargement interrompu.")
        self.groups = defaultdict(list)
        for row in rows:
            self.groups[normalize(row.get("nom"))].append(row)
        self.names = list(self.groups)
        self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), dtype=np.float32)
        self.matrix = self.vectorizer.fit_transform(self.names) if self.names else None

    def search(self, queries, limit=20, offset=0):
        normalized = [normalize(q) for q in queries if normalize(q)]
        exact_ids = [self.by_id[q.strip()] for q in queries if q.strip() in self.by_id]
        exact_names = {name for name in self.names if name in normalized}
        exact_dci = [r for r in self.rows if normalize(r.get("dci")) in normalized]
        if exact_ids:
            ranked, strategy = exact_ids, "exact_code"
        elif exact_names:
            # Include explicitly named line extensions, retaining their distinct labels.
            related = [n for n in self.names if any(n.startswith(q + " ") for q in exact_names)]
            ranked = [r for n in sorted(exact_names) + sorted(set(related) - exact_names)
                      for r in self.groups[n]]
            strategy = "exact_name_and_label_extensions"
        elif exact_dci:
            ranked, strategy = exact_dci, "exact_dci"
        elif self.matrix is not None and normalized:
            scores = (self.matrix @ self.vectorizer.transform(normalized).T).toarray().max(axis=1)
            best = sorted(range(len(scores)), key=lambda i: float(scores[i]), reverse=True)
            candidates = [i for i in best[:40] if scores[i] >= 0.22]
            # Shared suffixes (e.g. PLUS) must not dominate the complete name.
            # This reranks candidates only; it never confirms a drug identity.
            def ranking(i):
                ordered = max(SequenceMatcher(None, q, self.names[i], autojunk=False).ratio()
                              for q in normalized)
                return 0.5 * float(scores[i]) + 0.5 * ordered
            candidates.sort(key=ranking, reverse=True)
            names = [self.names[i] for i in candidates[:4]]
            ranked = [r for name in names for r in self.groups[name]]
            strategy = "spelling_candidates_not_confirmed"
        else:
            ranked, strategy = [], "no_match"
        unique = list({str(r[self.key]): r for r in ranked}.values())
        return {
            "strategy": strategy, "total": len(unique), "offset": offset,
            "has_more": offset + limit < len(unique),
            "results": unique[offset:offset + limit],
        }


class Catalogue:
    def __init__(self, repository, ttl=300):
        self.repository, self.ttl = repository, ttl
        self._cache = {}
        self._locks = {"medications": threading.Lock(), "notices": threading.Lock()}

    def _index(self, kind):
        with self._locks[kind]:
            cached = self._cache.get(kind)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
            if kind == "medications":
                rows = [medication(r) for r in self.repository.medications()]
                if any(not r["code"] or not r["nom"] for r in rows):
                    raise DataUnavailable("Code ou nom de médicament manquant dans la source.")
                index = RecordIndex(rows, "code")
            else:
                rows = [{k: text(v) for k, v in r.items()} for r in self.repository.notice_metadata()]
                index = RecordIndex(rows, "id")
            self._cache[kind] = (time.monotonic() + self.ttl, index)
            return index

    def search_medications(self, queries, limit=20, offset=0):
        return self._index("medications").search(queries, limit, offset)

    def get_medications(self, codes):
        index = self._index("medications")
        return {"results": [index.by_id[c] for c in codes if c in index.by_id],
                "not_found": [c for c in codes if c not in index.by_id]}

    def search_notices(self, queries, limit=8, offset=0):
        return self._index("notices").search(queries, limit, offset)


def strength_signature(row):
    """Compare explicit components without converting or changing source units."""
    dosage = text(row.get("dosage"))
    if not dosage:
        return None
    units = text(row.get("unite_dosage"))
    if units:
        values = [p.strip().replace(",", ".") for p in dosage.split("/")]
        unit_parts = [p.strip().casefold() for p in units.split("/")]
        if len(values) != len(unit_parts) or any(not re.fullmatch(r"\d+(?:\.\d+)?", v) for v in values):
            return None
        return list(zip(values, unit_parts))
    pairs = re.findall(r"(\d+(?:[.,]\d+)?)\s*(mg|µg|μg|g|mui|ui|%)\b", dosage, flags=re.I)
    return [(v.replace(",", "."), u.casefold()) for v, u in pairs] or None


def notice_compatibility(med, notice):
    comparisons = {}
    for key in ("nom", "dci", "forme"):
        left, right = normalize(med.get(key)), normalize(notice.get(key))
        comparisons[key] = "unknown" if not left or not right else "same" if left == right else "different"
    left, right = strength_signature(med), strength_signature(notice)
    comparisons["dosage"] = "unknown" if left is None or right is None else "same" if left == right else "different"
    status = ("conflict" if "different" in comparisons.values() else
              "compatible_fields" if all(v == "same" for v in comparisons.values()) else "unverified")
    return {"status": status, "fields": comparisons,
            "note": "Comparaison conservatrice des champs. Aucune fusion des sources ni validation clinique."}
