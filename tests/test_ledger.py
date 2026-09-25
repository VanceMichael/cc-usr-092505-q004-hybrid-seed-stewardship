"""谱系台账：重量守恒、并行占用、抽样扣减、损耗、谱系回溯。"""

import unittest

from helpers import ALICE, CAROL, DAVE, build_service, plot, submit
from hybrid_seed_stewardship.errors import LedgerError, SealedVersionError
from hybrid_seed_stewardship.models import NodeKind


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.contract = submit(self.svc, "武夷种业", "V两优1号", ALICE)
        self.svc.bind_plots(self.contract, [plot("P1"), plot("P2")], CAROL)

    def harvest(self, node_id: str, plot_id: str, batch: str, weight: float) -> None:
        self.svc.record_harvest(node_id, self.contract, 2, plot_id, batch, weight, ALICE)

    def test_harvest_opens_batch_with_full_availability(self) -> None:
        self.harvest("N1", "P1", "B1", 1000.0)
        origins = self.svc.plot_origins("B1")
        self.assertEqual(len(origins), 1)
        self.assertEqual(origins[0].plot_id, "P1")
        self.assertEqual(origins[0].parent_female_batch, "F-V两优1号-2026-01")

    def test_split_conserves_weight(self) -> None:
        self.harvest("N1", "P1", "B1", 1000.0)
        self.svc.record_transformation(
            "N2", NodeKind.SPLIT, self.contract, 2,
            {"B1": 1000.0}, {"B1-a": 600.0, "B1-b": 400.0}, ALICE,
        )
        # 再拆 400 与守恒相抵触的输出必须被拒
        with self.assertRaises(LedgerError):
            self.svc.record_transformation(
                "N3", NodeKind.SPLIT, self.contract, 2,
                {"B1": 400.0}, {"B1-c": 400.0}, ALICE,
            )

    def test_parallel_transformations_cannot_double_occupy(self) -> None:
        self.harvest("N1", "P1", "B1", 1000.0)
        # 第一次占用 600 成功
        self.svc.record_transformation(
            "N2", NodeKind.SPLIT, self.contract, 2,
            {"B1": 600.0}, {"B1-a": 600.0}, ALICE,
        )
        # 并行事件再申请 500（余量只剩 400）必须失败
        with self.assertRaises(LedgerError):
            self.svc.record_transformation(
                "N3", NodeKind.SPLIT, self.contract, 2,
                {"B1": 500.0}, {"B1-b": 500.0}, ALICE,
            )

    def test_merge_conserves_and_preserves_multi_origin(self) -> None:
        self.harvest("N1", "P1", "B1", 700.0)
        self.harvest("N2", "P2", "B2", 300.0)
        self.svc.record_transformation(
            "N3", NodeKind.MERGE, self.contract, 2,
            {"B1": 700.0, "B2": 300.0}, {"B3": 1000.0}, ALICE,
        )
        origins = self.svc.plot_origins("B3")
        self.assertEqual({o.plot_id for o in origins}, {"P1", "P2"})

    def test_blend_then_process_with_loss_conserves(self) -> None:
        self.harvest("N1", "P1", "B1", 500.0)
        self.harvest("N2", "P2", "B2", 500.0)
        # 混样：抽走 2kg 做样品后，其余进入混样批
        self.svc.draw_sample("S1", "B1", 1.0, "N1", DAVE)
        self.svc.draw_sample("S2", "B2", 1.0, "N2", DAVE)
        self.svc.record_transformation(
            "N3", NodeKind.BLEND, self.contract, 2,
            {"B1": 499.0, "B2": 499.0}, {"B3": 998.0}, ALICE,
        )
        # 加工允许登记损耗
        self.svc.record_transformation(
            "N4", NodeKind.PROCESS, self.contract, 2,
            {"B3": 998.0}, {"B4": 980.0}, ALICE, processing_loss_kg=18.0,
        )
        # 输入不存在或输出重复
        with self.assertRaises(LedgerError):
            self.svc.record_transformation(
                "N5", NodeKind.PROCESS, self.contract, 2,
                {"GHOST": 10.0}, {"X": 10.0}, ALICE,
            )
        with self.assertRaises(LedgerError):
            self.svc.record_transformation(
                "N6", NodeKind.PROCESS, self.contract, 2,
                {"B4": 980.0}, {"B4": 980.0}, ALICE,
            )

    def test_process_loss_only_allowed_in_processing(self) -> None:
        self.harvest("N1", "P1", "B1", 100.0)
        with self.assertRaises(LedgerError):
            self.svc.record_transformation(
                "N2", NodeKind.MERGE, self.contract, 2,
                {"B1": 100.0}, {"B2": 99.0}, ALICE, processing_loss_kg=1.0,
            )

    def test_sample_weight_occupies_quantity(self) -> None:
        self.harvest("N1", "P1", "B1", 100.0)
        self.svc.draw_sample("S1", "B1", 5.0, "N1", DAVE)
        # 抽样占用 5kg 后，全部 100kg 不能再被加工占用
        with self.assertRaises(LedgerError):
            self.svc.record_transformation(
                "N2", NodeKind.PROCESS, self.contract, 2,
                {"B1": 100.0}, {"B2": 100.0}, ALICE,
            )
        # 95kg 可以
        self.svc.record_transformation(
            "N2", NodeKind.PROCESS, self.contract, 2,
            {"B1": 95.0}, {"B2": 95.0}, ALICE,
        )

    def test_released_batch_is_frozen(self) -> None:
        self.harvest("N1", "P1", "B1", 100.0)
        self.svc.draw_sample("S1", "B1", 1.0, "N1", DAVE)
        from hybrid_seed_stewardship.models import Verdict
        self.svc.record_inspection("S1", DAVE, Verdict.QUALIFIED, {"纯度": 99.0})
        self.svc.release_batch("B1", DAVE)
        with self.assertRaises(SealedVersionError):
            self.svc.record_transformation(
                "N2", NodeKind.PROCESS, self.contract, 2,
                {"B1": 99.0}, {"B2": 99.0}, ALICE,
            )

    def test_harvest_must_reference_bound_plot_at_version(self) -> None:
        from hybrid_seed_stewardship.errors import ContractError, ValidationError
        with self.assertRaises(ValidationError):
            self.svc.record_harvest("N1", self.contract, 2, "P1", "B1", 0.0, ALICE)
        with self.assertRaises(ContractError):
            self.svc.record_harvest("N2", self.contract, 2, "PX", "B2", 100.0, ALICE)


if __name__ == "__main__":
    unittest.main()
