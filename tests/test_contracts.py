"""合同版本、受托人资格、隔离与花期冲突、可见性规则。"""

import unittest

from helpers import (
    ALICE,
    BOB,
    CAROL,
    DAVE,
    ENTERPRISE_A,
    ENTERPRISE_B,
    VARIETY_A,
    VARIETY_B,
    build_service,
    plot,
    submit,
)
from hybrid_seed_stewardship.errors import (
    EligibilityError,
    IsolationError,
    ValidationError,
)


class ContractVersionTest(unittest.TestCase):
    def test_enterprise_submission_creates_version_one(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
        contract = svc.current_contract(contract_id)
        self.assertEqual(contract.version, 1)
        self.assertFalse(contract.is_bound)
        self.assertEqual(contract.submission.rights.variety, VARIETY_A)

    def test_county_binding_derives_new_version_and_seals_old(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
        bound = svc.bind_plots(contract_id, [plot("P1")], CAROL, note="首轮绑定")
        self.assertEqual(bound.version, 2)
        self.assertEqual(bound.supersedes, 1)
        # v1 仍可读取且不含地块
        history = svc.contract_history(contract_id)
        self.assertEqual(history[0].version, 1)
        self.assertEqual(history[0].bindings, ())
        self.assertEqual(history[-1].bindings[0].plot_id, "P1")

    def test_field_checks_add_another_version(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
        svc.bind_plots(contract_id, [plot("P1")], CAROL)
        v3 = svc.add_field_checks(contract_id, "P1", ["花检合格", "去雄到位"], CAROL)
        self.assertEqual(v3.version, 3)
        self.assertIn("花检合格", v3.bindings[0].field_checks)

    def test_enterprise_cannot_bind(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
        with self.assertRaises(PermissionError):
            svc.bind_plots(contract_id, [plot("P1")], ALICE)

    def test_cross_enterprise_submission_rejected(self) -> None:
        svc = build_service()
        with self.assertRaises(PermissionError):
            submit(svc, ENTERPRISE_B, VARIETY_A, ALICE)


class EligibilityAndIsolationTest(unittest.TestCase):
    def test_grower_below_grade_rejected(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE, min_grade=3)
        with self.assertRaises(EligibilityError):
            svc.bind_plots(contract_id, [plot("P1", grade=2)], CAROL)

    def test_isolation_below_protocol_rejected(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE, isolation=300)
        with self.assertRaises(IsolationError):
            svc.bind_plots(contract_id, [plot("P1", isolation=200)], CAROL)

    def test_sowing_outside_window_rejected(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE, window=(100, 120))
        with self.assertRaises(IsolationError):
            svc.bind_plots(contract_id, [plot("P1", sowing=135)], CAROL)

    def test_adjacent_different_variety_flowering_conflict(self) -> None:
        svc = build_service()
        contract_a = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE, isolation=100)
        # 甲企业地块：花期 200-210，位于原点
        svc.bind_plots(contract_a, [plot("P1", loc=(0.0, 0.0), flowering=(200, 210))], CAROL)

        contract_b = submit(svc, ENTERPRISE_B, VARIETY_B, BOB, isolation=100)
        # 乙企业相邻地块仅 50m 且花期重叠
        with self.assertRaises(IsolationError):
            svc.bind_plots(
                contract_b,
                [plot("Q1", loc=(0.0, 0.05), flowering=(205, 215))],
                CAROL,
            )

    def test_same_variety_or_nonoverlapping_flowering_allowed(self) -> None:
        svc = build_service()
        contract_a = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE, isolation=100)
        svc.bind_plots(contract_a, [plot("P1", loc=(0.0, 0.0), flowering=(200, 210))], CAROL)
        contract_b = submit(svc, ENTERPRISE_B, VARIETY_B, BOB, isolation=100)
        # 距离不足但花期不重叠
        bound = svc.bind_plots(
            contract_b,
            [plot("Q1", loc=(0.0, 0.02), flowering=(230, 240))],
            CAROL,
        )
        self.assertEqual(bound.version, 2)

    def test_duplicate_plot_in_one_version_rejected(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
        with self.assertRaises(ValidationError):
            svc.bind_plots(contract_id, [plot("P1"), plot("P1")], CAROL)


class VisibilityTest(unittest.TestCase):
    def test_enterprise_sees_own_contract(self) -> None:
        svc = build_service()
        contract_id = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
        view = svc.view_contract(contract_id, ALICE)
        self.assertEqual(view.contract_id, contract_id)

    def test_enterprise_cannot_see_competitor_detail(self) -> None:
        svc = build_service()
        contract_b = submit(svc, ENTERPRISE_B, VARIETY_B, BOB)
        with self.assertRaises(PermissionError):
            svc.view_contract(contract_b, ALICE)

    def test_county_and_inspector_see_all(self) -> None:
        svc = build_service()
        contract_b = submit(svc, ENTERPRISE_B, VARIETY_B, BOB)
        self.assertEqual(svc.view_contract(contract_b, CAROL).version, 1)
        self.assertEqual(svc.view_contract(contract_b, DAVE).version, 1)


if __name__ == "__main__":
    unittest.main()
