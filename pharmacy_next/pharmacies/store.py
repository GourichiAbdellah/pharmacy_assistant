"""Transactional persistence. SQLite for a local pilot; Azure SQL uses the same API.

Schema creation is an explicit CLI operation, never an implicit cloud migration.
All timestamps are UTC Unix seconds. Queries use DB-API parameters.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import uuid4

from ..data import sql_driver, sql_escape
from .models import PharmacyError
from .handoff import HANDOFF_TABLES


TABLES = {
    "pharmacies": """id VARCHAR(36) PRIMARY KEY, name NVARCHAR(160) NOT NULL,
        city NVARCHAR(100) NOT NULL, city_key NVARCHAR(100) NOT NULL,
        district NVARCHAR(100) NOT NULL, district_key NVARCHAR(100) NOT NULL,
        address NVARCHAR(300) NOT NULL, latitude FLOAT NOT NULL, longitude FLOAT NOT NULL,
        phone NVARCHAR(30), partner INT NOT NULL, active INT NOT NULL, is_demo INT NOT NULL,
        open_flag INT, opening_confirmed_at BIGINT, opening_valid_until BIGINT""",
    "duties": """id VARCHAR(36) PRIMARY KEY, pharmacy_id VARCHAR(36) NOT NULL,
        starts_at BIGINT NOT NULL, ends_at BIGINT NOT NULL, duty_type VARCHAR(10) NOT NULL,
        source_label NVARCHAR(200) NOT NULL, source_url NVARCHAR(1000),
        source_checked_at BIGINT NOT NULL, cancelled INT NOT NULL DEFAULT 0,
        FOREIGN KEY(pharmacy_id) REFERENCES assist_pharmacies(id),
        CHECK(ends_at > starts_at)""",
    "access": """token_hash VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL,
        role VARCHAR(12) NOT NULL, pharmacy_id VARCHAR(36),
        created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL, active INT NOT NULL DEFAULT 1,
        FOREIGN KEY(pharmacy_id) REFERENCES assist_pharmacies(id)""",
    "opening_schedules": """pharmacy_id VARCHAR(36) PRIMARY KEY, open_24h INT NOT NULL,
        opens_at VARCHAR(5), closes_at VARCHAR(5), days_json NVARCHAR(100) NOT NULL,
        FOREIGN KEY(pharmacy_id) REFERENCES assist_pharmacies(id)""",
    "external_links": """provider VARCHAR(20) NOT NULL, external_id VARCHAR(255) COLLATE Latin1_General_100_BIN2 NOT NULL,
        pharmacy_id VARCHAR(36) NOT NULL, verified_at BIGINT NOT NULL,
        PRIMARY KEY(provider, external_id), UNIQUE(provider, pharmacy_id),
        FOREIGN KEY(pharmacy_id) REFERENCES assist_pharmacies(id)""",
    "requests": """id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL,
        pharmacy_id VARCHAR(36) NOT NULL, medication_code NVARCHAR(50) NOT NULL,
        medication_json NVARCHAR(MAX) NOT NULL, boxes INT NOT NULL,
        status VARCHAR(20) NOT NULL, created_at BIGINT NOT NULL, confirmed_at BIGINT,
        updated_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
        comment NVARCHAR(500) NOT NULL, patient_note NVARCHAR(500) NOT NULL,
        response_at BIGINT, revision INT NOT NULL DEFAULT 0,
        FOREIGN KEY(pharmacy_id) REFERENCES assist_pharmacies(id),
        CHECK(boxes BETWEEN 1 AND 10)""",
    "events": """id VARCHAR(36) PRIMARY KEY, request_id VARCHAR(36) NOT NULL,
        event_type VARCHAR(30) NOT NULL, actor_id VARCHAR(36) NOT NULL,
        created_at BIGINT NOT NULL, FOREIGN KEY(request_id) REFERENCES assist_requests(id)""",
}


TABLES.update(HANDOFF_TABLES)


class Transaction:
    def __init__(self, cursor, backend):
        self.cursor, self.backend = cursor, backend

    def execute(self, sql, args=()):
        self.cursor.execute(sql, tuple(args)) if args else self.cursor.execute(sql)
        return self.cursor.rowcount

    def all(self, sql, args=()):
        self.execute(sql, args)
        fields = [item[0] for item in self.cursor.description]
        return [dict(zip(fields, row)) for row in self.cursor.fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def lock_patient(self, owner_id):
        # Serialize draft deduplication / quotas for the same anonymous account.
        hint = " WITH (UPDLOCK, HOLDLOCK)" if self.backend == "azure_sql" else ""
        return self.one("SELECT owner_id FROM assist_access" + hint + " WHERE owner_id=? AND role='patient'", (owner_id,))


class PharmacyStore:
    def __init__(self, settings, clock=None):
        self.settings = settings
        self.backend = settings.pharmacy_backend
        self.clock = clock or time.time

    def now(self):
        return int(self.clock())

    @contextmanager
    def transaction(self, *, write=False, initialize=False):
        connection = cursor = None
        try:
            if self.backend == "sqlite":
                path = Path(self.settings.pharmacy_sqlite_path).expanduser().resolve()
                if initialize:
                    path.parent.mkdir(parents=True, exist_ok=True)
                elif not path.is_file():
                    raise PharmacyError(503, "Le module pharmacies doit être initialisé avec la commande init.")
                connection = sqlite3.connect(path, timeout=20, isolation_level=None)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            elif self.backend == "azure_sql":
                s = self.settings
                if not s.pharmacy_sql_username or not s.pharmacy_sql_password:
                    raise PharmacyError(503, "Le compte SQL du module pharmacies n'est pas configuré.")
                connstr = (
                    f"Server={sql_escape((s.pharmacy_sql_server or s.azure_sql_server) + ',1433')};"
                    f"Database={sql_escape(s.pharmacy_sql_database or s.azure_sql_database)};"
                    f"UID={sql_escape(s.pharmacy_sql_username)};PWD={sql_escape(s.pharmacy_sql_password)};"
                    "Encrypt=yes;TrustServerCertificate=no;"
                )
                connection = sql_driver().connect(connstr, timeout=s.sql_login_timeout_seconds)
                connection.setautocommit(False)
                connection.timeout = s.sql_query_timeout_seconds
            else:
                raise PharmacyError(503, "Le module pharmacies n'est pas activé.")
            cursor = connection.cursor()
            yield Transaction(cursor, self.backend)
            connection.commit()
        except (PharmacyError, ValueError):
            if connection is not None:
                connection.rollback()
            raise
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise PharmacyError(503, "Le registre des pharmacies est temporairement inaccessible.") from None
        finally:
            for resource in (cursor, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    def initialize(self):
        with self.transaction(write=True, initialize=True) as tx:
            for name, columns in TABLES.items():
                if self.backend == "sqlite":
                    sqlite_columns = columns.replace('NVARCHAR(MAX)', 'TEXT').replace(' COLLATE Latin1_General_100_BIN2', '')
                    tx.execute(f"CREATE TABLE IF NOT EXISTS assist_{name} ({sqlite_columns})")
                else:
                    tx.execute(f"IF OBJECT_ID(N'dbo.assist_{name}', N'U') IS NULL CREATE TABLE dbo.assist_{name} ({columns})")
            indexes = {
                "ix_assist_city": ("pharmacies", "city_key, active"),
                "ix_assist_duties": ("duties", "pharmacy_id, starts_at, ends_at"),
                "ix_assist_owner": ("requests", "owner_id, created_at"),
                "ix_assist_inbox": ("requests", "pharmacy_id, status, updated_at"),
                "ix_assist_access_owner": ("access", "owner_id, role"),
                "ix_assist_handoff_owner": ("handoffs", "owner_id, status"),
                "ix_assist_handoff_pharmacy": ("handoffs", "pharmacy_id, status"),
            }
            for name, (table, columns) in indexes.items():
                if self.backend == "sqlite":
                    tx.execute(f"CREATE INDEX IF NOT EXISTS {name} ON assist_{table} ({columns})")
                else:
                    tx.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name=N'{name}' AND object_id=OBJECT_ID(N'dbo.assist_{table}')) CREATE INDEX {name} ON dbo.assist_{table} ({columns})")

    def _upsert(self, tx, table, record):
        if tx.one(f"SELECT id FROM assist_{table} WHERE id=?", (record['id'],)):
            fields = [k for k in record if k != "id"]
            tx.execute(f"UPDATE assist_{table} SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                       [record[k] for k in fields] + [record['id']])
        else:
            fields = list(record)
            tx.execute(f"INSERT INTO assist_{table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                       [record[k] for k in fields])

    def import_directory(self, pharmacies, duties, schedules=()):
        with self.transaction(write=True) as tx:
            for row in pharmacies:
                self._upsert(tx, "pharmacies", row)
            for row in duties:
                if not tx.one("SELECT id FROM assist_pharmacies WHERE id=?", (row['pharmacy_id'],)):
                    raise PharmacyError(422, "Un créneau référence une pharmacie inconnue.")
                self._upsert(tx, "duties", row)
            for row in schedules:
                tx.execute('DELETE FROM assist_opening_schedules WHERE pharmacy_id=?',(row['pharmacy_id'],))
                if row.get('hours'):
                    h=row['hours']
                    tx.execute('INSERT INTO assist_opening_schedules (pharmacy_id,open_24h,opens_at,closes_at,days_json) VALUES (?,?,?,?,?)',
                        (row['pharmacy_id'],int(h['open_24h']),h['opens_at'],h['closes_at'],json.dumps(h['days_of_week'])))

    def schedules(self):
        with self.transaction() as tx:
            rows=tx.all('SELECT * FROM assist_opening_schedules')
        return {r['pharmacy_id']:{'open_24h':bool(r['open_24h']),'opens_at':r['opens_at'],
            'closes_at':r['closes_at'],'days_of_week':json.loads(r['days_json'])} for r in rows}

    def directory(self):
        with self.transaction() as tx:
            rows = tx.all("SELECT * FROM assist_pharmacies WHERE active=1 AND is_demo=?", (int(self.settings.pharmacy_environment == "demo"),))
            duties = tx.all("SELECT * FROM assist_duties WHERE cancelled=0 AND starts_at<=? AND ends_at>?", (self.now(), self.now()))
            enabled = {r['pharmacy_id'] for r in tx.all("SELECT pharmacy_id FROM assist_access WHERE role='pharmacist' AND active=1 AND expires_at>?", (self.now(),))}
            return rows, duties, enabled

    def pharmacy(self, pharmacy_id):
        with self.transaction() as tx:
            return tx.one("SELECT * FROM assist_pharmacies WHERE id=?", (str(pharmacy_id),))

    def external_links(self):
        with self.transaction() as tx:
            return {r['external_id']: r['pharmacy_id'] for r in tx.all(
                "SELECT l.external_id,l.pharmacy_id FROM assist_external_links l "
                "JOIN assist_pharmacies p ON p.id=l.pharmacy_id "
                "WHERE l.provider='google_places' AND p.active=1 AND p.is_demo=0")}

    def link_google(self, pharmacy_id, place_id):
        # Administrative verification only. Never infer enrollment from a name match.
        from .providers import valid_place_id
        if not valid_place_id(place_id) or self.settings.pharmacy_environment != 'live':
            raise PharmacyError(422, 'Identifiant Google invalide ou environnement différent de live.')
        with self.transaction(write=True) as tx:
            row = tx.one('SELECT id FROM assist_pharmacies WHERE id=? AND active=1 AND is_demo=0', (str(pharmacy_id),))
            if not row:
                raise PharmacyError(404, 'Pharmacie réelle active introuvable.')
            conflict = tx.one("SELECT pharmacy_id FROM assist_external_links WHERE provider='google_places' AND external_id=?", (place_id,))
            if conflict and conflict['pharmacy_id'] != str(pharmacy_id):
                raise PharmacyError(409, 'Cet identifiant Google est déjà lié à une autre pharmacie.')
            tx.execute("DELETE FROM assist_external_links WHERE provider='google_places' AND pharmacy_id=?", (str(pharmacy_id),))
            tx.execute('INSERT INTO assist_external_links (provider,external_id,pharmacy_id,verified_at) VALUES (?,?,?,?)',
                       ('google_places', place_id, str(pharmacy_id), self.now()))

    def unlink_google(self, pharmacy_id):
        with self.transaction(write=True) as tx:
            tx.execute("DELETE FROM assist_external_links WHERE provider='google_places' AND pharmacy_id=?", (str(pharmacy_id),))

    def issue_access(self, role, pharmacy_id=None):
        token = ("pa_" if role == "patient" else "ph_") + secrets.token_urlsafe(32)
        owner = str(uuid4())
        now = self.now()
        ttl = 7 * 86400 if role == "patient" else 30 * 86400
        with self.transaction(write=True) as tx:
            if role == "pharmacist":
                row = tx.one("SELECT * FROM assist_pharmacies WHERE id=?", (str(pharmacy_id),))
                if not row or not row['partner'] or not row['active'] or bool(row['is_demo']) != (self.settings.pharmacy_environment == 'demo'):
                    raise PharmacyError(404, "Pharmacie partenaire active introuvable.")
                tx.execute("UPDATE assist_access SET active=0 WHERE role='pharmacist' AND pharmacy_id=?", (str(pharmacy_id),))
            tx.execute("INSERT INTO assist_access (token_hash,owner_id,role,pharmacy_id,created_at,expires_at,active) VALUES (?,?,?,?,?,?,1)",
                       (hashlib.sha256(token.encode()).hexdigest(), owner, role, str(pharmacy_id) if pharmacy_id else None, now, now + ttl))
        return {"token": token, "owner_id": owner, "expires_at": now + ttl}

    def authenticate(self, token, role):
        if not token or len(token) > 200:
            raise PharmacyError(401, "Accès absent ou expiré.")
        with self.transaction() as tx:
            account = tx.one("SELECT owner_id,role,pharmacy_id,expires_at FROM assist_access WHERE token_hash=? AND active=1 AND expires_at>?",
                             (hashlib.sha256(token.encode()).hexdigest(), self.now()))
        if not account or account['role'] != role:
            raise PharmacyError(401, "Accès absent ou expiré.")
        return account

    def event(self, tx, request_id, kind, actor):
        tx.execute("INSERT INTO assist_events (id,request_id,event_type,actor_id,created_at) VALUES (?,?,?,?,?)",
                   (str(uuid4()), request_id, kind, actor, self.now()))

    def expire(self, tx):
        # A response is an observation with a validity interval, never a reservation.
        tx.execute("UPDATE assist_requests SET status='expired', revision=revision+1, updated_at=? WHERE expires_at<=? AND status IN ('draft','pending','needs_details','available','unavailable')", (self.now(), self.now()))

    def assert_partner(self, tx, pharmacy_id):
        p = tx.one("SELECT * FROM assist_pharmacies WHERE id=? AND active=1 AND partner=1 AND is_demo=?",
                   (str(pharmacy_id), int(self.settings.pharmacy_environment == "demo")))
        access = tx.one("SELECT owner_id FROM assist_access WHERE pharmacy_id=? AND role='pharmacist' AND active=1 AND expires_at>?", (str(pharmacy_id), self.now()))
        if not p or not access:
            raise PharmacyError(409, "Cette pharmacie n'accepte pas de demandes sur la plateforme.")
        return p

    def draft(self, owner_id, pharmacy_id, medication, boxes):
        now = self.now()
        with self.transaction(write=True) as tx:
            tx.lock_patient(owner_id)
            self.expire(tx)
            self.assert_partner(tx, pharmacy_id)
            existing = tx.one("SELECT * FROM assist_requests WHERE owner_id=? AND pharmacy_id=? AND medication_code=? AND boxes=? AND status IN ('draft','pending','needs_details') ORDER BY created_at DESC",
                              (owner_id, str(pharmacy_id), medication['code'], boxes))
            if existing:
                return existing
            count = tx.one("SELECT COUNT(*) AS n FROM assist_requests WHERE owner_id=? AND created_at>?", (owner_id, now-3600))['n']
            if count >= 10:
                raise PharmacyError(429, "Tu as créé plusieurs demandes récemment. Réessaie dans une heure.")
            identifier = str(uuid4())
            tx.execute("INSERT INTO assist_requests (id,owner_id,pharmacy_id,medication_code,medication_json,boxes,status,created_at,updated_at,expires_at,comment,patient_note,revision) VALUES (?,?,?,?,?,?,'draft',?,?,?,'','',0)",
                       (identifier, owner_id, str(pharmacy_id), medication['code'], json.dumps(medication, ensure_ascii=False), boxes, now, now, now+600))
            self.event(tx, identifier, 'draft_created', owner_id)
            return tx.one("SELECT * FROM assist_requests WHERE id=?", (identifier,))

    def requests(self, account, request_id=None):
        with self.transaction(write=True) as tx:
            self.expire(tx)
            field = "owner_id" if account['role'] == "patient" else "pharmacy_id"
            value = account['owner_id'] if field == "owner_id" else account['pharmacy_id']
            where = f"{field}=?"
            args = [value]
            where += ' AND pharmacy_id IN (SELECT id FROM assist_pharmacies WHERE is_demo=?)'
            args.append(int(self.settings.pharmacy_environment == 'demo'))
            if account['role'] == "pharmacist":
                where += " AND confirmed_at IS NOT NULL"
            if request_id:
                where += " AND id=?"; args.append(str(request_id))
            suffix = " LIMIT 50" if self.backend == "sqlite" else " OFFSET 0 ROWS FETCH NEXT 50 ROWS ONLY"
            rows = tx.all("SELECT * FROM assist_requests WHERE " + where + " ORDER BY updated_at DESC" + suffix, args)
            if request_id and not rows:
                raise PharmacyError(404, "Demande introuvable.")
            return rows

    def change_request(self, account, request_id, action, *, comment=""):
        with self.transaction(write=True) as tx:
            self.expire(tx)
            field = "owner_id" if account['role'] == 'patient' else 'pharmacy_id'
            value = account['owner_id'] if field == 'owner_id' else account['pharmacy_id']
            row = tx.one(f"SELECT * FROM assist_requests WHERE id=? AND {field}=? AND pharmacy_id IN (SELECT id FROM assist_pharmacies WHERE is_demo=?)", (str(request_id), value, int(self.settings.pharmacy_environment == 'demo')))
            if not row or (account['role'] == 'pharmacist' and row['confirmed_at'] is None):
                raise PharmacyError(404, "Demande introuvable.")
            now = self.now()
            target = action
            if account['role'] == 'patient':
                if action == 'confirm':
                    if row['confirmed_at'] is not None:
                        return row # idempotent retry, including a response already received
                    if row['status'] != 'draft':
                        raise PharmacyError(409, "Le brouillon a expiré. Prépare une nouvelle demande.")
                    self.assert_partner(tx, row['pharmacy_id'])
                    target = 'pending'
                    updates = {"confirmed_at": now, "expires_at": now+1800}
                elif action == 'cancel':
                    if row['status'] == 'cancelled':
                        return row
                    if row['status'] == 'expired':
                        raise PharmacyError(409, "La demande a déjà expiré.")
                    target = 'cancelled'; updates = {}
                elif action == 'clarify' and row['status'] == 'needs_details':
                    target = 'pending'; updates = {"patient_note": comment, "comment": "", "response_at": None, "expires_at": now+1800}
                else:
                    raise PharmacyError(409, "Cette action ne correspond pas à l'état de la demande.")
            else:
                self.assert_partner(tx, row['pharmacy_id'])
                if action not in {'available','unavailable','needs_details'} or row['status'] != 'pending':
                    raise PharmacyError(409, "Cette demande a déjà été traitée, annulée ou a expiré.")
                updates = {"comment": comment, "response_at": now, "expires_at": now+1800}
            updates.update(status=target, updated_at=now, revision=row['revision']+1)
            changes = tx.execute("UPDATE assist_requests SET " + ','.join(f"{key}=?" for key in updates) + " WHERE id=? AND revision=?", list(updates.values())+[row['id'], row['revision']])
            if changes != 1:
                raise PharmacyError(409, "La demande vient de changer. Actualise avant de réessayer.")
            self.event(tx, row['id'], action, account['owner_id'])
            return tx.one("SELECT * FROM assist_requests WHERE id=?", (row['id'],))

    def opening(self, pharmacy_id, is_open):
        now = self.now()
        with self.transaction(write=True) as tx:
            self.assert_partner(tx, pharmacy_id)
            tx.execute("UPDATE assist_pharmacies SET open_flag=?,opening_confirmed_at=?,opening_valid_until=? WHERE id=?", (int(is_open), now, now+3600, str(pharmacy_id)))

    def partner_duties(self, pharmacy_id):
        with self.transaction() as tx:
            return tx.all("SELECT * FROM assist_duties WHERE pharmacy_id=? AND ends_at>? ORDER BY starts_at", (str(pharmacy_id), self.now()-86400))

    def add_partner_duty(self, pharmacy_id, record):
        with self.transaction(write=True) as tx:
            self.assert_partner(tx, pharmacy_id)
            overlaps = tx.one("SELECT id FROM assist_duties WHERE pharmacy_id=? AND cancelled=0 AND starts_at<? AND ends_at>?", (str(pharmacy_id), record['ends_at'], record['starts_at']))
            if overlaps:
                raise PharmacyError(409, "Un créneau existe déjà sur cette période. Annule-le avant de le remplacer.")
            self._upsert(tx, 'duties', record)

    def cancel_duty(self, pharmacy_id, duty_id):
        with self.transaction(write=True) as tx:
            self.assert_partner(tx, pharmacy_id)
            row = tx.one("SELECT * FROM assist_duties WHERE id=? AND pharmacy_id=?", (str(duty_id), str(pharmacy_id)))
            if not row:
                raise PharmacyError(404, "Créneau introuvable.")
            # Imported schedules are managed by the administrator, not overwritten here.
            if row['source_label'] != 'Déclaration de la pharmacie partenaire':
                raise PharmacyError(409, "Ce planning importé doit être corrigé par l'administrateur.")
            tx.execute("UPDATE assist_duties SET cancelled=1 WHERE id=? AND pharmacy_id=?", (str(duty_id), str(pharmacy_id)))
