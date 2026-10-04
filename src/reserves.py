"""未决赔款准备金评估批次：冻结依据、失效重算、封账与财务转正式。"""
import re
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, PermissionDenied, ValidationError, number
from .rules import OPEN_CLAIM_STATES
from .audit import AuditRecorder

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
OPEN_RESERVE_STATES = set(OPEN_CLAIM_STATES)
CLOSED_CLAIM_STATES = {"claim_withdrawn", "rejected", "settled"}
BATCH_DRAFT = "draft"
BATCH_SEALED = "sealed"
BATCH_POSTED = "posted"
BATCH_ROLES = {
    "create_batch": {"claims_officer", "finance"},
    "resume_batch": {"claims_officer", "finance"},
    "refresh_batch": {"claims_officer", "finance"},
    "repair_item": {"claims_officer"},
    "seal_batch": {"finance", "claims_officer"},
    "post_batch": {"finance"},
}


def _round2(value: float) -> float:
    return round(float(value) + 0.0, 2)


class ReserveRules:
    """估算规则：规则参数在批次创建时冻结，重算也沿用当批快照。"""

    DEFAULTS = {"ibnr_pct": 0.10, "expense_pct": 0.05}

    @staticmethod
    def require_loss_date(value: Any) -> str:
        if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()):
            raise ValidationError("损失日期必须为YYYY-MM-DD")
        return value.strip()

    def validate_rules(self, data: Dict[str, Any]) -> Dict[str, float]:
        data = data or {}
        ibnr = float(number({"ibnr_pct": data.get("ibnr_pct", self.DEFAULTS["ibnr_pct"])}, "ibnr_pct", 0, 1))
        expense = float(number({"expense_pct": data.get("expense_pct", self.DEFAULTS["expense_pct"])}, "expense_pct", 0, 1))
        return {"ibnr_pct": ibnr, "expense_pct": expense}

    @staticmethod
    def validate_period(period: str) -> str:
        if not isinstance(period, str) or not PERIOD_RE.match(period.strip()):
            raise ValidationError("评估期间必须为YYYY-MM格式")
        return period.strip()

    def estimate(self, record_payload: Dict[str, Any], rules: Dict[str, float]) -> Dict[str, Any]:
        attachment = float(record_payload.get("attachment", 0))
        width = float(record_payload.get("layer_width")) if record_payload.get("layer_width") is not None else float(record_payload.get("limit", 0)) - attachment
        cession = float(record_payload.get("cession_pct", 0))
        gross = float(record_payload.get("approved_loss", record_payload.get("loss_amount", 0)) or 0)
        layer_recovery = min(max(0.0, gross - attachment), width) * cession
        ibnr_pct = float(rules.get("ibnr_pct", self.DEFAULTS["ibnr_pct"]))
        expense_pct = float(rules.get("expense_pct", self.DEFAULTS["expense_pct"]))
        ibnr = layer_recovery * ibnr_pct
        expense = (layer_recovery + ibnr) * expense_pct
        estimated = layer_recovery + ibnr + expense
        return {
            "gross_outstanding": _round2(gross),
            "estimated_recovery": _round2(layer_recovery),
            "ibnr_amount": _round2(ibnr),
            "expense_amount": _round2(expense),
            "estimated_amount": _round2(estimated),
        }


class ValuationService:
    def __init__(self, repository: Any, rules: ReserveRules = None, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules or ReserveRules()
        self.audit = audit or AuditRecorder(repository)

    # ---------- 身份与权限 ----------
    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _allow(self, actor: Actor, action: str) -> None:
        if actor.role != "admin" and actor.role not in BATCH_ROLES.get(action, set()):
            raise PermissionDenied("角色无权执行%s" % action)

    @staticmethod
    def _require_state(batch: Dict[str, Any], *states: str) -> None:
        if batch["state"] not in states:
            raise Conflict("批次当前状态为%s，不允许该操作" % batch["state"])

    # ---------- 损失日期回填 ----------
    def _loss_date_index(self) -> Dict[str, Dict[str, str]]:
        """按事件号汇总旧数据中已有的损失日期，取最近更新记录的日期回填。"""
        index: Dict[str, Dict[str, str]] = {}
        for record in self.repository.list_records(limit=500):
            payload = record.get("payload") or {}
            loss_date = str(payload.get("loss_date", "")).strip()
            event_id = str(payload.get("event_id", "")).strip()
            if not event_id or not loss_date:
                continue
            current = index.get(event_id)
            if current is None or record["updated_at"] >= current["updated_at"]:
                index[event_id] = {"loss_date": loss_date, "updated_at": record["updated_at"], "reference": record["reference"]}
        return index

    def _resolve_loss_date(self, payload: Dict[str, Any], date_index: Dict[str, Dict[str, str]]) -> Dict[str, str]:
        direct = str(payload.get("loss_date", "")).strip()
        if direct:
            return {"loss_date": direct, "source": "direct", "backfill_reference": ""}
        event_id = str(payload.get("event_id", "")).strip()
        hit = date_index.get(event_id)
        if hit:
            return {"loss_date": hit["loss_date"], "source": "event_backfill", "backfill_reference": hit["reference"]}
        return {"loss_date": "", "source": "missing", "backfill_reference": ""}

    # ---------- 明细估算 ----------
    def _evaluate(self, record: Dict[str, Any], frozen_rules: Dict[str, float], date_index: Dict[str, Dict[str, str]], override_reason: str = "") -> Dict[str, Any]:
        payload = record.get("payload") or {}
        state = record["state"]
        event_id = str(payload.get("event_id", ""))
        date_info = self._resolve_loss_date(payload, date_index)
        calc = self.rules.estimate(payload, frozen_rules)
        if state in CLOSED_CLAIM_STATES:
            status = "excluded"
            amount = 0.0
            calc = {key: 0.0 for key in calc}
            reason = {"claim_withdrawn": "赔案撤销，不计提未决", "rejected": "赔案拒赔，不计提未决", "settled": "赔案已结算，转已决"}.get(state, "赔案已关闭")
        elif state not in OPEN_RESERVE_STATES:
            status = "excluded"
            amount = 0.0
            calc = {key: 0.0 for key in calc}
            reason = "合约状态%s，不参与未决评估" % state
        else:
            amount = calc["estimated_amount"]
            if date_info["source"] == "missing":
                status = "needs_verification"
                reason = "旧数据缺损失日期，事件号%s无法回填" % event_id
            else:
                status = "estimated"
                reason = override_reason or ""
        if override_reason and status in ("excluded", "needs_verification"):
            reason = "%s；%s" % (override_reason, reason) if reason else override_reason
        basis = {
            "reference": record["reference"],
            "event_id": event_id,
            "claim_number": payload.get("claim_number", ""),
            "record_state": state,
            "attachment": payload.get("attachment", 0),
            "limit": payload.get("limit", 0),
            "layer_width": payload.get("layer_width", 0),
            "cession_pct": payload.get("cession_pct", 0),
            "approved_loss": payload.get("approved_loss", payload.get("loss_amount", 0)),
            "loss_amount": payload.get("loss_amount", 0),
            "loss_date": date_info["loss_date"],
            "loss_date_source": date_info["source"],
            "backfill_reference": date_info["backfill_reference"],
            **calc,
        }
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "loss_event_id": event_id,
            "contract_version": int(record["version"]),
            "claim_version": int(record["version"]),
            "loss_date": date_info["loss_date"],
            "estimated_amount": amount,
            "status": status,
            "change_reason": reason,
            "basis": basis,
            "rules": frozen_rules,
        }

    def _persist_item(self, batch: Dict[str, Any], item: Dict[str, Any]) -> None:
        payload = dict(item)
        payload["batch_id"] = batch["id"]
        self.repository.upsert_item(payload)
        # 明细落库成功后才推进成员游标，写入失败可从完整批次续作
        self.repository.mark_member_done(batch["id"], item["record_id"])

    def _process_pending(self, batch: Dict[str, Any], actor_id: str, override_reasons: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
        frozen_rules = batch["rules_snapshot"]
        date_index = self._loss_date_index()
        override_reasons = override_reasons or {}
        processed: List[Dict[str, Any]] = []
        for member in self.repository.batch_members(batch["id"], only_pending=True):
            record = self.repository.get(member["record_id"])
            item = self._evaluate(record, frozen_rules, date_index, override_reason=override_reasons.get(record["id"], ""))
            self._persist_item(batch, item)
            processed.append({"record_id": item["record_id"], "reference": item["reference"], "status": item["status"], "estimated_amount": item["estimated_amount"]})
        totals = self.repository.refresh_batch_totals(batch["id"])
        cursor = self.repository.member_count(batch["id"], done=True)
        member_total = self.repository.member_count(batch["id"])
        self.repository.update_batch(batch["id"], cursor_index=cursor)
        if processed:
            self.repository.add_batch_audit(
                batch["id"], actor_id, "evaluated",
                {"count": len(processed), "cursor": cursor, "estimated_total": totals["estimated_total"], "items": processed},
            )
        return {"processed": processed, "cursor": cursor, "member_total": member_total, **totals}

    # ---------- 创建批次（冻结参与合约、规则） ----------
    def create_batch(self, actor: Actor, reference: str, period: str, rule_data: Dict[str, Any] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._allow(actor, "create_batch")
        if not isinstance(reference, str) or not reference.strip():
            raise ValidationError("批次引用不能为空")
        reference = reference.strip()
        existing = self.repository.find_batch_by_reference(reference)
        if existing is not None:
            # 重复提交同一批次只留一份结果：直接回放既有批次
            existing["idempotent_replay"] = True
            return existing
        period = self.rules.validate_period(period)
        frozen_rules = self.rules.validate_rules(rule_data or {})
        members = [record["id"] for record in self.repository.list_records(limit=500) if record["state"] in OPEN_RESERVE_STATES]
        batch = self.repository.create_batch(reference, period, frozen_rules, members, actor.user_id)
        progress = self._process_pending(batch, actor.user_id)
        batch = self.repository.get_batch(batch["id"])
        batch["progress"] = progress
        batch["idempotent_replay"] = False
        return batch

    # ---------- 断点续作 ----------
    def resume_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._allow(actor, "resume_batch")
        batch = self.repository.get_batch(batch_id)
        self._require_state(batch, BATCH_DRAFT)
        progress = self._process_pending(batch, actor.user_id)
        result = self.repository.get_batch(batch_id)
        result["progress"] = progress
        result["idempotent_replay"] = False
        return result

    # ---------- 封账前赔案变化：仅受影响合约失效重算，已封账不动 ----------
    def refresh_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._allow(actor, "refresh_batch")
        batch = self.repository.get_batch(batch_id)
        self._require_state(batch, BATCH_DRAFT)
        invalidated: List[Dict[str, Any]] = []
        drifted: List[int] = []
        active = self.repository.active_items(batch_id)
        current = {record["id"]: record for record in self.repository.list_records(limit=500)}
        for item in active:
            record = current.get(item["record_id"])
            if record is None:
                continue
            reason = self._change_reason(item, record)
            if reason:
                drifted.append(record["id"])
                invalidated.append({"record_id": record["id"], "reference": item["reference"], "from_version": item["contract_version"], "to_version": record["version"], "reason": reason})
        if drifted:
            reason_text = "; ".join(sorted({x["reason"] for x in invalidated}))
            self.repository.invalidate_active_items(batch_id, drifted, "封账前依据失效：%s" % reason_text)
            self.repository.reset_members_for_records(batch_id, drifted)
        # 赔案补录：把新出现的在途赔案追加进本批
        known = {member["record_id"] for member in self.repository.batch_members(batch_id)}
        newcomers = [record for record in current.values() if record["id"] not in known and record["state"] in OPEN_RESERVE_STATES]
        appended = [record["reference"] for record in newcomers]
        if newcomers:
            self.repository.append_members(batch_id, [(record["id"], batch["member_total"] + index) for index, record in enumerate(newcomers)])
            self.repository.update_batch(batch_id, member_total=batch["member_total"] + len(newcomers))
        batch = self.repository.get_batch(batch_id)
        override_reasons = {entry["record_id"]: entry["reason"] for entry in invalidated}
        progress = self._process_pending(batch, actor.user_id, override_reasons=override_reasons)
        if invalidated or appended:
            self.repository.add_batch_audit(
                batch_id, actor.user_id, "refreshed",
                {"invalidated": invalidated, "appended": appended, "estimated_total": progress["estimated_total"]},
            )
        result = self.repository.get_batch(batch_id)
        result["progress"] = progress
        result["invalidated"] = invalidated
        result["appended"] = appended
        result["idempotent_replay"] = False
        return result

    @staticmethod
    def _change_reason(item: Dict[str, Any], record: Dict[str, Any]) -> str:
        if int(record["version"]) == int(item["contract_version"]) and record["state"] == item["basis"].get("record_state"):
            return ""
        payload = record.get("payload") or {}
        if record["state"] == "claim_withdrawn":
            return "赔案撤销：%s" % payload.get("withdraw_reason", "未注明原因")
        if record["state"] == "rejected":
            return "赔案拒赔：%s" % payload.get("reject_reason", "未注明原因")
        if record["state"] == "settled":
            return "赔案已结算转已决"
        old_loss = item["basis"].get("approved_loss")
        new_loss = payload.get("approved_loss")
        if old_loss not in (None, "") and new_loss not in (None, "") and float(old_loss) != float(new_loss):
            return "核定金额变化：%s→%s" % (old_loss, new_loss)
        if str(item["basis"].get("loss_date", "")) != str(payload.get("loss_date", "")):
            return "损失日期补录：%s→%s" % (item["basis"].get("loss_date", "空"), payload.get("loss_date", "空"))
        return "赔案版本变化：v%s→v%s" % (item["contract_version"], record["version"])

    # ---------- 人工补全损失日期 ----------
    def repair_item(self, actor: Actor, batch_id: int, record_id: int, loss_date: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._allow(actor, "repair_item")
        loss_date = self.rules.require_loss_date(loss_date)
        batch = self.repository.get_batch(batch_id)
        self._require_state(batch, BATCH_DRAFT)
        record = self.repository.get(record_id)
        waiting = [item for item in self.repository.list_items(batch_id, status="needs_verification") if item["record_id"] == record_id]
        if not waiting:
            raise Conflict("该合约没有待核对的评估明细")
        item_payload = self._evaluate(record, batch["rules_snapshot"], {})
        item_payload["loss_date"] = loss_date
        item_payload["basis"]["loss_date"] = loss_date
        item_payload["basis"]["loss_date_source"] = "manual_repair"
        item_payload["status"] = "estimated"
        item_payload["change_reason"] = "人工补录损失日期：%s" % loss_date
        item_payload["contract_version"] = waiting[-1]["contract_version"]
        item_payload["batch_id"] = batch_id
        saved = self.repository.upsert_item(item_payload)
        totals = self.repository.refresh_batch_totals(batch_id)
        self.repository.add_batch_audit(
            batch_id, actor.user_id, "repaired",
            {"record_id": record_id, "reference": record["reference"], "loss_date": loss_date, "estimated_total": totals["estimated_total"]},
        )
        return saved

    # ---------- 封账 ----------
    def seal_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._allow(actor, "seal_batch")
        batch = self.repository.get_batch(batch_id)
        if batch["state"] in (BATCH_SEALED, BATCH_POSTED):
            batch["idempotent_replay"] = True
            return batch
        self._require_state(batch, BATCH_DRAFT)
        pending = self.repository.member_count(batch_id, done=False)
        if pending:
            raise Conflict("尚有%s个合约未完成估算，请先续作" % pending)
        waiting = self.repository.list_items(batch_id, status="needs_verification")
        if waiting:
            raise Conflict("有%s个合约损失日期待核对，不能封账" % len(waiting))
        totals = self.repository.refresh_batch_totals(batch_id)
        self.repository.seal_batch(batch_id, actor.user_id, totals["estimated_total"])
        self.repository.add_batch_audit(
            batch_id, actor.user_id, "sealed",
            {"estimated_total": totals["estimated_total"], "rule_snapshot": batch["rules_snapshot"], "period": batch["period"]},
        )
        result = self.repository.get_batch(batch_id)
        result["idempotent_replay"] = False
        return result

    # ---------- 财务复核转正式准备金 ----------
    def post_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._allow(actor, "post_batch")
        batch = self.repository.get_batch(batch_id)
        if batch["state"] == BATCH_POSTED:
            batch["idempotent_replay"] = True
            return batch
        self._require_state(batch, BATCH_SEALED)
        items = self.repository.active_items(batch_id)
        formal_total = _round2(sum(float(item["estimated_amount"]) for item in items if item["status"] != "needs_verification"))
        self.repository.post_batch(batch_id, actor.user_id, formal_total)
        for item in items:
            if item["status"] == "needs_verification":
                continue
            self.repository.add_audit(
                item["record_id"], actor.user_id, "reserve_posted",
                {"batch_id": batch_id, "batch_reference": batch["reference"], "period": batch["period"],
                 "formal_amount": float(item["estimated_amount"]), "contract_version": item["contract_version"]},
            )
        self.repository.add_batch_audit(
            batch_id, actor.user_id, "posted",
            {"formal_total": formal_total, "estimated_total": batch["estimated_total"], "period": batch["period"]},
        )
        result = self.repository.get_batch(batch_id)
        result["idempotent_replay"] = False
        return result

    # ---------- 查询 ----------
    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        batch = self.repository.get_batch(batch_id)
        batch["cursor"] = self.repository.member_count(batch_id, done=True)
        batch["pending"] = self.repository.member_count(batch_id, done=False)
        return batch

    def list_batches(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        return self.repository.list_batches(state=state, limit=limit)

    def list_items(self, actor: Actor, batch_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self.repository.get_batch(batch_id)
        return self.repository.list_items(batch_id, status=status)

    def timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        return self.repository.batch_timeline(batch_id)

    def engagement_detail(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        record = self.repository.get(record_id)
        record["engagements"] = self.repository.engagement_items(record_id)
        return record

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        return self.repository.valuation_stats()
