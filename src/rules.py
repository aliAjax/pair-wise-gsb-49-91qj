"""未决赔款准备金评估的领域规则：合约要素、赔案版本、估算规则与批次项目计算。

金额一律四舍五入到分(2位小数)。所有计算不依赖数据库，便于单测与封账快照复核。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import (
    Actor,
    Conflict,
    ValidationError,
    boolean,
    iso_date,
    number,
    optional_text,
    text,
)

MONEY = 2

# 合约生命周期
CONTRACT_ACTIVE = "active"
CONTRACT_TERMINATED = "terminated"

# 赔案状态：已报未决 / 已核定 / 已撤销
CLAIM_OPEN = "open"
CLAIM_REASSESSED = "reassessed"
CLAIM_WITHDRAWN = "withdrawn"
CLAIM_STATUSES = (CLAIM_OPEN, CLAIM_REASSESSED, CLAIM_WITHDRAWN)

# 批次状态：待封账 / 已封账待复核 / 已转正式
BATCH_DRAFT = "open"
BATCH_CLOSED = "closed"
BATCH_POSTED = "posted"
BATCH_STATES = (BATCH_DRAFT, BATCH_CLOSED, BATCH_POSTED)

# 批次内合约项目状态：已评估 / 待核对(补不全损失日期等)
ITEM_ASSESSED = "assessed"
ITEM_PENDING_RECON = "pending_reconciliation"

DEFAULT_RULE_ID = "default"

ROLES = {
    "underwriter",       # 承保：维护合约
    "claims_officer",    # 理赔：补录/撤销/核定赔案
    "actuary",           # 精算：建批/刷新/封账
    "finance",           # 财务：复核并转正式
    "admin",
}

CREATE_CONTRACT_ROLES = {"underwriter", "admin"}
CLAIM_ROLES = {"claims_officer", "admin"}
BATCH_ROLES = {"actuary", "admin"}
REVIEW_ROLES = {"finance", "admin"}


def money(value: float) -> float:
    return round(float(value) + 0.0, MONEY)


def known_role(role: str) -> bool:
    return role in ROLES


class EstimationRules:
    """估算规则。封账批次冻结的是规则参数快照，后续规则调整不影响历史批次。"""

    def __init__(self, ibnr_factor: float = 0.1, discount_factor: float = 1.0):
        if ibnr_factor < 0:
            raise ValidationError("ibnr_factor不能为负")
        if not 0 < discount_factor <= 1:
            raise ValidationError("discount_factor必须在(0,1]之间")
        self.ibnr_factor = ibnr_factor
        self.discount_factor = discount_factor

    def as_snapshot(self, rule_id: str = DEFAULT_RULE_ID) -> Dict[str, Any]:
        return {
            "rule_id": rule_id,
            "ibnr_factor": self.ibnr_factor,
            "discount_factor": self.discount_factor,
            "version": 1,
        }

    @classmethod
    def from_snapshot(cls, snapshot: Dict[str, Any]) -> "EstimationRules":
        return cls(
            ibnr_factor=float(snapshot.get("ibnr_factor", 0.1)),
            discount_factor=float(snapshot.get("discount_factor", 1.0)),
        )


def validate_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload)
    text(p, "contract_ref")
    text(p, "counterparty")
    iso_date(p, "inception_date")
    iso_date(p, "expiry_date")
    attachment = number(p, "attachment", 0)
    limit = number(p, "limit", 0)
    cession_pct = number(p, "cession_pct", 0, 1)
    if limit <= attachment:
        raise ValidationError("赔偿限额必须高于起赔点")
    if p["inception_date"] > p["expiry_date"]:
        raise ValidationError("起保日不能晚于到期日")
    p["cession_pct"] = cession_pct
    return p


def validate_claim(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload)
    text(p, "claim_ref")
    text(p, "event_id")
    iso_date(p, "occurrence_date", required=False)
    status = p.get("status", CLAIM_OPEN)
    if status not in CLAIM_STATUSES:
        raise ValidationError("status只能是%s" % "/".join(CLAIM_STATUSES))
    p["status"] = status
    estimated_loss = number(p, "estimated_loss", 0)
    p["estimated_loss"] = estimated_loss
    approved = p.get("approved_loss")
    if approved is None:
        p["approved_loss"] = None
    else:
        if isinstance(approved, bool) or not isinstance(approved, (int, float)):
            raise ValidationError("approved_loss必须是数字或null")
        p["approved_loss"] = money(approved)
    p["review_flag"] = boolean(p, "review_flag", False)
    p["note"] = optional_text(p, "note")
    if status == CLAIM_REASSESSED and p["approved_loss"] is None:
        raise ValidationError("已核定赔案必须提供approved_loss")
    return p


def claim_reserve_basis(claim: Dict[str, Any]) -> Tuple[float, str]:
    """单笔赔案的准备金毛口径：已核定用核定金额，已报未决用报损估算，撤销为0。"""
    status = claim["status"]
    if status == CLAIM_WITHDRAWN:
        return 0.0, "withdrawn_zero"
    if status == CLAIM_REASSESSED:
        return money(claim["approved_loss"]), "approved_loss"
    return money(claim.get("estimated_loss", 0.0)), "estimated_loss"


def _ceded(layer: Dict[str, Any], gross: float) -> float:
    width = money(float(layer["limit"]) - float(layer["attachment"]))
    in_layer = min(max(0.0, gross - float(layer["attachment"])), width)
    return money(in_layer * float(layer["cession_pct"]))


def compare_claim_snapshot(frozen: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    """对比冻结赔案快照与当前赔案，返回变动原因列表；空列表表示无变化。"""
    reasons: List[str] = []
    watched = {
        "status": "状态变化",
        "estimated_loss": "报损金额变化",
        "approved_loss": "核定金额变化",
        "occurrence_date": "损失日期补录",
        "review_flag": "复核标记变化",
    }
    for field, label in watched.items():
        old = frozen.get(field)
        new = current.get(field)
        # 金额按分比较，避免浮点噪声
        if field in {"estimated_loss", "approved_loss"}:
            old = None if old is None else money(old)
            new = None if new is None else money(new)
        if old != new:
            if field == "status":
                reasons.append("%s(%s→%s)" % (label, old, new))
            else:
                reasons.append(label)
    return reasons


def evaluate_item(
    contract: Dict[str, Any],
    frozen_claims: List[Dict[str, Any]],
    rules: EstimationRules,
) -> Dict[str, Any]:
    """根据冻结赔案版本与规则快照评估单个合约项目。

    返回待冻结的项目依据(basis)与金额。补不全损失日期的合约停在待核对。
    """
    missing_date = [c["claim_ref"] for c in frozen_claims if not c.get("occurrence_date")]
    basis_claims: List[Dict[str, Any]] = []
    gross = 0.0
    pending_gross = 0.0
    for claim in frozen_claims:
        amount, source = claim_reserve_basis(claim)
        gross += amount
        if claim.get("review_flag"):
            pending_gross += amount
        basis_claims.append(
            {
                "claim_id": claim["id"],
                "claim_ref": claim["claim_ref"],
                "event_id": claim["event_id"],
                "claim_version": claim["version"],
                "status": claim["status"],
                "estimated_loss": money(claim.get("estimated_loss", 0.0)),
                "approved_loss": claim.get("approved_loss"),
                "occurrence_date": claim.get("occurrence_date"),
                "review_flag": bool(claim.get("review_flag")),
                "basis_source": source,
                "gross_basis": money(amount),
            }
        )

    issue: Optional[str] = None
    status = ITEM_ASSESSED
    if missing_date:
        status = ITEM_PENDING_RECON
        issue = "赔案缺少损失日期且无法按事件号回填: %s" % ", ".join(sorted(missing_date))

    assessed_gross = max(0.0, gross - pending_gross)
    assessed_ceded = _ceded(contract, assessed_gross)
    # 待复核部分的分层摊回用“整体摊回差额”近似，保证拆分合计等于整体摊回
    total_ceded = _ceded(contract, gross)
    pending_ceded = money(total_ceded - assessed_ceded)
    assessed_with_ibnr = money(assessed_ceded * (1.0 + rules.ibnr_factor) * rules.discount_factor)
    pending_with_ibnr = money(pending_ceded * (1.0 + rules.ibnr_factor) * rules.discount_factor)

    basis = {
        "contract_id": contract["id"],
        "contract_ref": contract["contract_ref"],
        "layer": {
            "attachment": money(contract["attachment"]),
            "limit": money(contract["limit"]),
            "cession_pct": contract["cession_pct"],
        },
        "rules": rules.as_snapshot(contract.get("rule_id", DEFAULT_RULE_ID)),
        "claims": basis_claims,
        "gross_basis": money(gross),
        "pending_gross": money(pending_gross),
        "ceded_before_ibnr": total_ceded,
    }
    return {
        "status": status,
        "issue": issue,
        "estimated_amount": assessed_with_ibnr,
        "pending_amount": pending_with_ibnr,
        "basis": basis,
    }


def resolve_backfill_date(claim: Dict[str, Any], siblings: Iterable[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """旧数据缺损失日期时，按同一事件号的其他赔案回填最早损失日期。"""
    if claim.get("occurrence_date"):
        return claim["occurrence_date"], None
    dates = sorted(s.get("occurrence_date") for s in siblings if s.get("occurrence_date"))
    if not dates:
        return None, None
    return dates[0], dates[0]
