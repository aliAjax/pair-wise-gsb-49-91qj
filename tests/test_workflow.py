"""端到端流程：冻结-赔案变化-只重算受影响合约-封账-财务复核转正式-审计核对。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import CLAIM_OPEN, CLAIM_REASSESSED, CLAIM_WITHDRAWN


UW = Actor("uw1", "underwriter")
CLM = Actor("clm1", "claims_officer")
ACT = Actor("act1", "actuary")
FIN = Actor("fin1", "finance")


def contract_payload(ref, attachment=1_000_000.0, limit=5_000_000.0, cession=0.4):
    return {
        "contract_ref": ref, "counterparty": "MunichRe",
        "inception_date": "2026-01-01", "expiry_date": "2026-12-31",
        "attachment": attachment, "limit": limit, "cession_pct": cession,
    }


def claim_payload(ref, event="EVT-1", status=CLAIM_OPEN, estimated=3_000_000.0,
                  approved=None, date="2026-09-10", review=False):
    return {
        "claim_ref": ref, "event_id": event, "status": status,
        "estimated_loss": estimated, "approved_loss": approved,
        "occurrence_date": date, "review_flag": review,
    }


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.c1 = self.service.create_contract(UW, contract_payload("RI-1"))
        self.c2 = self.service.create_contract(UW, contract_payload("RI-2"))
        self.cl1 = self.service.register_claim(CLM, self.c1["id"], claim_payload("CL-1"))
        self.cl2 = self.service.register_claim(CLM, self.c2["id"], claim_payload("CL-2"))

    def tearDown(self):
        self.temp.cleanup()

    def test_full_freeze_close_post_and_idempotency(self):
        batch = self.service.create_batch(ACT, "2026-09", "KEY-09", ibnr_factor=0.0)
        self.assertEqual(batch["batch"]["state"], "open")
        self.assertEqual(len(batch["items"]), 2)
        self.assertEqual(batch["totals"]["estimated_total"], 1_600_000.0)  # 800,000 * 2

        # 重复提交同一批次：只留一份结果
        again = self.service.create_batch(ACT, "2026-09", "KEY-09", ibnr_factor=0.0)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["batch"]["id"], batch["batch"]["id"])
        self.assertEqual(len(again["items"]), 2)

        # 精算才能封账；封账后转正式必须财务复核
        with self.assertRaises(PermissionDenied):
            self.service.close_batch(FIN, batch["batch"]["id"])
        closed = self.service.close_batch(ACT, batch["batch"]["id"])
        self.assertEqual(closed["batch"]["state"], "closed")

        with self.assertRaises(Conflict):
            self.service.close_batch(ACT, batch["batch"]["id"])
        with self.assertRaises(PermissionDenied):
            self.service.review_and_post(ACT, batch["batch"]["id"], confirmed=True)
        with self.assertRaises(ValidationError):
            self.service.review_and_post(FIN, batch["batch"]["id"])  # 未确认

        # 财务复核，对合约2做调整
        posted = self.service.review_and_post(
            FIN, batch["batch"]["id"],
            adjustments={str(self.c2["id"]): {"formal_amount": 700_000.0, "reason": "扣除摊回争议款"}},
            confirmed=True,
        )
        self.assertEqual(posted["batch"]["state"], "posted")
        self.assertEqual(posted["totals"]["formal_total"], 1_500_000.0)

        # 重复提交已转正式批次：只留一份结果
        repeated = self.service.review_and_post(FIN, batch["batch"]["id"], confirmed=True)
        self.assertTrue(repeated["already_posted"])
        self.assertEqual(len(self.service.repository.list_formal_for_batch(batch["batch"]["id"])), 2)

        # 已封账/已转正式批次不受后续赔案变化影响
        self.service.amend_claim(
            CLM, self.cl1["id"], self.cl1["version"],
            claim_payload("CL-1", status=CLAIM_WITHDRAWN, estimated=3_000_000.0, date="2026-09-10"),
            change_reason="赔案撤销")
        detail = self.service.get_batch_detail(FIN, batch["batch"]["id"])
        c1_item = next(i for i in detail["items"] if i["contract_id"] == self.c1["id"])
        self.assertEqual(c1_item["estimated_amount"], 800_000.0)  # 原依据保留
        self.assertEqual(c1_item["changed_reasons"], [])

        stats = self.service.stats(FIN)
        self.assertEqual(stats["batches"]["posted"], 1)
        self.assertEqual(stats["amounts"]["formal_total"], 1_500_000.0)

    def test_claim_change_before_close_only_recomputes_affected_contract(self):
        batch = self.service.create_batch(ACT, "2026-09", "KEY-09", ibnr_factor=0.0)
        bid = batch["batch"]["id"]

        # 合约1赔案核定金额变化(自动打失效标记)；合约2不动
        self.service.amend_claim(
            CLM, self.cl1["id"], self.cl1["version"],
            claim_payload("CL-1", status=CLAIM_REASSESSED, estimated=3_000_000.0, approved=5_000_000.0),
            change_reason="定损完成")
        detail = self.service.get_batch_detail(ACT, bid)
        c1_item = next(i for i in detail["items"] if i["contract_id"] == self.c1["id"])
        c2_item = next(i for i in detail["items"] if i["contract_id"] == self.c2["id"])
        self.assertEqual(detail["counts"]["stale"], 1)
        self.assertTrue(any("核定金额" in r for r in c1_item["changed_reasons"]))
        self.assertEqual(c2_item["changed_reasons"], [])

        # 带失效项目不能封账
        with self.assertRaises(Conflict):
            self.service.close_batch(ACT, bid)

        # 刷新：只重算合约1(核定500万 → 层内400万*0.4=160万)；合约2金额依据不动
        refreshed = self.service.refresh_batch(ACT, bid)
        self.assertEqual(refreshed["refreshed_contracts"], 1)
        c1_item = next(i for i in refreshed["items"] if i["contract_id"] == self.c1["id"])
        c2_item = next(i for i in refreshed["items"] if i["contract_id"] == self.c2["id"])
        self.assertEqual(c1_item["estimated_amount"], 1_600_000.0)
        self.assertEqual(c2_item["estimated_amount"], 800_000.0)
        self.assertEqual(refreshed["counts"]["stale"], 0)
        self.assertTrue(any("核定金额" in r for r in c1_item["changed_reasons"]))

        # 再刷新无变化
        again = self.service.refresh_batch(ACT, bid)
        self.assertEqual(again["refreshed_contracts"], 0)
        self.service.close_batch(ACT, bid)

        # 审计时间线：批次能核对变动原因与正式金额
        batch_timeline = self.service.timeline(FIN, "batch", bid)
        actions = [e["action"] for e in batch_timeline]
        self.assertIn("item_invalidated", actions)
        self.assertIn("items_recalculated", actions)
        self.assertIn("batch_closed", actions)
        recalc = next(e for e in batch_timeline if e["action"] == "items_recalculated")
        self.assertIn(self.c1["id"], recalc["details"]["affected_contract_ids"])
        self.assertIn(self.c2["id"], recalc["details"]["unaffected_contract_ids"])

        contract_timeline = self.service.timeline(FIN, "contract", self.c1["id"])
        contract_actions = [e["action"] for e in contract_timeline]
        self.assertIn("claim_amended", contract_actions)
        self.assertIn("batch_item_recalculated", contract_actions)

        detail = self.service.get_contract_detail(FIN, self.c1["id"])
        self.assertEqual(detail["batch_participation"][0]["batch_state"], "closed")

    def test_new_claim_registration_invalidates_open_batch(self):
        batch = self.service.create_batch(ACT, "2026-09", "KEY-09", ibnr_factor=0.0)
        bid = batch["batch"]["id"]
        new_claim = self.service.register_claim(
            CLM, self.c2["id"],
            claim_payload("CL-3", event="EVT-2", estimated=2_000_000.0, date="2026-09-20"))
        refreshed = self.service.refresh_batch(ACT, bid)
        c2_item = next(i for i in refreshed["items"] if i["contract_id"] == self.c2["id"])
        # 两笔合计毛口径500万 → 摊回160万
        self.assertEqual(c2_item["estimated_amount"], 1_600_000.0)
        refs = [c["claim_ref"] for c in c2_item["basis"]["claims"]]
        self.assertIn("CL-3", refs)

    def test_role_guards(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_contract(CLM, contract_payload("RI-X"))
        with self.assertRaises(PermissionDenied):
            self.service.register_claim(UW, self.c1["id"], claim_payload("CL-X"))
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(FIN, "2026-09", "K")
