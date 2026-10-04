"""失败场景：版本冲突、写入中断续作、旧数据回填、待核对合约不转正式、待复核金额。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.rules import CLAIM_OPEN, CLAIM_REASSESSED, ITEM_PENDING_RECON


UW = Actor("uw1", "underwriter")
CLM = Actor("clm1", "claims_officer")
ACT = Actor("act1", "actuary")
FIN = Actor("fin1", "finance")


def contract_payload(ref):
    return {
        "contract_ref": ref, "counterparty": "SwissRe",
        "inception_date": "2026-01-01", "expiry_date": "2026-12-31",
        "attachment": 1_000_000.0, "limit": 5_000_000.0, "cession_pct": 0.4,
    }


def claim_payload(ref, event="EVT-1", estimated=3_000_000.0, date="2026-09-10", review=False,
                  status=CLAIM_OPEN, approved=None):
    return {
        "claim_ref": ref, "event_id": event, "status": status,
        "estimated_loss": estimated, "approved_loss": approved,
        "occurrence_date": date, "review_flag": review,
    }


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_optimistic_conflict_and_immutable_fields(self):
        contract = self.service.create_contract(UW, contract_payload("RI-1"))
        claim = self.service.register_claim(CLM, contract["id"], claim_payload("CL-1"))
        with self.assertRaises(Conflict):
            self.service.amend_claim(
                CLM, claim["id"], claim["version"] - 1,
                claim_payload("CL-1", status=CLAIM_REASSESSED, approved=2_800_000.0))
        # 事件号不可改
        with self.assertRaises(ValidationError):
            self.service.amend_claim(
                CLM, claim["id"], claim["version"],
                claim_payload("CL-1", event="EVT-CHANGED"))
        # 无实际变化
        with self.assertRaises(Conflict):
            self.service.amend_claim(CLM, claim["id"], claim["version"], claim_payload("CL-1"))
        # 合约编号唯一
        with self.assertRaises(Conflict):
            self.service.create_contract(UW, contract_payload("RI-1"))

    def test_recover_from_incomplete_batch_write(self):
        c1 = self.service.create_contract(UW, contract_payload("RI-1"))
        c2 = self.service.create_contract(UW, contract_payload("RI-2"))
        c3 = self.service.create_contract(UW, contract_payload("RI-3"))
        for c in (c1, c2, c3):
            self.service.register_claim(CLM, c["id"], claim_payload("CL-" + c["payload"]["contract_ref"]))

        # 模拟在第2个项目写入后崩溃：批次头已落盘、项目不全
        real_upsert = self.service.repository.upsert_batch_item
        calls = {"n": 0}

        def flaky(item):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
            return real_upsert(item)

        self.service.repository.upsert_batch_item = flaky
        with self.assertRaises(RuntimeError):
            self.service.create_batch(ACT, "2026-09", "KEY-RESUME")
        self.service.repository.upsert_batch_item = real_upsert

        # 新进程同一幂等键续作：从完整批次恢复，不新建批次、不重复项目
        service2 = build_service(self.db)
        recovered = service2.create_batch(ACT, "2026-09", "KEY-RESUME")
        self.assertTrue(recovered["duplicate"])
        self.assertTrue(recovered["resumed"])
        self.assertEqual(len(recovered["items"]), 3)
        self.assertEqual({i["contract_id"] for i in recovered["items"]}, {c1["id"], c2["id"], c3["id"]})

        # 再重提：无需恢复，仍是同一份
        third = service2.create_batch(ACT, "2026-09", "KEY-RESUME")
        self.assertTrue(third["duplicate"])
        self.assertFalse(third["resumed"])
        self.assertEqual(len(third["items"]), 3)
        timeline = service2.timeline(FIN, "batch", recovered["batch"]["id"])
        self.assertIn("batch_resumed", [e["action"] for e in timeline])

    def test_scoped_batch_resume_does_not_expand(self):
        c1 = self.service.create_contract(UW, contract_payload("RI-1"))
        c2 = self.service.create_contract(UW, contract_payload("RI-2"))
        self.service.register_claim(CLM, c1["id"], claim_payload("CL-1"))
        self.service.register_claim(CLM, c2["id"], claim_payload("CL-2"))
        # 批次范围只含合约1，且在写入任何项目前崩溃
        bid = self.service.repository.create_batch(
            "2026-09", "KEY-SCOPE", self._rules_snapshot(), "act1", [c1["id"]])
        detail = self.service.get_batch_detail(ACT, bid)
        self.assertTrue(detail["resumed"])
        self.assertEqual([i["contract_id"] for i in detail["items"]], [c1["id"]])

    def test_resume_via_detail_endpoint(self):
        c1 = self.service.create_contract(UW, contract_payload("RI-1"))
        self.service.register_claim(CLM, c1["id"], claim_payload("CL-1"))
        bid = self.service.repository.create_batch("2026-09", "KEY-ORPHAN",
                                                   self._rules_snapshot(), "act1", [c1["id"]])
        # 批次头存在但一个项目都没写
        detail = self.service.get_batch_detail(ACT, bid)
        self.assertTrue(detail["resumed"])
        self.assertEqual(len(detail["items"]), 1)

    @staticmethod
    def _rules_snapshot():
        return {"rule_id": "default", "ibnr_factor": 0.1, "discount_factor": 1.0, "version": 1}

    def test_legacy_backfill_and_parked_contract(self):
        # 合约1：同事件号兄弟赔案带日期 → 回填
        c1 = self.service.create_contract(UW, contract_payload("RI-1"))
        self.service.register_claim(CLM, c1["id"], claim_payload("CL-A", event="EVT-B", date="2026-08-15"))
        old = self.service.register_claim(CLM, c1["id"], claim_payload("CL-B", event="EVT-B", date=None))
        # 合约2：孤立事件、无日期 → 补不全，停在待核对
        c2 = self.service.create_contract(UW, contract_payload("RI-2"))
        self.service.register_claim(CLM, c2["id"], claim_payload("CL-ORPHAN", event="EVT-X", date=None))

        batch = self.service.create_batch(ACT, "2026-09", "KEY-LEGACY", ibnr_factor=0.0)
        items = {i["contract_id"]: i for i in batch["items"]}
        self.assertEqual(items[c1["id"]]["status"], "assessed")
        self.assertTrue(any("回填" in r for r in items[c1["id"]]["changed_reasons"]))
        self.assertEqual(items[c2["id"]]["status"], ITEM_PENDING_RECON)
        self.assertIn("CL-ORPHAN", items[c2["id"]]["issue"])

        # 回填已写回赔案本身(数据清洗，不递增版本)
        backfilled = self.service.repository.get_claim(old["id"])
        self.assertEqual(backfilled["payload"]["occurrence_date"], "2026-08-15")
        self.assertEqual(backfilled["version"], 1)

        # 待核对不阻止封账；转正式只落已核对合约，待核对合约单独停着
        self.service.close_batch(ACT, batch["batch"]["id"])
        posted = self.service.review_and_post(FIN, batch["batch"]["id"], confirmed=True)
        formal = self.service.repository.list_formal_for_batch(batch["batch"]["id"])
        self.assertEqual({row["contract_id"] for row in formal}, {c1["id"]})
        self.assertTrue(posted["totals"]["parked_contracts"] >= 1)

        stats = self.service.stats(FIN)
        self.assertIn(c2["id"], stats["items"]["pending_reconciliation_contract_ids"])
        detail = self.service.get_contract_detail(FIN, c2["id"])
        self.assertTrue(detail["has_pending_reconciliation"])
        self.assertEqual(detail["formal_reserves"], [])

    def test_pending_review_amount_frozen(self):
        c1 = self.service.create_contract(UW, contract_payload("RI-1"))
        self.service.register_claim(
            CLM, c1["id"],
            claim_payload("CL-1", estimated=3_000_000.0, review=True))
        batch = self.service.create_batch(ACT, "2026-09", "KEY-REVIEW", ibnr_factor=0.0)
        item = batch["items"][0]
        self.assertEqual(item["estimated_amount"], 0.0)
        self.assertEqual(item["pending_amount"], 800_000.0)
        self.assertEqual(batch["totals"]["pending_review_total"], 800_000.0)
        # 默认转正式金额只认已评估部分，待复核金额不入正式
        self.service.close_batch(ACT, batch["batch"]["id"])
        posted = self.service.review_and_post(FIN, batch["batch"]["id"], confirmed=True)
        self.assertEqual(posted["totals"]["formal_total"], 0.0)

    def test_post_retry_keeps_single_result(self):
        c1 = self.service.create_contract(UW, contract_payload("RI-1"))
        self.service.register_claim(CLM, c1["id"], claim_payload("CL-1"))
        batch = self.service.create_batch(ACT, "2026-09", "KEY-ONCE")
        self.service.close_batch(ACT, batch["batch"]["id"])
        first = self.service.review_and_post(
            FIN, batch["batch"]["id"],
            adjustments={str(c1["id"]): {"formal_amount": 42.0, "reason": "x"}}, confirmed=True)
        self.assertEqual(first["totals"]["formal_total"], 42.0)
        # 重试时即便带了不同金额，也不能覆盖已落账结果(只留一份)
        retry = self.service.review_and_post(
            FIN, batch["batch"]["id"],
            adjustments={str(c1["id"]): {"formal_amount": 999.0, "reason": "y"}}, confirmed=True)
        self.assertTrue(retry["already_posted"])
        rows = self.service.repository.list_formal_for_batch(batch["batch"]["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["formal_amount"], 42.0)
