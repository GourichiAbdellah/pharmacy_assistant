"""Lecture seule des tables existantes et contrat de données présenté au LLM."""
from contextlib import contextmanager
import csv
from decimal import Decimal, InvalidOperation
import importlib
from pathlib import Path
import threading

SECTIONS = (
    "indications", "posologie", "contre_indications", "effets_indesirables",
    "interactions", "precautions", "grossesse_allaitement", "conduite", "surdosage",
)
MED_COLUMNS = (
    "code", "nom", "forme", "presentation", "dci", "dosage", "unite_dosage",
    "prix_public", "prix_hospitalier", "cnops_remboursable", "cnops_pbr_source",
    "cnss_remboursable", "cnss_taux_remboursement", "cnss_ppv_br_source", "present_cnops", "present_cnss",
)
NOTICE_COLUMNS = (
    "id", "url_source", "source", "titre", "nom", "dci", "dosage", "forme",
    "presentation", "date_mise_a_jour_source", "date_collecte",
)


class DataUnavailable(RuntimeError):
    pass


def text(value):
    if value is None or str(value).strip().lower() in {"", "nan", "null", "none"}:
        return None
    return str(value).strip()


def flag(value):
    value = text(value)
    if value in {"1", "1.0", "True", "true"}:
        return True
    if value in {"0", "0.0", "False", "false"}:
        return False
    return None


def amount(value):
    raw = text(value)
    if raw is None:
        return None
    try:
        number = Decimal(raw.replace(",", "."))
        if not number.is_finite() or number < 0:
            return None
        return format(number.normalize(), "f")
    except InvalidOperation:
        return None


def medication(row):
    result = {key: text(row.get(key)) for key in (
        "code", "nom", "forme", "presentation", "dci", "dosage", "unite_dosage",
    )}
    result.update(prix_public=amount(row.get("prix_public")),
                  prix_hospitalier=amount(row.get("prix_hospitalier")), currency="MAD")
    reimbursement = {}
    for fund in ("cnops", "cnss"):
        present = flag(row.get(f"present_{fund}")) is True
        status = flag(row.get(f"{fund}_remboursable")) if present else None
        rate = amount(row.get("cnss_taux_remboursement")) if fund == "cnss" and status is True else None
        if rate is not None and Decimal(rate) > 100:
            rate = None
        base_column = "cnops_pbr_source" if fund == "cnops" else "cnss_ppv_br_source"
        base = amount(row.get(base_column)) if status is True else None
        reimbursement[fund.upper()] = {
            "present_in_source": present,
            "remboursable": status,
            "taux_pourcent": rate,
            "base_remboursement": base,
            "montant_rembourse": None,
            "note": ("Le PBR est une base. Le taux n'est pas dans le catalogue : estimate_cnops_reimbursement applique la règle ambulatoire et calcule une estimation."
                     if fund == "cnops" else
                     "La base CNSS provient de cnss_ppv_br_source (PPV-BR), conformément au schéma confirmé. estimate_cnss_reimbursement calcule l'estimation standard à 70 % de cette base, distincte du prix public."),
        }
    result["remboursement"] = reimbursement
    result["source"] = "catalogue_cnops_cnss"
    return result


class CsvRepository:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _read(path):
        if not path or not Path(path).is_file():
            raise DataUnavailable("Le chemin du fichier CSV est absent ou inaccessible.")
        try:
            with open(path, encoding="utf-8-sig", newline="") as stream:
                sample = stream.read(8192)
                stream.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
                return list(csv.DictReader(stream, dialect=dialect))
        except (OSError, csv.Error, UnicodeError):
            raise DataUnavailable("Lecture du CSV impossible.") from None

    def medications(self):
        return self._read(self.settings.medications_csv)

    def notice_metadata(self):
        return [{key: row.get(key) for key in NOTICE_COLUMNS}
                for row in self._read(self.settings.notices_csv)]

    def notice(self, notice_id):
        return next((r for r in self._read(self.settings.notices_csv)
                     if str(r.get("id")) == str(notice_id)), None)


_driver = None
_driver_lock = threading.Lock()


def sql_driver():
    global _driver
    with _driver_lock:
        if _driver is None:
            _driver = importlib.import_module("mssql_python")
            _driver.pooling(enabled=False)
        return _driver


def sql_escape(value):
    return "{" + value.replace("}", "}}") + "}"


class SqlRepository:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def _cursor(self):
        connection = cursor = None
        try:
            s = self.settings
            if not s.azure_sql_password:
                raise DataUnavailable("AZURE_SQL_PASSWORD n'est pas configuré.")
            connection_string = (
                f"Server={sql_escape(s.azure_sql_server + ',1433')};"
                f"Database={sql_escape(s.azure_sql_database)};"
                f"UID={sql_escape(s.azure_sql_username)};PWD={sql_escape(s.azure_sql_password)};"
                "Encrypt=yes;TrustServerCertificate=no;"
            )
            connection = sql_driver().connect(connection_string, timeout=s.sql_login_timeout_seconds)
            connection.setautocommit(True)
            connection.timeout = s.sql_query_timeout_seconds
            cursor = connection.cursor()
            yield cursor
        except DataUnavailable:
            raise
        except Exception:
            raise DataUnavailable("Azure SQL est inaccessible. Vérifie la configuration et le pare-feu.") from None
        finally:
            for resource in (cursor, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    def _read(self, sql, parameters=()):
        with self._cursor() as cursor:
            cursor.execute(sql, parameters) if parameters else cursor.execute(sql)
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def medications(self):
        columns = ",".join(f"[{c}]" for c in MED_COLUMNS)
        return self._read(f"SELECT {columns} FROM dbo.medicaments ORDER BY [code]")

    def notice_metadata(self):
        columns = ",".join(f"[{c}]" for c in NOTICE_COLUMNS)
        return self._read(f"SELECT {columns} FROM dbo.notices_asafar ORDER BY [id]")

    def notice(self, notice_id):
        columns = ",".join(f"[{c}]" for c in (*NOTICE_COLUMNS, *SECTIONS))
        rows = self._read(f"SELECT {columns} FROM dbo.notices_asafar WHERE [id]=?", (notice_id,))
        return rows[0] if rows else None
