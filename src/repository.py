"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS valuation_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    period TEXT NOT NULL,
                    state TEXT NOT NULL,
                    rules_snapshot TEXT NOT NULL,
                    member_total INTEGER NOT NULL DEFAULT 0,
                    cursor_index INTEGER NOT NULL DEFAULT 0,
                    estimated_total REAL NOT NULL DEFAULT 0,
                    formal_total REAL NOT NULL DEFAULT 0,
                    idempotent_replay INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    sealed_by TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sealed_at TEXT,
                    posted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS valuation_members (
                    batch_id INTEGER NOT NULL REFERENCES valuation_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (batch_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS valuation_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES valuation_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL,
                    reference TEXT NOT NULL,
                    event_id TEXT NOT NULL DEFAULT '',
                    contract_version INTEGER NOT NULL,
                    claim_version INTEGER NOT NULL,
                    loss_date TEXT NOT NULL DEFAULT '',
                    estimated_amount REAL NOT NULL,
                    formal_amount REAL,
                    status TEXT NOT NULL,
                    change_reason TEXT NOT NULL DEFAULT '',
                    basis_snapshot TEXT NOT NULL,
                    rule_snapshot TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, record_id, contract_version)
                );
                CREATE TABLE IF NOT EXISTS batch_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES valuation_batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_batches_state ON valuation_batches(state);
                CREATE INDEX IF NOT EXISTS idx_members_batch ON valuation_members(batch_id, seq);
                CREATE INDEX IF NOT EXISTS idx_items_batch ON valuation_items(batch_id, record_id, contract_version);
                CREATE INDEX IF NOT EXISTS idx_items_record ON valuation_items(record_id);
                CREATE INDEX IF NOT EXISTS idx_batch_audit ON batch_audit_events(batch_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---------- 准备金评估批次 ----------
    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("rules_snapshot",):
            item[key] = json.loads(item[key]) if item.get(key) else {}
        item["idempotent_replay"] = bool(item.get("idempotent_replay"))
        return item

    @staticmethod
    def _item_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item.pop("basis_snapshot")) if item.get("basis_snapshot") else {}
        item["rules"] = json.loads(item.pop("rule_snapshot")) if item.get("rule_snapshot") else {}
        return item

    def find_batch_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM valuation_batches WHERE reference=?", (reference,)).fetchone()
        return self._batch_row(row) if row else None

    def create_batch(self, reference: str, period: str, rules: Dict[str, Any], member_ids: List[int], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO valuation_batches(reference,period,state,rules_snapshot,member_total,cursor_index,created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (reference, period, "draft", json.dumps(rules, ensure_ascii=False, sort_keys=True), len(member_ids), 0, actor_id, actor_id, now, now),
                )
                batch_id = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO valuation_members(batch_id,record_id,seq,done) VALUES(?,?,?,0)",
                    [(batch_id, int(record_id), seq) for seq, record_id in enumerate(member_ids)],
                )
                connection.execute(
                    "INSERT INTO batch_audit_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                    (batch_id, "created", actor_id, json.dumps({"reference": reference, "period": period, "members": len(member_ids)}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM valuation_batches WHERE id=?", (batch_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次引用已存在") from exc
        return self._batch_row(row)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM valuation_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("评估批次不存在")
        return self._batch_row(row)

    def list_batches(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM valuation_batches WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM valuation_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._batch_row(row) for row in rows]

    def batch_members(self, batch_id: int, only_pending: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT record_id AS record_id, seq, done FROM valuation_members WHERE batch_id=?"
        if only_pending:
            sql += " AND done=0"
        sql += " ORDER BY seq"
        with self._connect() as connection:
            rows = connection.execute(sql, (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def member_count(self, batch_id: int, done: Optional[bool] = None) -> int:
        sql = "SELECT COUNT(*) AS total FROM valuation_members WHERE batch_id=?"
        params: List[Any] = [batch_id]
        if done is not None:
            sql += " AND done=?"
            params.append(1 if done else 0)
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["total"])

    def mark_member_done(self, batch_id: int, record_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE valuation_members SET done=1 WHERE batch_id=? AND record_id=?", (batch_id, record_id))

    def reset_members_for_records(self, batch_id: int, record_ids: List[int]) -> None:
        if not record_ids:
            return
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            connection.execute(
                "UPDATE valuation_members SET done=0 WHERE batch_id=? AND record_id IN (%s)" % placeholders,
                [batch_id] + [int(rid) for rid in record_ids],
            )

    def append_members(self, batch_id: int, members: List[tuple]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO valuation_members(batch_id,record_id,seq,done) VALUES(?,?,?,0)",
                [(batch_id, int(record_id), int(seq)) for record_id, seq in members],
            )

    def upsert_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM valuation_items WHERE batch_id=? AND record_id=? AND contract_version=?",
                (item["batch_id"], item["record_id"], item["contract_version"]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO valuation_items(batch_id,record_id,reference,event_id,contract_version,claim_version,loss_date,"
                    "estimated_amount,formal_amount,status,change_reason,basis_snapshot,rule_snapshot,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["batch_id"], item["record_id"], item["reference"], item.get("loss_event_id", ""),
                        item["contract_version"], item["claim_version"], item.get("loss_date", ""),
                        item["estimated_amount"], item.get("formal_amount"), item["status"],
                        item.get("change_reason", ""), json.dumps(item.get("basis", {}), ensure_ascii=False, sort_keys=True),
                        json.dumps(item.get("rules", {}), ensure_ascii=False, sort_keys=True), now, now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE valuation_items SET claim_version=?,loss_date=?,estimated_amount=?,formal_amount=?,status=?,"
                    "change_reason=?,basis_snapshot=?,rule_snapshot=?,updated_at=? WHERE id=?",
                    (
                        item["claim_version"], item.get("loss_date", ""), item["estimated_amount"], item.get("formal_amount"),
                        item["status"], item.get("change_reason", ""),
                        json.dumps(item.get("basis", {}), ensure_ascii=False, sort_keys=True),
                        json.dumps(item.get("rules", {}), ensure_ascii=False, sort_keys=True), now, int(existing["id"]),
                    ),
                )
            connection.commit()
        return self.get_item(item["batch_id"], item["record_id"], item["contract_version"])

    def get_item(self, batch_id: int, record_id: int, contract_version: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM valuation_items WHERE batch_id=? AND record_id=? AND contract_version=?",
                (batch_id, record_id, contract_version),
            ).fetchone()
        if row is None:
            raise NotFound("评估明细不存在")
        return self._item_row(row)

    def list_items(self, batch_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM valuation_items WHERE batch_id=?"
        params: List[Any] = [batch_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._item_row(row) for row in rows]

    def active_items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM valuation_items WHERE batch_id=? AND status<>'invalid' ORDER BY record_id, contract_version",
                (batch_id,),
            ).fetchall()
        return [self._item_row(row) for row in rows]

    def invalidate_active_items(self, batch_id: int, record_ids: List[int], reason: str) -> None:
        if not record_ids:
            return
        placeholders = ",".join("?" for _ in record_ids)
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE valuation_items SET status='invalid',change_reason=?,updated_at=? WHERE batch_id=? "
                "AND record_id IN (%s) AND status<>'invalid'" % placeholders,
                [reason, now, batch_id] + [int(rid) for rid in record_ids],
            )

    def affected_record_ids(self, batch_id: int) -> List[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT record_id FROM valuation_items WHERE batch_id=? AND status<>'invalid'", (batch_id,)
            ).fetchall()
        return [int(row["record_id"]) for row in rows]

    def refresh_batch_totals(self, batch_id: int) -> Dict[str, float]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(SUM(estimated_amount),0) AS est, COALESCE(SUM(COALESCE(formal_amount,estimated_amount)),0) AS formal "
                "FROM valuation_items WHERE batch_id=? AND status<>'invalid' AND status<>'needs_verification'",
                (batch_id,),
            ).fetchone()
            now = _now()
            connection.execute(
                "UPDATE valuation_batches SET estimated_total=?,updated_at=? WHERE id=?",
                (round(float(row["est"]), 2), now, batch_id),
            )
            connection.commit()
        batch = self.get_batch(batch_id)
        return {"estimated_total": float(batch["estimated_total"])}

    def update_batch(self, batch_id: int, **fields: Any) -> Dict[str, Any]:
        if not fields:
            return self.get_batch(batch_id)
        fields["updated_at"] = _now()
        columns = ", ".join("%s=?" % key for key in fields)
        with self._connect() as connection:
            connection.execute("UPDATE valuation_batches SET %s WHERE id=?" % columns, list(fields.values()) + [batch_id])
        return self.get_batch(batch_id)

    def seal_batch(self, batch_id: int, actor_id: str, estimated_total: float) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE valuation_batches SET state='sealed',estimated_total=?,sealed_by=?,sealed_at=?,updated_by=?,updated_at=? WHERE id=?",
                (round(float(estimated_total), 2), actor_id, now, actor_id, now, batch_id),
            )

    def post_batch(self, batch_id: int, actor_id: str, formal_total: float) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE valuation_batches SET state='posted',formal_total=?,reviewed_by=?,posted_at=?,updated_by=?,updated_at=? WHERE id=?",
                (round(float(formal_total), 2), actor_id, now, actor_id, now, batch_id),
            )
            connection.execute(
                "UPDATE valuation_items SET formal_amount=estimated_amount,reviewed_by=?,reviewed_at=? "
                "WHERE batch_id=? AND status<>'invalid' AND status<>'needs_verification'",
                (actor_id, now, batch_id),
            )
            connection.commit()

    def add_batch_audit(self, batch_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batch_audit_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                (batch_id, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def batch_timeline(self, batch_id: int) -> List[Dict[str, Any]]:
        self.get_batch(batch_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batch_audit_events WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def engagement_items(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT i.*, b.reference AS batch_reference, b.state AS batch_state FROM valuation_items i "
                "JOIN valuation_batches b ON b.id=i.batch_id WHERE i.record_id=? ORDER BY i.batch_id, i.contract_version",
                (record_id,),
            ).fetchall()
        return [self._item_row(row) for row in rows]

    def valuation_stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            batch_rows = connection.execute("SELECT state, COUNT(*) AS total FROM valuation_batches GROUP BY state").fetchall()
            item_rows = connection.execute(
                "SELECT status, COUNT(*) AS total, COALESCE(SUM(estimated_amount),0) AS amount FROM valuation_items GROUP BY status"
            ).fetchall()
        return {
            "batches": {str(row["state"]): int(row["total"]) for row in batch_rows},
            "items": {
                str(row["status"]): {"count": int(row["total"]), "estimated_amount": round(float(row["amount"]), 2)}
                for row in item_rows
            },
        }
