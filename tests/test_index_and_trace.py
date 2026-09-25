"""匿名价格指数门槛、报价追问、成品袋扫码追溯。"""

import unittest

from helpers import (
    ALICE,
    BOB,
    CAROL,
    DAVE,
    FRANK,
    PERIOD,
    ENTERPRISE_A,
    ENTERPRISE_B,
    VARIETY_A,
    VARIETY_B,
    build_service,
    grow_release_ready_batch,
    plot,
    submit,
)
from hybrid_seed_stewardship.errors import QuotationError
from hybrid_seed_stewardship.models import (
    NodeKind,
    Quotation,
    ThresholdRule,
)
from hybrid_seed_stewardship.service import Role


def quote(quote_id: str, enterprise: str, variety: str, price: float, qty: float) -> Quotation:
    return Quotation(
        quote_id=quote_id,
        enterprise=enterprise,
        variety=variety,
        price_per_kg=price,
        quantity_kg=qty,
        period=PERIOD,
    )


class PriceIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.rule = ThresholdRule(
            period=PERIOD, variety=VARIETY_A,
            min_quantity_kg=5000, min_quotes=3,
            price_floor=10.0, price_ceiling=20.0,
        )
        self.svc.set_threshold(self.rule, FRANK)

    def test_only_threshold_passing_quotes_enter_index(self) -> None:
        svc = self.svc
        svc.submit_quotation(quote("Q1", ENTERPRISE_A, VARIETY_A, 12.0, 8000), ALICE)
        svc.submit_quotation(quote("Q2", ENTERPRISE_B, VARIETY_A, 14.0, 6000), BOB)
        # 第三条：数量不足
        svc.submit_quotation(quote("Q3", "第三种业", VARIETY_A, 13.0, 1000), self._register("第三种业", "erin"))
        # 数量够但价格超上限
        svc.submit_quotation(quote("Q4", "第四种业", VARIETY_A, 25.0, 9000), self._register("第四种业", "hank"))

        # 只有 2 条通过，未达最小报价家数，指数不发布
        pub = svc.publish_index(PERIOD, VARIETY_A, FRANK)
        self.assertFalse(pub.published)
        self.assertIsNone(pub.index_value)
        self.assertEqual(set(pub.included), {"Q1", "Q2"})
        self.assertIn("低于门槛", pub.excluded["Q3"])
        self.assertIn("超出区间", pub.excluded["Q4"])

        # 追加一条合格报价后重新编制（用新品种期模拟下一期）
        rule2 = ThresholdRule(PERIOD, VARIETY_B, 5000, 2, 10.0, 20.0)
        svc.set_threshold(rule2, FRANK)
        svc.submit_quotation(quote("Q5", ENTERPRISE_A, VARIETY_B, 12.0, 5000), ALICE)
        svc.submit_quotation(quote("Q6", ENTERPRISE_B, VARIETY_B, 18.0, 10000), BOB)
        pub2 = svc.publish_index(PERIOD, VARIETY_B, FRANK)
        self.assertTrue(pub2.published)
        # 数量加权：(12*5000 + 18*10000) / 15000 = 16
        self.assertAlmostEqual(pub2.index_value, 16.0, places=4)

    def _register(self, enterprise: str, actor: str) -> str:
        self.svc.register_actor(actor, Role.ENTERPRISE, enterprise)
        return actor

    def test_explain_inclusion_and_exclusion(self) -> None:
        svc = self.svc
        svc.submit_quotation(quote("Q1", ENTERPRISE_A, VARIETY_A, 12.0, 8000), ALICE)
        svc.submit_quotation(quote("Q2", ENTERPRISE_B, VARIETY_A, 14.0, 6000), BOB)
        svc.submit_quotation(quote("Q3", "第三种业", VARIETY_A, 13.0, 1000), self._register("第三种业", "erin"))
        svc.publish_index(PERIOD, VARIETY_A, FRANK)
        self.assertIn("通过全部门槛", svc.explain_quote(PERIOD, VARIETY_A, "Q1", FRANK))
        self.assertIn("低于门槛", svc.explain_quote(PERIOD, VARIETY_A, "Q3", FRANK))

    def test_enterprise_cannot_explain_competitor_quote(self) -> None:
        svc = self.svc
        svc.submit_quotation(quote("Q1", ENTERPRISE_A, VARIETY_A, 12.0, 8000), ALICE)
        svc.submit_quotation(quote("Q2", ENTERPRISE_B, VARIETY_A, 14.0, 6000), BOB)
        svc.publish_index(PERIOD, VARIETY_A, FRANK)
        with self.assertRaises(PermissionError):
            svc.explain_quote(PERIOD, VARIETY_A, "Q2", ALICE)
        # 本企业报价可追问
        self.assertIn("通过", svc.explain_quote(PERIOD, VARIETY_A, "Q1", ALICE))

    def test_duplicate_quote_same_period_rejected(self) -> None:
        self.svc.submit_quotation(quote("Q1", ENTERPRISE_A, VARIETY_A, 12.0, 8000), ALICE)
        with self.assertRaises(QuotationError):
            self.svc.submit_quotation(quote("Q9", ENTERPRISE_A, VARIETY_A, 13.0, 8000), ALICE)

    def test_enterprise_cannot_publish_index(self) -> None:
        self.svc.submit_quotation(quote("Q1", ENTERPRISE_A, VARIETY_A, 12.0, 8000), ALICE)
        with self.assertRaises(PermissionError):
            self.svc.publish_index(PERIOD, VARIETY_A, ALICE)


class BagScanTraceTest(unittest.TestCase):
    def test_scan_shows_plots_parents_inspections_and_release(self) -> None:
        svc = build_service()
        contract = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
        svc.bind_plots(contract, [plot("P1"), plot("P2")], CAROL)
        svc.record_harvest("N1", contract, 2, "P1", "B1", 600.0, ALICE)
        svc.record_harvest("N2", contract, 2, "P2", "B2", 400.0, ALICE)
        svc.record_transformation(
            "N3", NodeKind.MERGE, contract, 2,
            {"B1": 600.0, "B2": 400.0}, {"B3": 1000.0}, ALICE,
        )
        svc.draw_sample("S3", "B3", 2.0, "N3", DAVE)
        svc.record_transformation(
            "N4", NodeKind.PROCESS, contract, 2,
            {"B3": 998.0}, {"B4": 990.0}, ALICE, processing_loss_kg=8.0,
        )
        # 对成品批检验后放行，包装成品袋
        svc.draw_sample("S4", "B4", 1.0, "N4", DAVE)
        from hybrid_seed_stewardship.models import Verdict
        svc.record_inspection("S4", DAVE, Verdict.QUALIFIED, {"纯度": 99.1, "净度": 99.0})
        svc.release_batch("B4", DAVE)
        svc.package_bag("BAG-0001", "B4", 25.0, "包装岗-01")

        view = svc.scan_bag("BAG-0001", DAVE)
        self.assertEqual(view.final_batch, "B4")
        self.assertEqual({o.plot_id for o in view.origins}, {"P1", "P2"})
        self.assertTrue(all(o.parent_female_batch.startswith("F-") for o in view.origins))
        self.assertTrue(all(o.parent_male_batch.startswith("M-") for o in view.origins))
        kinds = [n.kind for n in view.node_chain]
        self.assertIn(NodeKind.HARVEST, kinds)
        self.assertIn(NodeKind.MERGE, kinds)
        self.assertIn(NodeKind.PROCESS, kinds)
        self.assertTrue(any(c.verdict.value == "qualified" for c in view.inspections))
        self.assertTrue(view.releases[-1].released)
        self.assertEqual(view.release_state.value, "released")

    def test_scan_requires_inspector_and_real_bag(self) -> None:
        svc = build_service()
        with self.assertRaises(PermissionError):
            svc.scan_bag("BAG-X", ALICE)
        grow_release_ready_batch(svc)
        svc.package_bag("BAG-0001", "B1", 25.0, "包装岗-01")
        from hybrid_seed_stewardship.errors import LedgerError
        with self.assertRaises(LedgerError):
            svc.scan_bag("BAG-404", DAVE)


if __name__ == "__main__":
    unittest.main()
