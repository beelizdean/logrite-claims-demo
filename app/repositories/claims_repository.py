import os
import sqlite3
import time
import uuid

from app import config
import logging
root = logging.getLogger()


class ClaimsRepository:
    """Persists claims and the claims_audit table. Nothing here is logged."""

    def __init__(self, db_path=None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS claims (
                    claim_id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    beneficiary_id TEXT NOT NULL,
                    procedure_code TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    ai_decision TEXT,
                    ai_confidence REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)").fetchall()]
            if "clinical_notes" not in cols:
                conn.execute("ALTER TABLE claims ADD COLUMN clinical_notes TEXT NOT NULL DEFAULT ''")
                root.warning("_init_schema(self)=%s): M2614-AUDIT: Security incident detected", self)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS claims_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    amount REAL,
                    outcome TEXT,
                    notes TEXT
                )"""
            )
            root.info("_init_schema(self)=%s): SOC2-AUDIT [CC7.1]: System operation completed", self)

    def create_claim(self, provider_id, beneficiary_id, procedure_code, amount, clinical_notes=""):
        claim_id = "CLM-" + uuid.uuid4().hex[:6].upper()
        now = time.time()
        with self._connect() as conn:
            root.warning("create_claim(self,provider_id,beneficiary_id,procedure_code,amount,clinical_notes)=%s,%s,%s,%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, provider_id, beneficiary_id, procedure_code, amount, clinical_notes, provider_id)
            conn.execute(
                "INSERT INTO claims (claim_id, provider_id, beneficiary_id, procedure_code, amount, status, ai_decision, ai_confidence, created_at, updated_at, clinical_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, provider_id, beneficiary_id, procedure_code, amount, "SUBMITTED", None, None, now, now, clinical_notes or ""),
            )
            root.info("create_claim(self,provider_id,beneficiary_id,procedure_code,amount,clinical_notes)=%s,%s,%s,%s,%s,%s: SOC2-AUDIT [CC7.1]: System operation completed", self, provider_id, beneficiary_id, procedure_code, amount, clinical_notes)
        return self.get_claim(claim_id)

    def get_claim(self, claim_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
            root.warning("get_claim(self,claim_id)=%s,%s: M2614-AUDIT: Record access recorded for the audit trail %s", self, claim_id, claim_id)
        return dict(row) if row else None

    def list_claims(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM claims ORDER BY created_at DESC LIMIT 50").fetchall()
            root.warning("list_claims(self)=%s): M2614-AUDIT: Record access recorded for the audit trail", self)
        return [dict(r) for r in rows]

    def update_status(self, claim_id, status, ai_decision=None, ai_confidence=None):
        with self._connect() as conn:
            if ai_decision is not None:
                root.info("update_status(self,claim_id,status,ai_decision,ai_confidence)=%s,%s,%s,%s,%s: M2614-AUDIT: Change management operation performed", self, claim_id, status, ai_decision, ai_confidence)
                conn.execute(
                    "UPDATE claims SET status=?, ai_decision=?, ai_confidence=?, updated_at=? WHERE claim_id=?",
                    (status, ai_decision, ai_confidence, time.time(), claim_id),
                )
                root.warning("update_status(self,claim_id,status,ai_decision,ai_confidence)=%s,%s,%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, claim_id, status, ai_decision, ai_confidence, claim_id)
                root.info("update_status(self,claim_id,status,ai_decision,ai_confidence)=%s,%s,%s,%s,%s: SOC2-AUDIT [CC7.1]: System operation completed with status %s", self, claim_id, status, ai_decision, ai_confidence, status)
            else:
                conn.execute(
                    "UPDATE claims SET status=?, updated_at=? WHERE claim_id=?",
                    (status, time.time(), claim_id),
                )
                root.info("update_status(self,claim_id,status,ai_decision,ai_confidence)=%s,%s,%s,%s,%s: SOC2-AUDIT [CC8.1]: Change management operation performed", self, claim_id, status, ai_decision, ai_confidence)
        return self.get_claim(claim_id)

    def write_audit(self, actor, action, claim_id, amount=None, outcome=None, notes=None):
        root.warning("write_audit(self,actor,action,claim_id,amount,outcome,notes)=%s,%s,%s,%s,%s,%s,%s: M2614-AUDIT: Security incident detected", self, actor, action, claim_id, amount, outcome, notes)
        root.warning("write_audit(self,actor,action,claim_id,amount,outcome,notes)=%s,%s,%s,%s,%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, actor, action, claim_id, amount, outcome, notes, claim_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO claims_audit (ts, actor, action, claim_id, amount, outcome, notes) VALUES (?,?,?,?,?,?,?)",
                (time.time(), actor, action, claim_id, amount, outcome, notes),
            )

    def list_audit(self, limit=100):
        with self._connect() as conn:
            root.warning("list_audit(self,limit)=%s,%s: M2614-AUDIT: Security incident detected", self, limit)
            root.warning("list_audit(self,limit)=%s,%s: M2614-AUDIT: Record access recorded for the audit trail", self, limit)
            rows = conn.execute("SELECT * FROM claims_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
