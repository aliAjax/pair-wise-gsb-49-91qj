"""纯规则计算：分层摊回、IBNR与折现、待复核拆分、回填与变动检测。"""
import unittest

from src.rules import (
    CLAIM_OPEN,
    CLAIM_REASSESSED,
    CLAIM_WITHDRAWN,
    EstimationRules,
    compare_claim_snapshot,
    evaluate_item,
    resolve_backfill_date,
    validate_claim,
    validate_contract,
)
from src.domain import ValidationError


CONTRACT = {
    "id": 1,
    "contract_ref": "RI-2026-001",
    "counterparty": "MunichRe",
    "inception_date": "2026-01-01",
    "expiry_date": "2026-12-31",
    "attachment": 1_000_000.0,
    "limit": 5_000_000.0,
    "cession_pct": 0.4,
}


def claim(cid, ref, status=CLAIM_OPEN, estimated=0.0, approved=None, date="2026-09-10", review=False, event="EVT-1"):
    return {
        "id": cid, "claim_ref": ref, "event_id": event, "version": 1, "status": status,
        "estimated_loss": estimated, "approved_loss": approved, "occurrence_date": date,
        "review_flag": review,
    }


class RulesTest(unittest.TestCase):
    def test_layered_ceded_with_ibnr_and_discount(self):
        rules = EstimationRules(ibnr_factor=0.1, discount_factor=1.0)
        # 毛口径 3,000,000：超起赔点 2,000,000 在层内，摊回 800,000，IBNR后 880,000
        result = evaluate_item(CONTRACT, [claim(1, "CL-1", estimated=3_000_000.0)], rules)
        self.assertEqual(result["status"], "assessed")
        self.assertEqual(result["estimated_amount"], 880_000.0)
        self.assertEqual(result["basis"]["ceded_before_ibnr"], 800_000.0)
        self.assertEqual(result["basis"]["rules"]["ibnr_factor"], 0.1)

    def test_assessed_and_pending_review_split(self):
        rules = EstimationRules(ibnr_factor=0.0)
        claims = [
            claim(1, "CL-1", estimated=3_000_000.0),               # 摊回 800,000
            claim(2, "CL-2", estimated=3_000_000.0, review=True),  # 待复核 800,000
        ]
        result = evaluate_item(CONTRACT, claims, rules)
        self.assertEqual(result["estimated_amount"], 800_000.0)
        self.assertEqual(result["pending_amount"], 800_000.0)
        # 合计等于整体摊回，不重不漏
        gross = 6_000_000.0
        total_layer = min(max(0.0, gross - 1_000_000.0), 4_000_000.0) * 0.4
        self.assertAlmostEqual(result["estimated_amount"] + result["pending_amount"], total_layer, 2)

    def test_withdrawn_and_reassessed_basis(self):
        rules = EstimationRules(ibnr_factor=0.0)
        claims = [
            claim(1, "CL-1", status=CLAIM_REASSESSED, estimated=5_000_000.0, approved=2_500_000.0),
            claim(2, "CL-2", status=CLAIM_WITHDRAWN, estimated=9_000_000.0),
        ]
        result = evaluate_item(CONTRACT, claims, rules)
        # 仅核定 2,500,000 入账：层内 1,500,000 * 0.4
        self.assertEqual(result["estimated_amount"], 600_000.0)
        sources = {c["claim_ref"]: c["basis_source"] for c in result["basis"]["claims"]}
        self.assertEqual(sources, {"CL-1": "approved_loss", "CL-2": "withdrawn_zero"})

    def test_missing_loss_date_parks_item(self):
        rules = EstimationRules()
        result = evaluate_item(CONTRACT, [claim(1, "CL-OLD", estimated=2_000_000.0, date=None)], rules)
        self.assertEqual(result["status"], "pending_reconciliation")
        self.assertIn("CL-OLD", result["issue"])

    def test_backfill_by_event_id_uses_earliest(self):
        target = {"event_id": "EVT-9", "occurrence_date": None}
        siblings = [
            {"event_id": "EVT-9", "occurrence_date": "2026-08-03"},
            {"event_id": "EVT-9", "occurrence_date": "2026-07-20"},
        ]
        date_value, source = resolve_backfill_date(target, siblings)
        self.assertEqual(date_value, "2026-07-20")
        self.assertEqual(source, "2026-07-20")
        self.assertIsNone(resolve_backfill_date({"event_id": "X", "occurrence_date": None},
                                                [{"event_id": "X"}])[0])

    def test_change_detection_labels(self):
        before = {"status": CLAIM_OPEN, "estimated_loss": 3_000_000.0, "approved_loss": None,
                  "occurrence_date": "2026-09-01", "review_flag": False}
        after = dict(before, status=CLAIM_WITHDRAWN)
        reasons = compare_claim_snapshot(before, after)
        self.assertIn("状态变化(open→withdrawn)", reasons)
        self.assertEqual(compare_claim_snapshot(before, before), [])

    def test_validation_rules(self):
        bad = dict(CONTRACT, limit=500_000.0)
        with self.assertRaises(ValidationError):
            validate_contract(bad)
        with self.assertRaises(ValidationError):
            validate_claim({"claim_ref": "C", "event_id": "E", "status": CLAIM_REASSESSED,
                            "estimated_loss": 1.0})  # 已核定缺approved_loss
