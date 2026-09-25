"""检验复核、放行职责分离、替代版本纠正、风险与资格变化边界。"""

import unittest

from helpers import ALICE, CAROL, DAVE, build_service, grow_release_ready_batch
from hybrid_seed_stewardship.errors import (
    InspectionError,
    ReleaseError,
    SealedVersionError,
)
from hybrid_seed_stewardship.models import Verdict


class InspectionAndReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        from helpers import ENTERPRISE_A, VARIETY_A, plot, submit
        self.svc = build_service()
        # 一条尚未放行的链：合同 v2 + 收获 + 抽样 + 合格初检
        self.contract = submit(self.svc, ENTERPRISE_A, VARIETY_A, ALICE)
        self.svc.bind_plots(self.contract, [plot("P1", grower="G1")], CAROL)
        self.svc.record_harvest("N-H-B1", self.contract, 2, "P1", "B1", 1000.0, ALICE)
        self.svc.draw_sample("S-B1", "B1", 1.0, "N-H-B1", DAVE)
        self.svc.record_inspection("S-B1", DAVE, Verdict.QUALIFIED,
                                   {"纯度": 99.0, "发芽率": 90.0})

    def test_producer_cannot_release(self) -> None:
        with self.assertRaises(ReleaseError):
            self.svc.release_batch("B1", ALICE)

    def test_independent_inspector_releases(self) -> None:
        record = self.svc.release_batch("B1", DAVE)
        self.assertTrue(record.released)
        self.assertEqual(record.producer, ALICE)
        self.assertNotEqual(record.released_by, record.producer)

    def test_unqualified_batch_blocked(self) -> None:
        # 复核改为不合格，放行被阻止
        self.svc.review_inspection("S-B1", DAVE, Verdict.UNQUALIFIED,
                                   {"纯度": 94.0}, note="花检存疑")
        with self.assertRaises(ReleaseError):
            self.svc.release_batch("B1", DAVE)

    def test_review_only_before_release(self) -> None:
        self.svc.release_batch("B1", DAVE)
        with self.assertRaises(SealedVersionError):
            self.svc.review_inspection("S-B1", DAVE, Verdict.UNQUALIFIED, {"纯度": 90.0})

    def test_correction_after_release_keeps_history_and_revokes(self) -> None:
        self.svc.release_batch("B1", DAVE)
        corrected = self.svc.correct_conclusion(
            "B1", DAVE, Verdict.UNQUALIFIED, {"纯度": 93.0}, note="南繁复检纯度不足"
        )
        self.assertEqual(corrected.version, 2)
        self.assertEqual(corrected.supersedes, 1)
        # 历史结论仍在
        history = self.svc._conclusions["B1"]
        self.assertEqual(history[0].verdict, Verdict.QUALIFIED)
        self.assertEqual(history[1].verdict, Verdict.UNQUALIFIED)
        # 放行被撤销，且有放行记录链
        releases = self.svc._releases["B1"]
        self.assertTrue(releases[0].released)
        self.assertFalse(releases[-1].released)
        self.assertEqual(self.svc._batches["B1"].release_state.value, "revoked")

    def test_correction_qualified_keeps_release(self) -> None:
        self.svc.release_batch("B1", DAVE)
        self.svc.correct_conclusion("B1", DAVE, Verdict.QUALIFIED, {"纯度": 99.2})
        self.assertEqual(self.svc._batches["B1"].release_state.value, "released")

    def test_correction_not_allowed_before_release(self) -> None:
        with self.assertRaises(ReleaseError):
            self.svc.correct_conclusion("B1", DAVE, Verdict.QUALIFIED, {"纯度": 99.0})

    def test_inspection_requires_real_sample(self) -> None:
        with self.assertRaises(InspectionError):
            self.svc.record_inspection("GHOST", DAVE, Verdict.QUALIFIED, {"纯度": 99.0})


class RiskAndEligibilityTest(unittest.TestCase):
    def _prepared(self):
        svc = build_service()
        contract, _, _ = grow_release_ready_batch(svc, batch="B1", plot_id="P1")
        # 再准备一条尚未放行的同地块批次 B2
        svc.record_harvest("N-H-B2", contract, 2, "P1", "B2", 500.0, ALICE)
        svc.draw_sample("S-B2", "B2", 1.0, "N-H-B2", DAVE)
        svc.record_inspection("S-B2", DAVE, Verdict.QUALIFIED, {"纯度": 99.0})
        return svc

    def test_climate_risk_holds_only_unreleased(self) -> None:
        svc = self._prepared()
        flag = svc.flag_risk(["P1"], "台风外围影响扬花期", CAROL)
        self.assertIn("B2", flag.held_batches)
        self.assertIn("B1", flag.unaffected_released)
        # 未放行批次因挂起不能放行
        with self.assertRaises(ReleaseError):
            svc.release_batch("B2", DAVE)
        # 风险解除后可放行
        svc.clear_risk(flag.risk_id, CAROL)
        self.assertTrue(svc.release_batch("B2", DAVE).released)

    def test_grower_downgrade_blocks_release_spares_released(self) -> None:
        svc = self._prepared()
        flag = svc.change_grower_grade("G1", 1, CAROL)
        self.assertIn("B2", flag.held_batches)
        self.assertIn("B1", flag.unaffected_released)
        with self.assertRaises(ReleaseError):
            svc.release_batch("B2", DAVE)
        # 恢复资格并解除挂起后放行
        svc.change_grower_grade("G1", 3, CAROL)
        for risk in svc._risks:
            svc.clear_risk(risk.risk_id, CAROL)
        self.assertTrue(svc.release_batch("B2", DAVE).released)

    def test_released_batch_unaffected_then_corrected_only_via_new_version(self) -> None:
        svc = self._prepared()
        svc.flag_risk(["P1"], "花期冲突复核", CAROL)
        # 已放行的 B1 状态不被风险改变
        self.assertEqual(svc._batches["B1"].release_state.value, "released")
        self.assertEqual(svc._batches["B1"].holds, [])


if __name__ == "__main__":
    unittest.main()
