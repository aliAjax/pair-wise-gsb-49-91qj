"""SQLite 表结构与事务访问。

审计事件统一存放(audit_events.entity_type/entity_id)，批次动作会扇出到
合约时间线，因此"合约详情、批次时间线、变动原因"可以互相对账。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: str) -> Any:
    return json.loads(value) if value is not None else None


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
                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_ref TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'active',
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    claim_ref TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(contract_id, claim_ref)
                );
                CREATE TABLE IF NOT EXISTS reserve_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'open',
                    rules_snapshot TEXT NOT NULL,
                    contract_scope TEXT,
                    created_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    closed_at TEXT,
                    posted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_period ON reserve_batches(period);
                CREATE TABLE IF NOT EXISTS reserve_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES reserve_batches(id) ON DELETE CASCADE,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id),
                    status TEXT NOT NULL,
                    estimated_amount REAL NOT NULL DEFAULT 0,
                    pending_amount REAL NOT NULL DEFAULT 0,
                    formal_amount REAL,
                    issue TEXT,
                    basis TEXT NOT NULL,
                    changed_reasons TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, contract_id)
                );
                CREATE INDEX IF NOT EXISTS idx_items_batch ON reserve_batch_items(batch_id);
                CREATE INDEX IF NOT EXISTS idx_items_contract ON reserve_batch_items(contract_id);
                CREATE TABLE IF NOT EXISTS formal_reserves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES reserve_batches(id),
                    period TEXT NOT NULL,
                    item_id INTEGER NOT NULL REFERENCES reserve_batch_items(id),
                    contract_id INTEGER NOT NULL REFERENCES contracts(id),
                    formal_amount REAL NOT NULL,
                    posted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, contract_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(entity_type, id) WHERE entity_type = 'batch';
                """
            )
            # 轻量迁移：旧库补 contract_scope 列
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(reserve_batches)").fetchall()}
            if "contract_scope" not in columns:
                connection.execute("ALTER TABLE reserve_batches ADD COLUMN contract_scope TEXT")

    # ---------- 通用 ----------
    @staticmethod
    def _row(row: sqlite3.Row, json_fields: Sequence[str] = ()) -> Dict[str, Any]:
        item = dict(row)
        for field in json_fields:
            if field in item:
                item[field] = _loads(item[field])
        return item

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def add_audit(
        self,
        entity_type: str,
        entity_id: int,
        action: str,
        actor_id: str,
        details: Dict[str, Any],
        now: str = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (entity_type, entity_id, action, actor_id,
                 json.dumps(details, ensure_ascii=False, sort_keys=True), now or _now()),
            )

    def add_audit_many(self, events: Sequence[Dict[str, Any]]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                [
                    (
                        e["entity_type"], e["entity_id"], e["action"], e["actor_id"],
                        json.dumps(e.get("details", {}), ensure_ascii=False, sort_keys=True),
                        e.get("created_at") or _now(),
                    )
                    for e in events
                ],
            )

    def audit_timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, entity_id),
            ).fetchall()
        return [self._row(row, ("details",)) for row in rows]

    # ---------- 合约 ----------
    def create_contract(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO contracts(contract_ref,version,state,payload,created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (payload["contract_ref"], 1, "active",
                     json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now),
                )
                contract_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,details,created_at)"
                    " VALUES('contract',?, 'created', ?, ?, ?)",
                    (contract_id, actor_id,
                     json.dumps({"contract_ref": payload["contract_ref"]}, ensure_ascii=False), now),
                )
                row = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("合约编号已存在") from exc
        return self._row(row, ("payload",))

    def get_contract(self, contract_id: int, lock: bool = False) -> Dict[str, Any]:
        with self._connect() as connection:
            if lock:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
                connection.commit()
            else:
                row = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if row is None:
            raise NotFound("再保合约不存在")
        return self._row(row, ("payload",))

    def list_contracts(self, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute(
                    "SELECT * FROM contracts WHERE state=? ORDER BY id LIMIT ?", (state, limit)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM contracts ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [self._row(row, ("payload",)) for row in rows]

    def mutate_contract(
        self,
        contract_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM contracts WHERE id=?", (contract_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("再保合约不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("合约版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE contracts SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, contract_id),
            )
            result = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
            connection.commit()
        return self._row(result, ("payload",))

    # ---------- 赔案 ----------
    def create_claim(self, contract_id: int, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO claims(contract_id,claim_ref,event_id,version,status,payload,"
                    "created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (contract_id, payload["claim_ref"], payload["event_id"], 1, payload["status"],
                     json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now),
                )
                claim_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("赔案编号在该合约下已存在") from exc
        return self._row(row, ("payload",))

    def get_claim(self, claim_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound("赔案不存在")
        return self._row(row, ("payload",))

    def list_claims_for_contracts(self, contract_ids: Sequence[int]) -> List[Dict[str, Any]]:
        if not contract_ids:
            return []
        marks = ",".join("?" for _ in contract_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE contract_id IN (%s) ORDER BY contract_id, id" % marks,
                tuple(contract_ids),
            ).fetchall()
        return [self._row(row, ("payload",)) for row in rows]

    def list_claims_by_event(self, event_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE event_id=? ORDER BY id", (event_id,)
            ).fetchall()
        return [self._row(row, ("payload",)) for row in rows]

    def mutate_claim(
        self,
        claim_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        actor_id: str,
        changed_fields: Sequence[str],
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT contract_id, version FROM claims WHERE id=?", (claim_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("赔案不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("赔案版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE claims SET event_id=?,status=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (payload["event_id"], payload["status"], version,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, claim_id),
            )
            connection.execute(
                "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,details,created_at)"
                " VALUES('claim', ?, 'amended', ?, ?, ?)",
                (claim_id, actor_id,
                 json.dumps({"changed_fields": list(changed_fields),
                             "from_version": int(expected_version), "to_version": version,
                             "status": payload["status"]}, ensure_ascii=False),
                 now),
            )
            result = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            connection.commit()
        return self._row(result, ("payload",))

    def backfill_claim_date(self, claim_id: int, occurrence_date: str, source: str) -> None:
        """旧数据回填损失日期：属于数据清洗，不递增赔案版本，仅留审计。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM claims WHERE id=?", (claim_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("赔案不存在")
            payload = _loads(row["payload"])
            if payload.get("occurrence_date"):
                connection.rollback()
                return
            payload["occurrence_date"] = occurrence_date
            connection.execute(
                "UPDATE claims SET payload=?,updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), now, claim_id),
            )
            connection.execute(
                "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,details,created_at)"
                " VALUES('claim', ?, 'loss_date_backfilled', 'system', ?, ?)",
                (claim_id,
                 json.dumps({"occurrence_date": occurrence_date, "source": source}, ensure_ascii=False), now),
            )
            connection.commit()

    # ---------- 评估批次 ----------
    def create_batch(self, period: str, idempotency_key: str, rules_snapshot: Dict[str, Any],
                     actor_id: str, contract_scope: Optional[List[int]] = None) -> int:
        now = _now()
        scope = json.dumps(sorted(contract_scope)) if contract_scope is not None else None
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO reserve_batches(period,idempotency_key,state,rules_snapshot,contract_scope,"
                "created_by,created_at,updated_at) VALUES(?,?,'open',?,?,?,?,?)",
                (period, idempotency_key,
                 json.dumps(rules_snapshot, ensure_ascii=False, sort_keys=True), scope, actor_id, now, now),
            )
            return int(cursor.lastrowid)

    def get_batch_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reserve_batches WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
        return self._row(row, ("rules_snapshot",)) if row else None

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reserve_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("评估批次不存在")
        return self._row(row, ("rules_snapshot",))

    def list_batches(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reserve_batches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(row, ("rules_snapshot",)) for row in rows]

    def upsert_batch_item(self, item: Dict[str, Any]) -> int:
        """幂等写入单个项目；崩溃后续作按(batch_id, contract_id)覆盖，不产生重复。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM reserve_batch_items WHERE batch_id=? AND contract_id=?",
                (item["batch_id"], item["contract_id"]),
            ).fetchone()
            if existing:
                item_id = int(existing["id"])
                connection.execute(
                    "UPDATE reserve_batch_items SET status=?,estimated_amount=?,pending_amount=?,"
                    "formal_amount=?,issue=?,basis=?,changed_reasons=?,updated_at=? WHERE id=?",
                    (item["status"], item["estimated_amount"], item["pending_amount"],
                     item.get("formal_amount"), item.get("issue"),
                     json.dumps(item["basis"], ensure_ascii=False, sort_keys=True),
                     json.dumps(item.get("changed_reasons", []), ensure_ascii=False),
                     now, item_id),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO reserve_batch_items(batch_id,contract_id,status,estimated_amount,pending_amount,"
                    "formal_amount,issue,basis,changed_reasons,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (item["batch_id"], item["contract_id"], item["status"],
                     item["estimated_amount"], item["pending_amount"], item.get("formal_amount"),
                     item.get("issue"),
                     json.dumps(item["basis"], ensure_ascii=False, sort_keys=True),
                     json.dumps(item.get("changed_reasons", []), ensure_ascii=False),
                     now, now),
                )
                item_id = int(cursor.lastrowid)
            connection.commit()
        return item_id

    def list_batch_items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reserve_batch_items WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        return [self._row(row, ("basis", "changed_reasons")) for row in rows]

    def get_batch_item(self, batch_id: int, contract_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reserve_batch_items WHERE batch_id=? AND contract_id=?",
                (batch_id, contract_id),
            ).fetchone()
        return self._row(row, ("basis", "changed_reasons")) if row else None

    def item_counts(self, batch_id: int) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM reserve_batch_items WHERE batch_id=? GROUP BY status",
                (batch_id,),
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def touch_batch(self, batch_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE reserve_batches SET updated_at=? WHERE id=?", (_now(), batch_id)
            )

    def close_batch(self, batch_id: int, actor_id: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM reserve_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("评估批次不存在")
            if str(row["state"]) != "open":
                connection.rollback()
                raise Conflict("批次不是待封账状态，无法封账")
            connection.execute(
                "UPDATE reserve_batches SET state='closed',closed_at=?,updated_at=? WHERE id=?",
                (now, now, batch_id),
            )
            connection.commit()

    def list_formal_for_batch(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM formal_reserves WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_batch_posted(
        self,
        batch_id: int,
        postings: Sequence[Dict[str, Any]],
        actor_id: str,
    ) -> None:
        """复核转正式：正式金额一次性落账。重入时已转正式直接成功(只留一份)。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, period FROM reserve_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("评估批次不存在")
            if str(row["state"]) == "posted":
                connection.rollback()
                return
            if str(row["state"]) != "closed":
                connection.rollback()
                raise Conflict("只有已封账待复核批次才能转正式")
            for posting in postings:
                connection.execute(
                    "UPDATE reserve_batch_items SET formal_amount=?,updated_at=? WHERE id=? AND batch_id=?",
                    (posting["formal_amount"], now, posting["item_id"], batch_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO formal_reserves"
                    "(batch_id,period,item_id,contract_id,formal_amount,posted_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (batch_id, str(row["period"]), posting["item_id"], posting["contract_id"],
                     posting["formal_amount"], actor_id, now),
                )
            connection.execute(
                "UPDATE reserve_batches SET state='posted',reviewed_by=?,posted_at=?,updated_at=? WHERE id=?",
                (actor_id, now, now, batch_id),
            )
            connection.commit()

    # ---------- 正式准备金查询 ----------
    def formal_reserves_for_contract(self, contract_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT f.*, b.period AS period_label FROM formal_reserves f"
                " JOIN reserve_batches b ON b.id=f.batch_id"
                " WHERE f.contract_id=? ORDER BY f.id",
                (contract_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def formal_totals(self) -> Dict[str, float]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT b.period AS period, SUM(f.formal_amount) AS total, COUNT(*) AS cnt"
                " FROM formal_reserves f JOIN reserve_batches b ON b.id=f.batch_id"
                " GROUP BY b.period ORDER BY b.period"
            ).fetchall()
        return {
            str(row["period"]): {"formal_total": round(float(row["total"]), 2), "contracts": int(row["cnt"])}
            for row in rows
        }

    def pending_reconciliation_contracts(self) -> List[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT contract_id FROM reserve_batch_items WHERE status='pending_reconciliation'"
            ).fetchall()
        return [int(row["contract_id"]) for row in rows]
