"""用例编排：合约/赔案维护、评估批次冻结-刷新-封账-复核转正式。

关键不变量：
- 批次创建时逐项目落盘并立即提交，写入中途失败后，凭幂等键重提可从完整批次续作；
- 已封账批次冻结的合约版本/赔案快照/规则/金额不再被后续赔案变化影响；
- 封账前赔案变化只失效受影响合约所在项目，刷新时仅重算这些合约；
- 转正式必须财务复核，重复提交只留一份正式结果。
"""
from typing import Any, Dict, List, Optional, Sequence

from .domain import (
    Actor,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationError,
    iso_date,
    number,
    optional_text,
    period_label,
    text,
)
from .repository import Repository
from .repository import _loads
from .rules import (
    BATCH_CLOSED,
    BATCH_DRAFT,
    BATCH_POSTED,
    BATCH_STATES,
    CLAIM_OPEN,
    CLAIM_REASSESSED,
    CLAIM_STATUSES,
    CLAIM_WITHDRAWN,
    ITEM_ASSESSED,
    ITEM_PENDING_RECON,
    EstimationRules,
    compare_claim_snapshot,
    evaluate_item,
    known_role,
    money,
    resolve_backfill_date,
    validate_claim,
    validate_contract,
)

# 项目失效标记：封账前赔案有变化时，受影响项目先置为失效等待重算
REASON_STALE = "待重算(赔案已变化)"


class Service:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    # ---------- 身份与权限 ----------
    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _require_roles(self, actor: Actor, allowed: Sequence[str]) -> None:
        if actor.role not in set(allowed) | {"admin"}:
            raise PermissionDenied("角色无权执行该操作")

    def _ensure_known_role(self, actor: Actor) -> None:
        if not known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---------- 合约 ----------
    def create_contract(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_roles(actor, {"underwriter"})
        prepared = validate_contract(payload or {})
        return self.repository.create_contract(prepared, actor.user_id)

    def list_contracts(self, actor: Actor, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_contracts(state=state, limit=limit)

    def get_contract_detail(self, actor: Actor, contract_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        contract = self.repository.get_contract(contract_id)
        claims = self.repository.list_claims_for_contracts([contract_id])
        items = self._items_for_contract(contract_id)
        formal = self.repository.formal_reserves_for_contract(contract_id)
        pending = contract_id in self.repository.pending_reconciliation_contracts()
        return {
            "contract": contract,
            "claims": claims,
            "batch_participation": items,
            "formal_reserves": formal,
            "has_pending_reconciliation": pending,
        }

    def _items_for_contract(self, contract_id: int) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for batch in self.repository.list_batches(limit=500):
            item = self.repository.get_batch_item(batch["id"], contract_id)
            if item:
                item["batch_state"] = batch["state"]
                item["period"] = batch["period"]
                result.append(item)
        return result

    # ---------- 赔案 ----------
    def register_claim(self, actor: Actor, contract_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_roles(actor, {"claims_officer"})
        self.repository.get_contract(contract_id)
        prepared = validate_claim(payload or {})
        claim = self.repository.create_claim(contract_id, prepared, actor.user_id)
        self.repository.add_audit(
            "contract", contract_id, "claim_registered", actor.user_id,
            {"claim_id": claim["id"], "claim_ref": claim["claim_ref"],
             "event_id": claim["event_id"], "status": claim["status"]},
        )
        # 补录的赔案落到尚未封账的批次：受影响合约项目失效
        self._invalidate_open_items(contract_id, ["赔案补录:%s" % claim["claim_ref"]], actor.user_id)
        return claim

    def amend_claim(
        self,
        actor: Actor,
        claim_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        change_reason: str = "",
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_roles(actor, {"claims_officer"})
        if not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        reason = optional_text({"change_reason": change_reason}, "change_reason")
        current = self.repository.get_claim(claim_id)
        prepared = validate_claim(payload or {})
        # 事件号与赔案编号不可改
        prepared["claim_ref"] = current["claim_ref"]
        if prepared["event_id"] != current["event_id"]:
            raise ValidationError("事件号不可修改，如需挂接其他事件请补录新赔案")
        changed_fields = [
            key for key in ("status", "estimated_loss", "approved_loss", "occurrence_date", "review_flag", "note")
            if _field_changed(current["payload"].get(key), prepared.get(key), key)
        ]
        if not changed_fields:
            raise Conflict("赔案无实际变化")
        claim = self.repository.mutate_claim(
            claim_id, expected_version, prepared, actor.user_id, changed_fields,
        )
        labels = self._change_labels(current["payload"], prepared, changed_fields)
        if reason:
            labels.append("原因:%s" % reason)
        self.repository.add_audit(
            "contract", current["contract_id"], "claim_amended", actor.user_id,
            {"claim_id": claim_id, "claim_ref": claim["claim_ref"],
             "changed_fields": changed_fields, "change_reason": reason},
        )
        self._invalidate_open_items(current["contract_id"], labels, actor.user_id)
        return claim

    @staticmethod
    def _change_labels(before: Dict[str, Any], after: Dict[str, Any], fields: Sequence[str]) -> List[str]:
        names = {
            "status": "赔案状态",
            "estimated_loss": "报损金额",
            "approved_loss": "核定金额",
            "occurrence_date": "损失日期",
            "review_flag": "复核标记",
            "note": "备注",
        }
        labels: List[str] = []
        for field in fields:
            if field == "status":
                labels.append("赔案状态:%s→%s" % (before.get(field), after.get(field)))
            else:
                labels.append("%s变化" % names[field])
        return labels

    def _invalidate_open_items(self, contract_id: int, reasons: Sequence[str], actor_id: str) -> None:
        """封账前赔案变化：只让受影响合约在未封账批次中的项目失效；已封账批次保留原依据。"""
        for batch in self.repository.list_batches(limit=500):
            if batch["state"] != BATCH_DRAFT:
                continue
            item = self.repository.get_batch_item(batch["id"], contract_id)
            if not item:
                continue
            merged = list(item.get("changed_reasons") or [])
            for why in reasons:
                line = "%s:%s" % (REASON_STALE, why)
                if line not in merged:
                    merged.append(line)
            self.repository.upsert_batch_item(
                {
                    "batch_id": batch["id"],
                    "contract_id": contract_id,
                    "status": ITEM_ASSESSED,
                    "estimated_amount": item["estimated_amount"],
                    "pending_amount": item["pending_amount"],
                    "formal_amount": item.get("formal_amount"),
                    "issue": item.get("issue"),
                    "basis": item["basis"],
                    "changed_reasons": merged,
                    "stale": True,
                }
            )
            self.repository.add_audit(
                "batch", batch["id"], "item_invalidated", actor_id,
                {"contract_id": contract_id, "reasons": list(reasons)},
            )
            self.repository.add_audit(
                "contract", contract_id, "batch_item_invalidated", actor_id,
                {"batch_id": batch["id"], "reasons": list(reasons)},
            )

    # ---------- 评估批次 ----------
    def create_batch(
        self,
        actor: Actor,
        period: str,
        idempotency_key: str,
        ibnr_factor: float = 0.1,
        discount_factor: float = 1.0,
        contract_ids: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_roles(actor, {"actuary"})
        period = period_label({"period": period}, "period")
        idem = text({"idempotency_key": idempotency_key}, "idempotency_key")

        # 重复提交同一批次：只返回已有那一份结果；若上次写入中断则从完整批次续作
        existing = self.repository.get_batch_by_idempotency_key(idem)
        if existing:
            resumed = False
            if existing["state"] == BATCH_DRAFT:
                resumed = self._resume_if_incomplete(existing)
            return self._complete(
                self.repository.get_batch(existing["id"]), resumed=resumed, duplicate=True)

        rules = EstimationRules(ibnr_factor=ibnr_factor, discount_factor=discount_factor)
        snapshot = rules.as_snapshot()
        all_contracts = [c for c in self.repository.list_contracts(limit=500) if c["state"] != "terminated"]
        scope_ids: Optional[List[int]] = None
        if contract_ids is not None:
            scope_ids = sorted({int(cid) for cid in contract_ids})
            by_id = {c["id"]: c for c in all_contracts}
            missing = [cid for cid in scope_ids if cid not in by_id]
            if missing:
                raise ValidationError("合约不存在:%s" % ",".join(map(str, missing)))
            contracts = [by_id[cid] for cid in scope_ids]
        else:
            contracts = all_contracts

        batch_id = self.repository.create_batch(period, idem, snapshot, actor.user_id, scope_ids)
        self.repository.add_audit("batch", batch_id, "batch_created", actor.user_id,
                                  {"period": period, "rules": snapshot,
                                   "contract_count": len(contracts), "resumed": False})
        # 逐项目独立提交：写完批次头之后即使中断，重提同一幂等键也能识别并续作
        self._write_items(batch_id, contracts, rules, actor.user_id)
        self.repository.touch_batch(batch_id)
        return self._complete(self.repository.get_batch(batch_id), resumed=False, duplicate=False)

    @staticmethod
    def _flat_contract(contract: Dict[str, Any]) -> Dict[str, Any]:
        flat = dict(contract)
        flat.update(contract["payload"])
        return flat

    def _write_items(
        self,
        batch_id: int,
        contracts: Sequence[Dict[str, Any]],
        rules: EstimationRules,
        actor_id: str,
        reasons: Optional[Dict[int, List[str]]] = None,
    ) -> None:
        claims = self.repository.list_claims_for_contracts([c["id"] for c in contracts])
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for claim in claims:
            grouped.setdefault(claim["contract_id"], []).append(claim)

        for raw_contract in contracts:
            contract = self._flat_contract(raw_contract)
            contract_claims = grouped.get(contract["id"], [])
            frozen, backfilled = self._freeze_claims(batch_id, contract["id"], contract_claims, actor_id)
            result = evaluate_item(contract, frozen, rules)
            # 重算后清除“待重算”失效标记，保留本次变动原因作为可核对的依据说明
            item_reasons: List[str] = list((reasons or {}).get(contract["id"], []))
            item_reasons.extend(backfilled)
            self.repository.upsert_batch_item(
                {
                    "batch_id": batch_id,
                    "contract_id": contract["id"],
                    "status": result["status"],
                    "estimated_amount": result["estimated_amount"],
                    "pending_amount": result["pending_amount"],
                    "formal_amount": None,
                    "issue": result["issue"],
                    "basis": result["basis"],
                    "changed_reasons": item_reasons,
                }
            )

    def _freeze_claims(
        self,
        batch_id: int,
        contract_id: int,
        claims: Sequence[Dict[str, Any]],
        actor_id: str,
    ) -> "tuple[List[Dict[str, Any]], List[str]]":
        """冻结赔案版本；旧数据缺损失日期按事件号回填，补不全的留给评估判定待核对。"""
        frozen: List[Dict[str, Any]] = []
        backfilled: List[str] = []
        for claim in claims:
            claim = dict(claim)
            payload = dict(claim["payload"])
            if not payload.get("occurrence_date"):
                sibling_rows = self.repository.list_claims_by_event(claim["event_id"])
                siblings = [s["payload"] for s in sibling_rows if s["id"] != claim["id"]]
                date_value, _source = resolve_backfill_date(payload, siblings)
                if date_value:
                    self.repository.backfill_claim_date(
                        claim["id"], date_value,
                        "event_id:%s 同事件最早损失日期" % claim["event_id"],
                    )
                    payload["occurrence_date"] = date_value
                    backfilled.append(
                        "赔案%s损失日期按事件号%s回填为%s" % (claim["claim_ref"], claim["event_id"], date_value)
                    )
                    self.repository.add_audit(
                        "batch", batch_id, "loss_date_backfilled", actor_id,
                        {"contract_id": contract_id, "claim_id": claim["id"],
                         "claim_ref": claim["claim_ref"], "occurrence_date": date_value},
                    )
                    self.repository.add_audit(
                        "contract", contract_id, "loss_date_backfilled", actor_id,
                        {"batch_id": batch_id, "claim_id": claim["id"], "occurrence_date": date_value},
                    )
            claim["payload"] = payload
            claim.update(payload)
            frozen.append(claim)
        return frozen, backfilled

    def refresh_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        """封账前刷新：仅对赔案有变化(含补录)的受影响合约失效重算，其余项目保持原依据。"""
        actor = self._actor(actor)
        self._require_roles(actor, {"actuary"})
        batch = self.repository.get_batch(batch_id)
        if batch["state"] != BATCH_DRAFT:
            raise Conflict("批次已封账或已转正式，不能刷新重算")
        rules = EstimationRules.from_snapshot(batch["rules_snapshot"])
        items = self.repository.list_batch_items(batch_id)
        current_claims = self.repository.list_claims_for_contracts([item["contract_id"] for item in items])
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for claim in current_claims:
            grouped.setdefault(claim["contract_id"], []).append(claim)

        affected: List[int] = []
        unaffected: List[int] = []
        recompute_reasons: Dict[int, List[str]] = {}
        for item in items:
            contract_id = item["contract_id"]
            current = grouped.get(contract_id, [])
            frozen_by_id = {entry["claim_id"]: entry for entry in item["basis"].get("claims", [])}
            reasons: List[str] = []
            # 已有赔案：版本或字段变化
            for claim in current:
                frozen = frozen_by_id.get(claim["id"])
                if frozen is None:
                    reasons.append("赔案补录:%s(v%s)" % (claim["claim_ref"], claim["version"]))
                    continue
                if claim["version"] != frozen["claim_version"]:
                    diffs = compare_claim_snapshot(frozen, claim["payload"])
                    label = "赔案%s(v%s→v%s)" % (claim["claim_ref"], frozen["claim_version"], claim["version"])
                    reasons.append("%s %s" % (label, "、".join(diffs) if diffs else "版本变化"))
            stale_marks = [r for r in (item.get("changed_reasons") or []) if r.startswith(REASON_STALE)]
            if reasons or stale_marks:
                affected.append(contract_id)
                # 失效标记前缀只用于标识“待重算”；重算后剥掉前缀，保留可读的变动原因
                cleaned_stale = [r[len(REASON_STALE) + 1:].lstrip(":") for r in stale_marks]
                recompute_reasons[contract_id] = list(dict.fromkeys(reasons + cleaned_stale))
            else:
                unaffected.append(contract_id)

        if not affected:
            self.repository.add_audit("batch", batch_id, "refresh_no_change", actor.user_id, {})
            return self._complete(batch, resumed=False, duplicate=False, refreshed=0)

        contracts = [self.repository.get_contract(cid) for cid in affected]
        self._write_items(batch_id, contracts, rules, actor.user_id, reasons=recompute_reasons)
        self.repository.add_audit(
            "batch", batch_id, "items_recalculated", actor.user_id,
            {"affected_contract_ids": affected, "unaffected_contract_ids": unaffected,
             "reasons": recompute_reasons},
        )
        for cid in affected:
            self.repository.add_audit(
                "contract", cid, "batch_item_recalculated", actor.user_id,
                {"batch_id": batch_id, "reasons": recompute_reasons[cid]},
            )
        self.repository.touch_batch(batch_id)
        return self._complete(self.repository.get_batch(batch_id), resumed=False, duplicate=False, refreshed=len(affected))

    def close_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        """封账：冻结参与合约、赔案版本、估算规则和待复核金额。存在失效待重算项目时拒绝。"""
        actor = self._actor(actor)
        self._require_roles(actor, {"actuary"})
        batch = self.repository.get_batch(batch_id)
        if batch["state"] == BATCH_CLOSED:
            raise Conflict("批次已封账")
        if batch["state"] == BATCH_POSTED:
            raise Conflict("批次已转正式")
        items = self.repository.list_batch_items(batch_id)
        stale = [item["contract_id"] for item in items if _is_stale(item)]
        if stale:
            raise Conflict("存在赔案已变化但未重算的合约，请先刷新:%s" % ",".join(map(str, stale)))
        if not items:
            raise ValidationError("空批次不能封账")
        self.repository.close_batch(batch_id, actor.user_id)
        totals = _item_totals(items)
        self.repository.add_audit(
            "batch", batch_id, "batch_closed", actor.user_id,
            {"rules_snapshot": batch["rules_snapshot"], "totals": totals,
             "pending_reconciliation_contracts": [
                 item["contract_id"] for item in items if item["status"] == ITEM_PENDING_RECON]},
        )
        for item in items:
            self.repository.add_audit(
                "contract", item["contract_id"], "batch_closed", actor.user_id,
                {"batch_id": batch_id, "estimated_amount": item["estimated_amount"],
                 "pending_amount": item["pending_amount"], "status": item["status"]},
            )
        return self._complete(self.repository.get_batch(batch_id), resumed=False, duplicate=False)

    def review_and_post(
        self,
        actor: Actor,
        batch_id: int,
        adjustments: Optional[Dict[str, Any]] = None,
        confirmed: bool = False,
    ) -> Dict[str, Any]:
        """财务复核后转正式。adjustments: {contract_id: {"formal_amount": x, "reason": "..."}}。

        重复提交同一批次只留一份结果：已转正式的批次重入直接返回既有结果。
        """
        actor = self._actor(actor)
        self._require_roles(actor, {"finance"})
        if not confirmed:
            raise ValidationError("复核转正式需要confirmed=true")
        batch = self.repository.get_batch(batch_id)
        if batch["state"] == BATCH_POSTED:
            return self._complete(batch, resumed=False, duplicate=True, already_posted=True)
        if batch["state"] != BATCH_CLOSED:
            raise Conflict("只有已封账待复核批次才能转正式")
        items = self.repository.list_batch_items(batch_id)
        assessed = [item for item in items if item["status"] == ITEM_ASSESSED]
        parked = [item for item in items if item["status"] == ITEM_PENDING_RECON]
        postings: List[Dict[str, Any]] = []
        adjustment_log: List[Dict[str, Any]] = []
        adjustments = adjustments or {}
        for item in assessed:
            key = str(item["contract_id"])
            formal = item["estimated_amount"]
            if key in adjustments:
                adj = adjustments[key]
                formal = money(number(adj, "formal_amount", 0))
                adjustment_log.append(
                    {"contract_id": item["contract_id"],
                     "estimated_amount": item["estimated_amount"],
                     "formal_amount": formal,
                     "reason": optional_text(adj, "reason", "财务复核调整")}
                )
            postings.append(
                {"item_id": item["id"], "contract_id": item["contract_id"], "formal_amount": formal}
            )
        self.repository.mark_batch_posted(batch_id, postings, actor.user_id)
        self.repository.add_audit(
            "batch", batch_id, "batch_posted", actor.user_id,
            {"formal_count": len(postings),
             "parked_contracts": [item["contract_id"] for item in parked],
             "adjustments": adjustment_log},
        )
        for posting in postings:
            self.repository.add_audit(
                "contract", posting["contract_id"], "formal_reserve_posted", actor.user_id,
                {"batch_id": batch_id, "formal_amount": posting["formal_amount"]},
            )
        return self._complete(self.repository.get_batch(batch_id), resumed=False, duplicate=False, already_posted=False)

    def get_batch_detail(self, actor: Actor, batch_id: int, resume: bool = True) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        result = self._complete(batch, resumed=False, duplicate=False)
        # 写入失败恢复：批次头存在但项目缺失/不全时，凭幂等批次自动续作
        if resume and batch["state"] == BATCH_DRAFT:
            resumed = self._resume_if_incomplete(batch)
            if resumed:
                result = self._complete(self.repository.get_batch(batch_id), resumed=True, duplicate=False)
        return result

    def _resume_if_incomplete(self, batch: Dict[str, Any]) -> bool:
        # 续作必须按建批时冻结的参与合约范围恢复，不能扩张到其他合约
        scope = batch.get("contract_scope")
        scope_ids = sorted(_loads(scope)) if scope else None
        all_contracts = self.repository.list_contracts(limit=500)
        by_id = {c["id"]: c for c in all_contracts}
        if scope_ids is not None:
            contracts = [by_id[cid] for cid in scope_ids if cid in by_id]
        else:
            contracts = [c for c in all_contracts if c["state"] != "terminated"]
        items = self.repository.list_batch_items(batch["id"])
        present = {item["contract_id"] for item in items}
        missing = [c for c in contracts if c["id"] not in present]
        if not missing:
            return False
        rules = EstimationRules.from_snapshot(batch["rules_snapshot"])
        self._write_items(batch["id"], missing, rules, batch["created_by"])
        self.repository.add_audit(
            "batch", batch["id"], "batch_resumed", batch["created_by"],
            {"recovered_contract_ids": [c["id"] for c in missing]},
        )
        self.repository.touch_batch(batch["id"])
        return True

    def list_batches(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return [self._complete(batch, resumed=False, duplicate=False) for batch in self.repository.list_batches(limit)]

    def _complete(
        self,
        batch: Dict[str, Any],
        resumed: bool,
        duplicate: bool,
        refreshed: int = 0,
        already_posted: bool = False,
    ) -> Dict[str, Any]:
        items = self.repository.list_batch_items(batch["id"])
        totals = _item_totals(items)
        return {
            "batch": batch,
            "items": items,
            "totals": totals,
            "counts": {
                ITEM_ASSESSED: sum(1 for i in items if i["status"] == ITEM_ASSESSED),
                ITEM_PENDING_RECON: sum(1 for i in items if i["status"] == ITEM_PENDING_RECON),
                "stale": sum(1 for i in items if _is_stale(i)),
            },
            "resumed": resumed,
            "duplicate": duplicate,
            "refreshed_contracts": refreshed,
            "already_posted": already_posted,
        }

    # ---------- 查询 ----------
    def timeline(self, actor: Actor, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if entity_type not in {"contract", "claim", "batch"}:
            raise ValidationError("entity_type只能是contract/claim/batch")
        if entity_type == "contract":
            self.repository.get_contract(entity_id)
        elif entity_type == "claim":
            self.repository.get_claim(entity_id)
        else:
            self.repository.get_batch(entity_id)
        return self.repository.audit_timeline(entity_type, entity_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with _stats_view(self.repository) as view:
            return view


def _field_changed(old: Any, new: Any, field: str) -> bool:
    if field in {"estimated_loss", "approved_loss"}:
        if old is None and new is None:
            return False
        if old is None or new is None:
            return True
        return money(old) != money(new)
    return old != new


def _is_stale(item: Dict[str, Any]) -> bool:
    return any(str(r).startswith(REASON_STALE) for r in (item.get("changed_reasons") or []))


def _item_totals(items: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    assessed = [i for i in items if i["status"] == ITEM_ASSESSED]
    return {
        "estimated_total": money(sum(float(i["estimated_amount"]) for i in assessed)),
        "pending_review_total": money(sum(float(i["pending_amount"]) for i in assessed)),
        "formal_total": money(sum(float(i["formal_amount"]) for i in items if i.get("formal_amount") is not None)),
        "parked_contracts": len([i for i in items if i["status"] == ITEM_PENDING_RECON]),
    }


class _stats_view:
    """状态统计：批次状态、项目状态、待核对合约与正式金额汇总。"""

    def __init__(self, repository: Repository):
        self.repository = repository
        self.data: Dict[str, Any] = {}

    def __enter__(self) -> Dict[str, Any]:
        repo = self.repository
        batches = repo.list_batches(limit=500)
        batch_state_counts = {state: 0 for state in BATCH_STATES}
        item_status_counts = {ITEM_ASSESSED: 0, ITEM_PENDING_RECON: 0, "stale": 0}
        totals = {"estimated_total": 0.0, "pending_review_total": 0.0, "formal_total": 0.0}
        for batch in batches:
            batch_state_counts[batch["state"]] = batch_state_counts.get(batch["state"], 0) + 1
            for item in repo.list_batch_items(batch["id"]):
                item_status_counts[item["status"]] = item_status_counts.get(item["status"], 0) + 1
                if _is_stale(item):
                    item_status_counts["stale"] += 1
                if item["status"] == ITEM_ASSESSED:
                    totals["estimated_total"] += float(item["estimated_amount"])
                    totals["pending_review_total"] += float(item["pending_amount"])
                if item.get("formal_amount") is not None:
                    totals["formal_total"] += float(item["formal_amount"])
        contracts = repo.list_contracts(limit=500)
        pending = set(repo.pending_reconciliation_contracts())
        self.data = {
            "contracts": {
                "total": len(contracts),
                "active": len([c for c in contracts if c["state"] == "active"]),
                "terminated": len([c for c in contracts if c["state"] == "terminated"]),
            },
            "batches": {"total": len(batches), **batch_state_counts},
            "items": {
                ITEM_ASSESSED: item_status_counts[ITEM_ASSESSED],
                ITEM_PENDING_RECON: item_status_counts[ITEM_PENDING_RECON],
                "stale": item_status_counts["stale"],
                "pending_reconciliation_contract_ids": sorted(pending),
            },
            "amounts": {key: money(value) for key, value in totals.items()},
            "formal_by_period": repo.formal_totals(),
        }
        return self.data

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False
