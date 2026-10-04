"""再保险合约与巨灾暴露管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "quoted"
CREATE_ROLES = {'underwriter'}
ACTION_ROLES = {'bind': {'underwriter'}, 'submit_claim': {'claims_officer'}, 'calculate': {'claims_officer'}, 'recalculate': {'claims_officer'}, 'withdraw_claim': {'claims_officer'}, 'settle': {'finance'}, 'reject': {'finance', 'claims_officer'}}
TRANSITIONS = {'bind': {'quoted': 'bound'}, 'submit_claim': {'bound': 'claim_submitted'}, 'calculate': {'claim_submitted': 'calculated'}, 'recalculate': {'calculated': 'calculated'}, 'withdraw_claim': {'claim_submitted': 'claim_withdrawn', 'calculated': 'claim_withdrawn'}, 'settle': {'calculated': 'settled'}, 'reject': {'claim_submitted': 'rejected', 'calculated': 'rejected'}}
OPEN_CLAIM_STATES = {'claim_submitted', 'calculated'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "event_id")
        attachment = number(p, "attachment", 0)
        limit = number(p, "limit", 0)
        number(p, "cession_pct", 0, 1)
        number(p, "loss_amount", 0)
        number(p, "reinstatement_pct", 0, 1)
        number(p, "aggregate_prior", 0)
        loss_date = p.get("loss_date", "")
        if loss_date not in ("", None):
            self.require_loss_date(loss_date)
            p["loss_date"] = str(loss_date).strip()
        if limit <= attachment:
            raise ValidationError("赔款限额必须高于起赔点")
        return p

    @staticmethod
    def require_loss_date(value: Any) -> str:
        import re
        if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()):
            raise ValidationError("损失日期必须为YYYY-MM-DD")
        return value.strip()

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        width = float(p["limit"]) - float(p["attachment"])
        retained_loss = max(0.0, float(p["loss_amount"]) - float(p["attachment"]))
        recovery = min(retained_loss, width) * float(p["cession_pct"])
        p["layer_width"] = round(width, 2)
        p["recoverable_amount"] = round(recovery, 2)
        p["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
        p["net_retention"] = round(float(p["loss_amount"]) - recovery, 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        event_id = payload.get("event_id")
        used = float(payload.get("aggregate_prior", 0))
        for item in existing:
            if item["state"] in {"rejected"} or item["payload"].get("event_id") != event_id:
                continue
            used += float(item["payload"].get("recoverable_amount", 0))
        capacity = float(payload["layer_width"]) * float(payload["cession_pct"])
        projected = min(max(0.0, float(payload["loss_amount"]) - float(payload["attachment"])), float(payload["layer_width"])) * float(payload["cession_pct"])
        if used + projected > capacity + 0.01:
            raise Conflict("同一事件累计摊回超过再保容量")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            summary = "再保合约已绑定"
        elif action == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            changes["claim_event_id"] = text(data, "event_id")
            loss_date = data.get("loss_date", p.get("loss_date", ""))
            if loss_date:
                changes["loss_date"] = self.require_loss_date(loss_date)
            summary = "赔案已提交"
        elif action == "calculate":
            loss = number(data, "approved_loss", 0)
            width = float(p["layer_width"])
            recovery = min(max(0.0, loss - float(p["attachment"])), width) * float(p["cession_pct"])
            changes["approved_loss"] = loss
            changes["recoverable_amount"] = round(recovery, 2)
            changes["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
            loss_date = data.get("loss_date")
            if loss_date:
                changes["loss_date"] = self.require_loss_date(loss_date)
            summary = "摊回金额已计算"
        elif action == "recalculate":
            loss = number(data, "approved_loss", 0)
            previous = float(p.get("approved_loss", 0))
            width = float(p["layer_width"])
            recovery = min(max(0.0, loss - float(p["attachment"])), width) * float(p["cession_pct"])
            changes["approved_loss"] = loss
            changes["recoverable_amount"] = round(recovery, 2)
            changes["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
            loss_date = data.get("loss_date")
            if loss_date:
                changes["loss_date"] = self.require_loss_date(loss_date)
            summary = "核定金额由%s调整为%s，准备金依据需重算" % (previous, loss)
        elif action == "withdraw_claim":
            changes["withdraw_reason"] = text(data, "withdraw_reason")
            changes["recoverable_amount"] = 0.0
            summary = "赔案已撤销"
        elif action == "settle":
            if float(p["recoverable_amount"]) <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            summary = "摊回赔款已结算"
        elif action == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒绝"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
