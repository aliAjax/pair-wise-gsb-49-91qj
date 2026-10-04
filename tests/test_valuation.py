import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.repository import Repository
from src.reserves import ReserveRules


CLAIM_OFFICER = Actor("officer-1", "claims_officer")
FINANCE = Actor("finance-1", "finance")
UW = Actor("uw-1", "underwriter")

CREATE_DATA = {
    'event_id': 'CAT-2026-01', 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4,
    'loss_amount': 3000000.0, 'reinstatement_pct': 0.15, 'aggregate_prior': 0.0, 'loss_date': '2026-09-12',
}
RULES = {'ibnr_pct': 0.1, 'expense_pct': 0.05}


def bind_and_claim(service, reference='RI-26001', data=None, claim='CLM-1'):
    data = data or CREATE_DATA
    record = service.create(UW, reference, data)
    record = service.act(Actor("op", "underwriter"), record["id"], record["version"], "bind", {'underwriter_id': 'UW-8'})
    record = service.act(CLAIM_OFFICER, record["id"], record["version"], "submit_claim",
                         {'claim_number': claim, 'event_id': data['event_id'], 'loss_date': data.get('loss_date', '')})
    return record


def calculated(service, record, approved=2800000.0):
    return service.act(CLAIM_OFFICER, record["id"], record["version"], "calculate", {'approved_loss': approved})


def expected_estimate(gross, data=CREATE_DATA, rules=RULES):
    return ReserveRules().estimate({**data, 'approved_loss': gross}, rules)['estimated_amount']


class ValuationBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_full_seal_and_finance_post(self):
        record = calculated(self.service, bind_and_claim(self.service))
        expected = expected_estimate(2800000.0)

        batch = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        self.assertEqual(batch['state'], 'draft')
        self.assertEqual(batch['member_total'], 1)
        self.assertAlmostEqual(batch['estimated_total'], expected, places=2)

        items = self.service.valuation.list_items(CLAIM_OFFICER, batch['id'])
        self.assertEqual(items[0]['status'], 'estimated')
        self.assertEqual(items[0]['contract_version'], record['version'])
        self.assertEqual(items[0]['basis']['loss_date'], '2026-09-12')
        self.assertEqual(items[0]['rules'], RULES)

        with self.assertRaises(PermissionDenied):
            self.service.valuation.post_batch(CLAIM_OFFICER, batch['id'])
        sealed = self.service.valuation.seal_batch(FINANCE, batch['id'])
        self.assertEqual(sealed['state'], 'sealed')
        self.assertEqual(sealed['sealed_by'], 'finance-1')

        # 封账后不允许再刷新重算
        with self.assertRaises(Conflict):
            self.service.valuation.refresh_batch(CLAIM_OFFICER, batch['id'])

        posted = self.service.valuation.post_batch(FINANCE, batch['id'])
        self.assertEqual(posted['state'], 'posted')
        self.assertAlmostEqual(posted['formal_total'], expected, places=2)
        self.assertAlmostEqual(posted['estimated_total'], expected, places=2)

        timeline = self.service.valuation.timeline(FINANCE, batch['id'])
        actions = [event['action'] for event in timeline]
        self.assertEqual(actions, ['created', 'evaluated', 'sealed', 'posted'])
        self.assertEqual(timeline[-1]['details']['formal_total'], posted['formal_total'])

        # 合约详情能核对批次与正式金额
        detail = self.service.valuation.engagement_detail(FINANCE, record['id'])
        self.assertEqual(len(detail['engagements']), 1)
        self.assertEqual(detail['engagements'][0]['batch_reference'], 'RES-2026-09')
        self.assertAlmostEqual(detail['engagements'][0]['formal_amount'], expected, places=2)

        record_timeline = self.service.timeline(FINANCE, record['id'])
        self.assertIn('reserve_posted', [event['action'] for event in record_timeline])

    def test_claim_change_before_seal_only_invalidates_affected_contract(self):
        first = calculated(self.service, bind_and_claim(self.service, 'RI-26001', {**CREATE_DATA}, 'CLM-1'))
        second_data = dict(CREATE_DATA)
        second_data['event_id'] = 'CAT-2026-02'
        second = calculated(self.service, bind_and_claim(self.service, 'RI-26002', second_data, 'CLM-2'))

        batch = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        self.assertEqual(batch['member_total'], 2)

        # 核定金额变化（赔案版本推进）
        first = self.service.act(CLAIM_OFFICER, first["id"], first["version"], "recalculate", {'approved_loss': 3600000.0})
        refreshed = self.service.valuation.refresh_batch(CLAIM_OFFICER, batch['id'])
        self.assertEqual(len(refreshed['invalidated']), 1)
        self.assertEqual(refreshed['invalidated'][0]['record_id'], first['id'])

        items = self.service.valuation.list_items(CLAIM_OFFICER, batch['id'])
        rows = {item['record_id']: item for item in items if item['contract_version'] == items[-1]['contract_version'] or item['status'] != 'invalid'}
        first_items = [item for item in items if item['record_id'] == first['id']]
        self.assertEqual({item['status'] for item in first_items}, {'invalid', 'estimated'})
        new_first = [item for item in first_items if item['status'] == 'estimated'][0]
        self.assertEqual(new_first['contract_version'], first['version'])
        self.assertIn('核定金额变化', new_first['change_reason'] if new_first['change_reason'] else '')
        # 第二个合约依据不动
        second_items = [item for item in items if item['record_id'] == second['id']]
        self.assertEqual(len(second_items), 1)
        self.assertEqual(second_items[0]['status'], 'estimated')

        # 撤销赔案：失效后按撤销重算为不计提
        second = self.service.act(CLAIM_OFFICER, second["id"], second["version"], "withdraw_claim", {'withdraw_reason': '重复报案'})
        refreshed = self.service.valuation.refresh_batch(CLAIM_OFFICER, batch['id'])
        self.assertEqual(refreshed['invalidated'][0]['reason'], '赔案撤销：重复报案')
        second_items = self.service.valuation.list_items(CLAIM_OFFICER, batch['id'])
        latest_second = [item for item in second_items if item['record_id'] == second['id'] and item['status'] != 'invalid'][0]
        self.assertEqual(latest_second['status'], 'excluded')
        self.assertEqual(latest_second['estimated_amount'], 0.0)

        sealed = self.service.valuation.seal_batch(FINANCE, batch['id'])
        self.assertEqual(sealed['state'], 'sealed')
        self.assertAlmostEqual(sealed['estimated_total'], expected_estimate(3600000.0), places=2)

        # 封账后赔案再变，已封账批次保留原依据
        self.service.act(CLAIM_OFFICER, first["id"], first["version"], "recalculate", {'approved_loss': 4200000.0})
        with self.assertRaises(Conflict):
            self.service.valuation.refresh_batch(CLAIM_OFFICER, batch['id'])
        posted = self.service.valuation.post_batch(FINANCE, batch['id'])
        self.assertAlmostEqual(posted['formal_total'], expected_estimate(3600000.0), places=2)

        # 已封账批次的时间线保留失效重算的变动原因
        timeline = self.service.valuation.timeline(FINANCE, batch['id'])
        reasons = [entry['details'] for entry in timeline if entry['action'] == 'refreshed']
        self.assertTrue(any(any('核定金额变化' in inv['reason'] for inv in d['invalidated']) for d in reasons))

    def test_backlog_claim_is_appended_to_draft_batch(self):
        first = calculated(self.service, bind_and_claim(self.service))
        batch = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        self.assertEqual(batch['member_total'], 1)

        second_data = dict(CREATE_DATA)
        second_data['event_id'] = 'CAT-2026-03'
        second = calculated(self.service, bind_and_claim(self.service, 'RI-26003', second_data, 'CLM-3'))
        refreshed = self.service.valuation.refresh_batch(CLAIM_OFFICER, batch['id'])
        self.assertEqual(refreshed['appended'], ['RI-26003'])
        self.assertEqual(refreshed['progress']['member_total'], 2)

    def test_idempotent_create_seal_and_post(self):
        calculated(self.service, bind_and_claim(self.service))
        first = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        again = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        self.assertTrue(again['idempotent_replay'])
        self.assertEqual(again['id'], first['id'])
        batches = self.service.valuation.list_batches(CLAIM_OFFICER)
        self.assertEqual(len(batches), 1)

        self.service.valuation.seal_batch(FINANCE, first['id'])
        sealed_again = self.service.valuation.seal_batch(FINANCE, first['id'])
        self.assertTrue(sealed_again['idempotent_replay'])
        self.service.valuation.post_batch(FINANCE, first['id'])
        posted_again = self.service.valuation.post_batch(FINANCE, first['id'])
        self.assertTrue(posted_again['idempotent_replay'])
        timeline = self.service.valuation.timeline(FINANCE, first['id'])
        self.assertEqual([event['action'] for event in timeline].count('sealed'), 1)
        self.assertEqual([event['action'] for event in timeline].count('posted'), 1)

    def test_resume_after_write_failure(self):
        first = calculated(self.service, bind_and_claim(self.service, 'RI-26001'))
        second_data = dict(CREATE_DATA)
        second_data['event_id'] = 'CAT-2026-09'
        second = calculated(self.service, bind_and_claim(self.service, 'RI-26009', second_data, 'CLM-9'))

        batch = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        self.assertEqual(batch['member_total'], 2)

        original_upsert = Repository.upsert_item
        calls = {'count': 0}

        def flaky_upsert(self, item):
            calls['count'] += 1
            if calls['count'] == 1:
                raise RuntimeError("模拟写盘失败")
            return original_upsert(self, item)

        # 新批次：前两条落库，第三条写入失败，完整批次保留可续作
        third_data = dict(CREATE_DATA)
        third_data['event_id'] = 'CAT-2026-10'
        third = calculated(self.service, bind_and_claim(self.service, 'RI-26010', third_data, 'CLM-10'))

        Repository.upsert_item = flaky_upsert
        try:
            with self.assertRaises(RuntimeError):
                self.service.valuation.refresh_batch(CLAIM_OFFICER, batch['id'])
        finally:
            Repository.upsert_item = original_upsert

        detail = self.service.valuation.get_batch(CLAIM_OFFICER, batch['id'])
        self.assertEqual(detail['member_total'], 3)
        self.assertGreaterEqual(detail['pending'], 1)

        resumed = self.service.valuation.resume_batch(CLAIM_OFFICER, batch['id'])
        self.assertEqual(resumed['progress']['pending'] if 'pending' in resumed['progress'] else 0, 0)
        self.assertEqual(resumed['progress']['cursor'], 3)
        self.assertEqual(len(self.service.valuation.list_items(CLAIM_OFFICER, batch['id'])), 3)

    def test_missing_loss_date_backfilled_by_event_or_stops_for_review(self):
        # 同事件号的旧数据已有损失日期，用于回填
        anchor_data = dict(CREATE_DATA)
        anchor_data['loss_date'] = '2026-08-20'
        self.service.create(UW, 'RI-OLD-1', anchor_data)

        # 补录赔案时缺失损失日期：按事件号回填
        no_date = dict(CREATE_DATA)
        no_date.pop('loss_date')
        record = self.service.create(UW, 'RI-26020', no_date)
        record = self.service.act(Actor("op", "underwriter"), record["id"], record["version"], "bind", {'underwriter_id': 'UW-8'})
        record = self.service.act(CLAIM_OFFICER, record["id"], record["version"], "submit_claim",
                                  {'claim_number': 'CLM-20', 'event_id': no_date['event_id']})
        record = self.service.act(CLAIM_OFFICER, record["id"], record["version"], "calculate", {'approved_loss': 2500000.0})

        # 事件号完全新、无任何日期可回填的合约
        orphan = dict(CREATE_DATA)
        orphan['event_id'] = 'CAT-NEW-X'
        orphan.pop('loss_date')
        orphan_record = self.service.create(UW, 'RI-26021', orphan)
        orphan_record = self.service.act(Actor("op", "underwriter"), orphan_record["id"], orphan_record["version"], "bind", {'underwriter_id': 'UW-8'})
        orphan_record = self.service.act(CLAIM_OFFICER, orphan_record["id"], orphan_record["version"], "submit_claim",
                                         {'claim_number': 'CLM-21', 'event_id': 'CAT-NEW-X'})
        orphan_record = self.service.act(CLAIM_OFFICER, orphan_record["id"], orphan_record["version"], "calculate", {'approved_loss': 1800000.0})

        batch = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        items = {item['record_id']: item for item in self.service.valuation.list_items(CLAIM_OFFICER, batch['id'])}
        self.assertEqual(items[record['id']]['status'], 'estimated')
        self.assertEqual(items[record['id']]['loss_date'], '2026-08-20')
        self.assertEqual(items[record['id']]['basis']['loss_date_source'], 'event_backfill')
        self.assertEqual(items[orphan_record['id']]['status'], 'needs_verification')
        self.assertIn('无法回填', items[orphan_record['id']]['change_reason'])

        # 待核对合约阻止封账
        with self.assertRaises(Conflict):
            self.service.valuation.seal_batch(FINANCE, batch['id'])

        repaired = self.service.valuation.repair_item(CLAIM_OFFICER, batch['id'], orphan_record['id'], '2026-09-28')
        self.assertEqual(repaired['status'], 'estimated')
        self.assertEqual(repaired['basis']['loss_date_source'], 'manual_repair')

        sealed = self.service.valuation.seal_batch(FINANCE, batch['id'])
        posted = self.service.valuation.post_batch(FINANCE, batch['id'])
        self.assertEqual(posted['state'], 'posted')

    def test_batch_stats(self):
        calculated(self.service, bind_and_claim(self.service))
        batch = self.service.valuation.create_batch(CLAIM_OFFICER, 'RES-2026-09', '2026-09', RULES)
        stats = self.service.valuation.stats(FINANCE)
        self.assertEqual(stats['batches']['draft'], 1)
        self.assertEqual(stats['items']['estimated']['count'], 1)
        self.service.valuation.seal_batch(FINANCE, batch['id'])
        self.service.valuation.post_batch(FINANCE, batch['id'])
        stats = self.service.valuation.stats(FINANCE)
        self.assertEqual(stats['batches']['posted'], 1)


if __name__ == '__main__':
    unittest.main()
