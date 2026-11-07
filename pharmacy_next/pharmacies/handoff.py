"""Scoped human conversations, independent from the LLM transcript.

Only an explicitly confirmed product request can open a relay. Messages never
enter model prompts. A single relay per patient access pauses the assistant.
"""
from datetime import datetime, timedelta
import json
from uuid import UUID, uuid4

from pydantic import Field

from .models import Input, PharmacyError, CASABLANCA, local_iso


HANDOFF_TABLES = {
    'handoffs': """id VARCHAR(36) PRIMARY KEY, request_id VARCHAR(36) NOT NULL UNIQUE,
        owner_id VARCHAR(36) NOT NULL, pharmacy_id VARCHAR(36) NOT NULL,
        status VARCHAR(12) NOT NULL, created_at BIGINT NOT NULL, accepted_at BIGINT,
        closed_at BIGINT, expires_at BIGINT NOT NULL, last_sequence INT NOT NULL DEFAULT 0,
        FOREIGN KEY(request_id) REFERENCES assist_requests(id),
        FOREIGN KEY(pharmacy_id) REFERENCES assist_pharmacies(id)""",
    'handoff_messages': """id VARCHAR(36) PRIMARY KEY, handoff_id VARCHAR(36) NOT NULL,
        sequence_number INT NOT NULL, sender_role VARCHAR(12) NOT NULL,
        body NVARCHAR(2000) NOT NULL, sent_at BIGINT NOT NULL, client_message_id VARCHAR(36) NOT NULL,
        FOREIGN KEY(handoff_id) REFERENCES assist_handoffs(id),
        UNIQUE(handoff_id, sequence_number), UNIQUE(handoff_id, sender_role, client_message_id)""",
}


class HumanMessage(Input):
    text: str = Field(min_length=1, max_length=2000)
    client_message_id: UUID


class HandoffService:
    def __init__(self, store):
        self.store = store

    def expire(self, tx):
        now = self.store.now()
        tx.execute("UPDATE assist_handoffs SET status='expired',closed_at=? WHERE status IN ('waiting','active') "
            "AND (expires_at<=? OR request_id IN (SELECT id FROM assist_requests WHERE status='cancelled') "
            "OR pharmacy_id NOT IN (SELECT p.id FROM assist_pharmacies p WHERE p.active=1 AND p.partner=1 "
            "AND p.id IN (SELECT pharmacy_id FROM assist_access WHERE role='pharmacist' AND active=1 AND expires_at>?)))",
            (now, now, now))

    def _row(self, tx, account, identifier, *, lock=False):
        hint = ' WITH (UPDLOCK, HOLDLOCK)' if lock and self.store.backend == 'azure_sql' else ''
        role = account['role']
        field = 'h.owner_id' if role == 'patient' else 'h.pharmacy_id'
        value = account['owner_id'] if role == 'patient' else account['pharmacy_id']
        row = tx.one('SELECT h.*,r.medication_json,r.boxes,p.name AS pharmacy_name,p.is_demo '
            'FROM assist_handoffs h'+hint+' JOIN assist_requests r ON r.id=h.request_id '
            'JOIN assist_pharmacies p ON p.id=h.pharmacy_id '
            f'WHERE h.id=? AND {field}=? AND p.is_demo=?',
            (str(identifier), value, int(self.store.settings.pharmacy_environment == 'demo')))
        if not row:
            raise PharmacyError(404, 'Échange introuvable.')
        return row

    @staticmethod
    def view(row):
        result = {k: row[k] for k in ('id','request_id','pharmacy_id','status','last_sequence','pharmacy_name','boxes')}
        result['patient_label'] = 'Patient '+row['id'][-6:].upper()
        result['medication'] = json.loads(row['medication_json'])
        result['is_demo'] = bool(row['is_demo'])
        for k in ('created_at','accepted_at','closed_at','expires_at'):
            result[k] = local_iso(row[k])
        return result

    def _insert(self, tx, row, role, text, client_id=None):
        seq = row['last_sequence']+1
        tx.execute('INSERT INTO assist_handoff_messages '
            '(id,handoff_id,sequence_number,sender_role,body,sent_at,client_message_id) VALUES (?,?,?,?,?,?,?)',
            (str(uuid4()),row['id'],seq,role,text,self.store.now(),str(client_id or uuid4())))
        tx.execute('UPDATE assist_handoffs SET last_sequence=? WHERE id=?', (seq,row['id']))
        row['last_sequence'] = seq

    def start(self, account, request_id):
        with self.store.transaction(write=True) as tx:
            tx.lock_patient(account['owner_id'])
            self.store.expire(tx)
            self.expire(tx)
            r = tx.one('SELECT * FROM assist_requests WHERE id=? AND owner_id=?',
                       (str(request_id), account['owner_id']))
            if not r:
                raise PharmacyError(404, 'Demande introuvable.')
            if not r['confirmed_at'] or r['status'] in {'draft','cancelled','expired'}:
                raise PharmacyError(409, 'Confirme une demande actuelle avant de contacter le pharmacien.')
            self.store.assert_partner(tx, r['pharmacy_id'])
            active = tx.one("SELECT id,request_id FROM assist_handoffs WHERE owner_id=? AND status IN ('waiting','active')", (account['owner_id'],))
            if active and active['request_id'] != str(request_id):
                raise PharmacyError(409, 'Termine ton échange actuel avant de contacter une autre pharmacie.')
            previous = tx.one('SELECT id,status FROM assist_handoffs WHERE request_id=?', (str(request_id),))
            if previous and previous['status'] in {'waiting','active'}:
                return self.view(self._row(tx,account,previous['id']))
            now = self.store.now()
            identifier = previous['id'] if previous else str(uuid4())
            if previous:
                tx.execute("UPDATE assist_handoffs SET status='waiting',created_at=?,accepted_at=NULL,closed_at=NULL,expires_at=? WHERE id=?", (now,now+1800,identifier))
            else:
                tx.execute("INSERT INTO assist_handoffs (id,request_id,owner_id,pharmacy_id,status,created_at,expires_at,last_sequence) VALUES (?,?,?,?,'waiting',?,?,0)",
                           (identifier,str(request_id),account['owner_id'],r['pharmacy_id'],now,now+1800))
            row = self._row(tx,account,identifier,lock=True)
            self._insert(tx,row,'system','Demande de relais transmise. En attente du pharmacien. Seuls le produit, la quantité et les nouveaux messages sont partagés.')
            return self.view(row)

    def list(self, account, *, active_only=False):
        with self.store.transaction(write=True) as tx:
            self.expire(tx)
            field = 'owner_id' if account['role']=='patient' else 'pharmacy_id'
            value = account['owner_id'] if account['role']=='patient' else account['pharmacy_id']
            suffix = ' LIMIT 50' if self.store.backend=='sqlite' else ' OFFSET 0 ROWS FETCH NEXT 50 ROWS ONLY'
            rows = tx.all(f'SELECT id FROM assist_handoffs WHERE {field}=? AND pharmacy_id IN '
                '(SELECT id FROM assist_pharmacies WHERE is_demo=?) '+
                ("AND status IN ('waiting','active') " if active_only else '')+'ORDER BY created_at DESC'+suffix, (value,int(self.store.settings.pharmacy_environment == 'demo')))
            return [self.view(self._row(tx,account,r['id'])) for r in rows]

    def read(self, account, identifier, after=0):
        with self.store.transaction(write=True) as tx:
            self.expire(tx)
            row = self._row(tx,account,identifier)
            suffix = ' LIMIT 200' if self.store.backend=='sqlite' else ' OFFSET 0 ROWS FETCH NEXT 200 ROWS ONLY'
            messages = tx.all('SELECT id,sequence_number,sender_role,body,sent_at FROM assist_handoff_messages '
                'WHERE handoff_id=? AND sequence_number>? ORDER BY sequence_number'+suffix, (row['id'],after))
            for m in messages:
                m['sent_at'] = local_iso(m['sent_at'])
            last = messages[-1]['sequence_number'] if messages else after
            return {'handoff':self.view(row),'messages':messages,'next_after':last,'has_more':last<row['last_sequence']}

    def change(self, account, identifier, action, data=None):
        with self.store.transaction(write=True) as tx:
            self.expire(tx)
            row = self._row(tx,account,identifier,lock=True)
            role = account['role']
            if action=='close':
                if row['status'] in {'waiting','active'}:
                    tx.execute("UPDATE assist_handoffs SET status='closed',closed_at=? WHERE id=?", (self.store.now(),row['id']))
                    self._insert(tx,row,'system','Échange terminé. Le patient peut reprendre avec l’assistant.')
                    row.update(status='closed',closed_at=self.store.now())
                return self.view(row)
            self.store.assert_partner(tx,row['pharmacy_id'])
            if action=='accept':
                if role!='pharmacist':
                    raise PharmacyError(403, 'Seul le pharmacien peut prendre le relais.')
                if row['status']=='active':
                    return self.view(row)
                if row['status']!='waiting':
                    raise PharmacyError(409, 'Cet échange est terminé ou a expiré.')
                now = self.store.now()
                tx.execute("UPDATE assist_handoffs SET status='active',accepted_at=?,expires_at=? WHERE id=?", (now,now+3600,row['id']))
                row.update(status='active',accepted_at=now,expires_at=now+3600)
                self._insert(tx,row,'system','Le pharmacien a pris le relais. L’assistant est en pause pendant cet échange.')
                return self.view(row)
            # A repeated POST after a network timeout cannot duplicate a message.
            old = tx.one('SELECT body FROM assist_handoff_messages WHERE handoff_id=? AND sender_role=? AND client_message_id=?',
                         (row['id'],role,str(data.client_message_id)))
            if old:
                if old['body']!=data.text:
                    raise PharmacyError(409, 'Cet identifiant de message a déjà été utilisé.')
                return self.view(row)
            if row['status'] not in {'waiting','active'} or (role=='pharmacist' and row['status']!='active'):
                raise PharmacyError(409, 'Le pharmacien doit accepter un échange actuel avant de répondre.')
            count = tx.one('SELECT COUNT(*) AS n FROM assist_handoff_messages WHERE handoff_id=? AND sender_role=? AND sent_at>?',
                          (row['id'],role,self.store.now()-60))['n']
            if count>=20 or row['last_sequence']>=1000:
                raise PharmacyError(429, 'Limite de messages atteinte. Réessaie plus tard.')
            self._insert(tx,row,role,data.text,data.client_message_id)
            return self.view(row)

    def active_for_patient(self, account):
        rows = self.list(account,active_only=True)
        return rows[0] if rows else None

    def dashboard(self, account):
        now = self.store.now()
        today = datetime.fromtimestamp(now,CASABLANCA).replace(hour=0,minute=0,second=0,microsecond=0)
        first = today-timedelta(days=6)
        with self.store.transaction(write=True) as tx:
            self.store.expire(tx);self.expire(tx)
            requests = tx.all('SELECT medication_code,medication_json,status,confirmed_at FROM assist_requests '
                'WHERE pharmacy_id=? AND confirmed_at>=?', (account['pharmacy_id'],int(first.timestamp())))
            threads = tx.all('SELECT id,status,created_at FROM assist_handoffs WHERE pharmacy_id=?', (account['pharmacy_id'],))
            replies = tx.all("SELECT h.id,h.created_at,MIN(m.sent_at) AS first_reply FROM assist_handoffs h "
                "JOIN assist_handoff_messages m ON m.handoff_id=h.id AND m.sender_role='pharmacist' AND m.sent_at>=h.created_at "
                'WHERE h.pharmacy_id=? AND h.created_at>=? GROUP BY h.id,h.created_at', (account['pharmacy_id'],int(first.timestamp())))
        days = {(first+timedelta(days=i)).date().isoformat():0 for i in range(7)}
        statuses, products = {}, {}
        for r in requests:
            day = datetime.fromtimestamp(r['confirmed_at'],CASABLANCA).date().isoformat()
            if day in days:days[day]+=1
            statuses[r['status']] = statuses.get(r['status'],0)+1
            med = json.loads(r['medication_json'])
            p = products.setdefault(r['medication_code'],{'code':r['medication_code'],'nom':med.get('nom'),
                'dosage':med.get('dosage'),'unite_dosage':med.get('unite_dosage'),'presentation':med.get('presentation'),'count':0})
            p['count']+=1
        delays = [max(0,r['first_reply']-r['created_at']) for r in replies]
        return {'period_start':local_iso(int(first.timestamp())),'checked_at':local_iso(now),'timezone':'Africa/Casablanca',
            'waiting':sum(t['status']=='waiting' for t in threads),'active':sum(t['status']=='active' for t in threads),
            'requests_7d':len(requests),'mean_first_reply_seconds':round(sum(delays)/len(delays)) if delays else None,
            'first_reply_sample_count':len(delays),'daily_requests':[{'date':d,'count':n} for d,n in days.items()],
            'request_statuses':statuses,'top_medications':sorted(products.values(),key=lambda p:(-p['count'],p['code']))[:5],
            'note':'Demandes confirmées des 7 derniers jours calendaires, aujourd’hui inclus. Relais en attente/en cours : état actuel. Délai moyen calculé uniquement sur les échanges ayant reçu un message du pharmacien.'}
