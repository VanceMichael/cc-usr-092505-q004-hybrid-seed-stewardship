"""制种受托与质量追溯服务的领域不变量测试。"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from hybrid_seed_stewardship.model import (
    EventType,
    FieldInspection,
    Inspection,
    IsolationZone,
    MaterialEvent,
    Plot,
    Quotation,
    Release,
    ReleaseCorrection,
    Role,
    SeedContract,
    StewardGrade,
    StewardQualification,
    User,
    VarietyFiling,
)
from hybrid_seed_stewardship.service import (
    AuthorizationError,
    ConflictError,
    ConservationError,
    StateError,
    StewardshipService,
)
from hybrid_seed_stewardship.index import IndexOffice, IndexRules

D = Decimal
W = lambda v: D(str(v))


def _bootstrap() -> StewardshipService:
    """搭建两企业、两县、两个品种的最小可用世界。"""
    svc = StewardshipService()
    svc.register_user(User('ent1', '企业一', Role.ENTERPRISE, enterprise_id='e1'))
    svc.register_user(User('ent2', '企业二', Role.ENTERPRISE, enterprise_id='e2'))
    svc.register_user(User('cnt1', '建宁县人员', Role.COUNTY, county_id='c1'))
    svc.register_user(User('cnt2', '泰宁县人员', Role.COUNTY, county_id='c2'))
    svc.register_user(User('insp1', '检验员甲', Role.INSPECTOR))
    svc.register_user(User('insp2', '检验员乙', Role.INSPECTOR))
    svc.register_user(User('off1', '放行人甲', Role.RELEASE_OFFICER))
    svc.register_user(User('sec', '秘书处', Role.SECRETARIAT))
    svc.register_user(User('smp', '抽查员', Role.SAMPLER))

    svc.record_steward_grade(
        'cnt1', 's1', StewardQualification('2026-01-01', None, StewardGrade.A)
    )
    svc.record_steward_grade(
        'cnt2', 's2', StewardQualification('2026-01-01', None, StewardGrade.A)
    )

    svc.register_zone(
        'cnt1',
        IsolationZone('z1', 'c1', 'V1', isolation_distance_m=200,
                      flowering_start='2026-07-01', flowering_end='2026-07-10'),
    )
    svc.register_zone(
        'cnt1',
        IsolationZone('zX', 'c1', 'VX', isolation_distance_m=200,
                      flowering_start='2026-07-20', flowering_end='2026-07-25'),
    )
    svc.register_plot(
        'cnt1',
        Plot('p1', 'c1', 'z1', 's1', D('10'),
             ('2026-04-01', '2026-04-10'), ('2026-05-01', '2026-05-10')),
    )
    svc.register_plot(
        'cnt1',
        Plot('pX', 'c1', 'zX', 's2', D('8'),
             ('2026-04-01', '2026-04-10'), ('2026-05-01', '2026-05-10')),
    )

    svc.submit_filing(
        'ent1',
        VarietyFiling('f1', 'e1', 'V1', 1, 'PVP-V1', 'M1', 'F1', '联盟亲本库',
                      W(10000), 'proto-v1', rights_valid_to='2030-12-31'),
    )
    svc.submit_filing(
        'ent2',
        VarietyFiling('fX', 'e2', 'VX', 1, 'PVP-VX', 'MX', 'FX', '自留亲本',
                      W(8000), 'proto-vx', rights_valid_to='2030-12-31'),
    )

    svc.bind_contract(
        'cnt1',
        SeedContract('C1', 'e1', 'f1', 1, 1, 's1', StewardGrade.A,
                     frozenset({'p1'}), frozenset({'z1'}),
                     ('2026-04-02', '2026-04-08'), ('2026-05-02', '2026-05-08')),
    )
    svc.bind_contract(
        'cnt1',
        SeedContract('CX', 'e2', 'fX', 1, 1, 's2', StewardGrade.A,
                     frozenset({'pX'}), frozenset({'zX'}),
                     ('2026-04-02', '2026-04-08'), ('2026-05-02', '2026-05-08')),
    )
    svc.bind_field_inspection(
        'cnt1',
        FieldInspection('fi1', 'C1', 1, '2026-06-20', 'insp1', True, '花检正常'),
    )
    svc.bind_field_inspection(
        'cnt1',
        FieldInspection('fiX', 'CX', 1, '2026-06-20', 'insp1', True, '花检正常'),
    )
    return svc


def _build_chain(svc: StewardshipService) -> None:
    """收获 L1 → 抽样 → 加工 L2 → 拆批 L3/L4 → 包装 L4。"""
    svc.harvest('ent1', 'h1', 'L1', ['p1'], W(1000), '2026-08-01')
    svc.harvest('ent2', 'hX', 'LX', ['pX'], W(500), '2026-08-01')

    svc.apply_material_event(
        'insp1',
        MaterialEvent('e_sample', EventType.SAMPLE, '2026-08-02', 'insp1',
                      inputs=frozenset({('L1', W(5))}), outputs=frozenset(), loss=W(5)),
    )
    svc.apply_material_event(
        'ent1',
        MaterialEvent('e_proc', EventType.PROCESS, '2026-08-03', 'ent1',
                      inputs=frozenset({('L1', W(995))}),
                      outputs=frozenset({('L2', W(980))}), loss=W(15)),
    )
    svc.apply_material_event(
        'ent1',
        MaterialEvent('e_split', EventType.SPLIT, '2026-08-04', 'ent1',
                      inputs=frozenset({('L2', W(980))}),
                      outputs=frozenset({('L3', W(500)), ('L4', W(480))})),
    )
    svc.pack('ent1', 'e_pack', 'L4', {'BAG-1': W(200), 'BAG-2': W(280)}, '2026-08-05')


class FilingContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _bootstrap()

    def test_filing_and_contract_versions_append_only(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.svc.submit_filing(
                'ent2',
                VarietyFiling('f9', 'e1', 'V9', 1, 'x', 'm', 'f', 's', W(1), 'p',
                              rights_valid_to='2030-01-01'),
            )
        amended = VarietyFiling(
            'f1', 'e1', 'V1', 2, 'PVP-V1', 'M1', 'F1b', '联盟亲本库',
            W(12000), 'proto-v1.1', rights_valid_to='2030-12-31', supersedes=1,
        )
        self.svc.amend_filing('ent1', 'f1', amended)
        self.assertEqual(self.svc.latest_filing_version('f1'), 2)
        # 历史版本原样保留，合同可继续引用旧版本。
        self.assertEqual(self.svc.filing_version('f1', 1).parent_female_batch, 'F1')
        with self.assertRaises(StateError):
            self.svc.amend_filing('ent1', 'f1', amended)  # 版本号不连续
        with self.assertRaises(AuthorizationError):
            self.svc.amend_filing('ent2', 'f1', amended)

    def test_contract_windows_and_grade_must_match(self) -> None:
        bad = SeedContract('C2', 'e1', 'f1', 1, 1, 's1', StewardGrade.B,
                           frozenset({'p1'}), frozenset({'z1'}),
                           ('2026-04-02', '2026-04-08'), ('2026-05-02', '2026-05-08'))
        with self.assertRaises(StateError):
            self.svc.bind_contract('cnt1', bad)  # 等级与登记不一致
        outside = SeedContract('C2', 'e1', 'f1', 1, 1, 's1', StewardGrade.A,
                               frozenset({'p1'}), frozenset({'z1'}),
                               ('2026-03-01', '2026-03-02'), ('2026-05-02', '2026-05-08'))
        with self.assertRaises(ConflictError):
            self.svc.bind_contract('cnt1', outside)
        with self.assertRaises(AuthorizationError):
            self.svc.bind_contract('cnt2',  # 外县人员不能绑定本县地块
                                   SeedContract('C3', 'e1', 'f1', 1, 1, 's1',
                                                StewardGrade.A, frozenset({'p1'}),
                                                frozenset({'z1'}),
                                                ('2026-04-02', '2026-04-08'),
                                                ('2026-05-02', '2026-05-08')))

    def test_harvest_requires_passed_field_inspection(self) -> None:
        svc = StewardshipService()
        svc.register_user(User('e', '企', Role.ENTERPRISE, enterprise_id='e1'))
        svc.register_user(User('c', '县', Role.COUNTY, county_id='c1'))
        svc.record_steward_grade('c', 's', StewardQualification('2026-01-01', None, StewardGrade.A))
        svc.register_zone('c', IsolationZone('z', 'c1', 'V1', 200, '2026-07-01', '2026-07-10'))
        svc.register_plot('c', Plot('p', 'c1', 'z', 's', D('5'),
                                    ('2026-04-01', '2026-04-10'), ('2026-05-01', '2026-05-10')))
        svc.submit_filing('e', VarietyFiling('f', 'e1', 'V1', 1, 'r', 'm', 'f', 's',
                                             W(100), 'p', rights_valid_to='2030-01-01'))
        svc.bind_contract('c', SeedContract('C', 'e1', 'f', 1, 1, 's', StewardGrade.A,
                                            frozenset({'p'}), frozenset({'z'}),
                                            ('2026-04-02', '2026-04-08'),
                                            ('2026-05-02', '2026-05-08')))
        with self.assertRaises(StateError):
            svc.harvest('e', 'h', 'L', ['p'], W(10), '2026-08-01')
        svc.bind_field_inspection('c', FieldInspection('fi', 'C', 1, '2026-06-20', 'insp', False))
        with self.assertRaises(StateError):
            svc.harvest('e', 'h', 'L', ['p'], W(10), '2026-08-01')


class MaterialChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _bootstrap()
        _build_chain(self.svc)

    def test_weight_conservation_and_lineage(self) -> None:
        self.assertEqual(self.svc.available('L3'), W(500))
        self.assertEqual(self.svc.available('L1'), W(0))
        self.assertEqual(self.svc.available('L2'), W(0))
        self.assertEqual(self.svc.available('L4'), W(0))  # 已全部包装
        self.svc.total_weight_check()
        lineage = self.svc.trace_lot('L4')
        self.assertEqual(lineage['parent_male_batch'], 'M1')
        self.assertEqual(lineage['parent_female_batch'], 'F1')
        self.assertEqual(lineage['plot_ids'], ['p1'])
        types = [e['type'] for e in lineage['events']]
        self.assertEqual(types, ['harvest', 'sample', 'process', 'split', 'pack'])

    def test_package_scan_shows_full_provenance(self) -> None:
        trace = self.svc.trace_package('BAG-2')
        self.assertEqual(trace['package_weight'], '280')
        self.assertEqual(trace['variety_code'], 'V1')
        self.assertEqual(trace['plot_ids'], ['p1'])
        self.assertEqual(trace['parent_male_batch'], 'M1')
        self.assertIn('harvest', [e['type'] for e in trace['events']])

    def test_non_conserving_event_rejected_without_side_effect(self) -> None:
        before = self.svc.available('L3')
        with self.assertRaises(ConservationError):
            self.svc.apply_material_event(
                'ent1',
                MaterialEvent('bad', EventType.SPLIT, '2026-08-10', 'ent1',
                              inputs=frozenset({('L3', W(500))}),
                              outputs=frozenset({('L5', W(499))})),  # 少 1kg 无损耗
            )
        self.assertNotIn('L5', self.svc.lots)
        self.assertEqual(self.svc.available('L3'), before)

    def test_different_parent_batches_cannot_merge(self) -> None:
        with self.assertRaises(ConflictError):
            self.svc.apply_material_event(
                'ent1',
                MaterialEvent('bad_merge', EventType.MERGE, '2026-08-10', 'ent1',
                              inputs=frozenset({('L3', W(10)), ('LX', W(10))}),
                              outputs=frozenset({('LM', W(20))})),
            )

    def test_reservations_prevent_double_occupation(self) -> None:
        svc = _bootstrap()
        svc.harvest('ent1', 'h1', 'L1', ['p1'], W(1000), '2026-08-01')
        svc.reserve_quantity('insp1', 'r1', {'L1': W(400)})
        self.assertEqual(svc.available('L1'), W(600))
        # 第二笔并行预约叠加后超出库存，拒绝。
        with self.assertRaises(ConservationError):
            svc.reserve_quantity('insp2', 'r2', {'L1': W(700)})
        # 未使用该预约的抽样不能动用已占数量。
        with self.assertRaises(ConservationError):
            svc.apply_material_event(
                'insp2',
                MaterialEvent('s2', EventType.SAMPLE, '2026-08-02', 'insp2',
                              inputs=frozenset({('L1', W(650))}),
                              outputs=frozenset(), loss=W(650)),
            )
        # 数量与预约不符同样拒绝。
        with self.assertRaises(ConservationError):
            svc.apply_material_event(
                'insp1',
                MaterialEvent('s1', EventType.SAMPLE, '2026-08-02', 'insp1',
                              inputs=frozenset({('L1', W(399))}),
                              outputs=frozenset(), loss=W(399), reserve_id='r1'),
            )
        # 与预约一致的事件成功，占用随之释放。
        svc.apply_material_event(
            'insp1',
            MaterialEvent('s1', EventType.SAMPLE, '2026-08-02', 'insp1',
                          inputs=frozenset({('L1', W(400))}),
                          outputs=frozenset(), loss=W(400), reserve_id='r1'),
        )
        self.assertEqual(svc.available('L1'), W(600))
        svc.total_weight_check()

    def test_cancel_reservation_restores_quantity(self) -> None:
        svc = _bootstrap()
        svc.harvest('ent1', 'h1', 'L1', ['p1'], W(100), '2026-08-01')
        svc.reserve_quantity('smp', 'r9', {'L1': W(100)})
        self.assertEqual(svc.available('L1'), W(0))
        svc.cancel_reservation('r9')
        self.assertEqual(svc.available('L1'), W(100))


class SuspensionReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _bootstrap()
        _build_chain(self.svc)
        self.svc.record_inspection(
            'insp1',
            Inspection('ins4', 'L4', '2026-08-06', 'insp1',
                       {'germination': '合格', 'purity': '合格'}, True),
        )
        self.svc.release_lot(
            'off1',
            Release('rel4', 'L4', '2026-08-07', 'off1', 'ins4', True, '准予放行'),
        )

    def test_climate_and_qualification_only_affect_unreleased(self) -> None:
        affected = self.svc.flag_climate_risk('2026-08-20', ['L3', 'L4'], '低温阴雨')
        self.assertEqual(affected, ['L3'])  # L4 已放行，不受影响
        self.assertIsNone(self.svc.suspension_reason('L4'))
        self.assertIn('气候风险', self.svc.suspension_reason('L3'))

        self.svc.record_steward_grade(
            'cnt1', 's1', StewardQualification('2026-08-21', None, StewardGrade.REVOKED)
        )
        self.assertTrue(self.svc.release_status('L4')['released'])
        self.assertIsNotNone(self.svc.suspension_reason('L3'))
        # 资格撤销后不能再收获新批次。
        with self.assertRaises(AuthorizationError):
            self.svc.harvest('ent1', 'h2', 'L9', ['p1'], W(10), '2026-08-22')

    def test_inspection_review_rules(self) -> None:
        # L3 尚无检验；复核不通过暂停未放行批次。
        self.svc.record_inspection(
            'insp1',
            Inspection('ins3', 'L3', '2026-08-06', 'insp1', {'moisture': '合格'}, True),
        )
        self.svc.review_inspection(
            'insp2',
            Inspection('ins3r', 'L3', '2026-08-08', 'insp2',
                       {'moisture': '不合格'}, False, review_of='ins3'),
        )
        self.assertIn('检验复核不通过', self.svc.suspension_reason('L3'))
        # 再次复核通过，清除该项暂停。
        self.svc.review_inspection(
            'insp2',
            Inspection('ins3r2', 'L3', '2026-08-09', 'insp2',
                       {'moisture': '合格'}, True, review_of='ins3r'),
        )
        self.assertIsNone(self.svc.suspension_reason('L3'))
        self.assertEqual(self.svc.latest_inspection('L3').inspection_id, 'ins3r2')

    def test_release_requires_independent_officer_and_latest_inspection(self) -> None:
        # 生产提交人（企业）不是放行人角色。
        with self.assertRaises(AuthorizationError):
            self.svc.release_lot(
                'ent1', Release('x', 'L3', '2026-08-07', 'ent1', 'ins3', True)
            )
        # 县级人员同样不能放行。
        with self.assertRaises(AuthorizationError):
            self.svc.release_lot(
                'cnt1', Release('x', 'L3', '2026-08-07', 'cnt1', 'ins3', True)
            )
        # 放行人若实际参与了该批次生产提交，仍然拒绝（同人双账户防线）。
        self.svc.record_inspection(
            'insp1',
            Inspection('ins3', 'L3', '2026-08-06', 'insp1', {'purity': '合格'}, True),
        )
        self.svc._production_actors['L3'].add('off1')
        with self.assertRaises(AuthorizationError):
            self.svc.release_lot(
                'off1', Release('rel3', 'L3', '2026-08-07', 'off1', 'ins3', True)
            )

    def test_suspended_lot_cannot_be_released(self) -> None:
        self.svc.record_inspection(
            'insp1',
            Inspection('ins3', 'L3', '2026-08-06', 'insp1', {'purity': '合格'}, True),
        )
        self.svc.flag_climate_risk('2026-08-20', ['L3'], '倒伏')
        with self.assertRaises(StateError):
            self.svc.release_lot(
                'off1', Release('rel3', 'L3', '2026-08-21', 'off1', 'ins3', True)
            )

    def test_released_conclusion_only_corrected_by_new_version(self) -> None:
        # 已放行批次不能重复放行，也不能再被物料移动。
        with self.assertRaises(StateError):
            self.svc.release_lot(
                'off1', Release('rel4b', 'L4', '2026-08-08', 'off1', 'ins4', True)
            )
        with self.assertRaises(StateError):
            self.svc.apply_material_event(
                'ent1',
                MaterialEvent('m', EventType.PROCESS, '2026-08-09', 'ent1',
                              inputs=frozenset({('L4', W(10))}),
                              outputs=frozenset({('Lz', W(10))})),
            )
        # 复核产生新检验，再以替代版本纠正已发布结论。
        self.svc.review_inspection(
            'insp2',
            Inspection('ins4r', 'L4', '2026-08-10', 'insp2',
                       {'germination': '不合格'}, False, review_of='ins4'),
        )
        self.svc.correct_release(
            'off1',
            ReleaseCorrection('cor1', 'rel4', 'L4', '2026-08-11', 'off1',
                              'ins4r', False, '发芽率复测不合格，撤销放行'),
        )
        status = self.svc.release_status('L4')
        self.assertTrue(status['released'])  # 原结论仍在
        self.assertEqual(status['original'].release_id, 'rel4')
        self.assertEqual(len(status['corrections']), 1)
        self.assertFalse(status['corrections'][0].released)
        with self.assertRaises(StateError):  # 同一结论不能重复纠正
            self.svc.correct_release(
                'off1',
                ReleaseCorrection('cor2', 'rel4', 'L4', '2026-08-12', 'off1',
                                  'ins4r', False, '重复纠正'),
            )

    def test_release_must_use_latest_inspection(self) -> None:
        self.svc.record_inspection(
            'insp1',
            Inspection('ins3', 'L3', '2026-08-06', 'insp1', {'purity': '合格'}, True),
        )
        self.svc.review_inspection(
            'insp2',
            Inspection('ins3r', 'L3', '2026-08-08', 'insp2',
                       {'purity': '合格'}, True, review_of='ins3'),
        )
        with self.assertRaises(StateError):
            self.svc.release_lot(
                'off1', Release('rel3', 'L3', '2026-08-09', 'off1', 'ins3', True)
            )
        self.svc.release_lot(
            'off1', Release('rel3', 'L3', '2026-08-09', 'off1', 'ins3r', True)
        )
        self.assertTrue(self.svc.release_status('L3')['released'])


class FloweringConflictTest(unittest.TestCase):
    def test_cross_county_flowering_conflict_suspends_and_blocks_binding(self) -> None:
        svc = _bootstrap()
        # 冲突尚未登记时可以正常收获。
        svc.harvest('ent1', 'h1', 'L1', ['p1'], W(1000), '2026-08-01')

        # 邻县新增不同品种的隔离区，秘书处登记跨县相邻关系：
        # 实际距离 150m 小于要求的 300m，且花期重叠。
        svc.register_zone(
            'cnt2',
            IsolationZone('z2', 'c2', 'V2', isolation_distance_m=300,
                          flowering_start='2026-07-05', flowering_end='2026-07-12'),
        )
        with self.assertRaises(AuthorizationError):
            svc.set_zone_adjacency('cnt1', 'z1', 'z2', 150)
        svc.set_zone_adjacency('sec', 'z1', 'z2', 150)
        conflicts = svc.flowering_conflicts()
        self.assertIn(('z1', 'z2'), conflicts)

        # 已存在的未放行批次被暂停。
        affected = svc.flag_flowering_conflicts('2026-07-06')
        self.assertEqual(affected, ['L1'])

        # 冲突未解决不能追加合同版本，也不能收获新批次。
        with self.assertRaises(ConflictError):
            svc.bind_contract(
                'cnt1',
                SeedContract('C1', 'e1', 'f1', 1, 2, 's1', StewardGrade.A,
                             frozenset({'p1'}), frozenset({'z1'}),
                             ('2026-04-02', '2026-04-08'), ('2026-05-02', '2026-05-08'),
                             supersedes=1),
            )
        with self.assertRaises(ConflictError):
            svc.harvest('ent1', 'h2', 'L2', ['p1'], W(10), '2026-08-02')

        # 拉开距离后冲突消除，暂停解除后批次可以继续流转。
        svc.set_zone_adjacency('sec', 'z1', 'z2', 500)
        self.assertEqual(svc.flowering_conflicts(), [])
        svc.resolve_suspension('L1')
        svc.apply_material_event(
            'ent1',
            MaterialEvent('e_sample', EventType.SAMPLE, '2026-08-03', 'insp1',
                          inputs=frozenset({('L1', W(5))}), outputs=frozenset(), loss=W(5)),
        )

    def test_released_lot_untouched_by_new_conflict(self) -> None:
        svc = _bootstrap()
        svc.harvest('ent1', 'h1', 'L1', ['p1'], W(100), '2026-08-01')
        svc.record_inspection(
            'insp1',
            Inspection('i1', 'L1', '2026-08-02', 'insp1', {'purity': '合格'}, True),
        )
        svc.release_lot('off1', Release('r1', 'L1', '2026-08-03', 'off1', 'i1', True))
        svc.register_zone(
            'cnt2',
            IsolationZone('z2', 'c2', 'V2', 300, '2026-07-05', '2026-07-12'),
        )
        svc.set_zone_adjacency('sec', 'z1', 'z2', 100)
        self.assertEqual(svc.flag_flowering_conflicts('2026-07-06'), [])
        self.assertTrue(svc.release_status('L1')['released'])
        self.assertIsNone(svc.suspension_reason('L1'))


class VisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _bootstrap()
        _build_chain(self.svc)

    def test_enterprise_sees_only_own_lots(self) -> None:
        own = self.svc.enterprise_lots('ent1')
        self.assertEqual(set(own), {'L1', 'L2', 'L3', 'L4'})
        self.assertEqual(self.svc.enterprise_lots('ent2'), ['LX'])
        self.svc.assert_can_read_lot('ent1', 'L3')
        with self.assertRaises(AuthorizationError):
            self.svc.assert_can_read_lot('ent1', 'LX')
        # 抽查员不受企业边界限制。
        self.svc.assert_can_read_lot('smp', 'LX')


class PriceIndexTest(unittest.TestCase):
    PERIOD = '2026-Q3'

    def setUp(self) -> None:
        self.svc = _bootstrap()
        # 第二、三家企业的备案。
        self.svc.submit_filing(
            'ent2',
            VarietyFiling('f2', 'e2', 'V1', 1, 'PVP-V1-E2', 'M1', 'F1', '亲本库',
                          W(9000), 'proto-v1', rights_valid_to='2030-12-31'),
        )
        self.svc.register_user(User('ent3', '企业三', Role.ENTERPRISE, enterprise_id='e3'))
        self.svc.submit_filing(
            'ent3',
            VarietyFiling('f3', 'e3', 'V1', 1, 'PVP-V1-E3', 'M1', 'F1', '亲本库',
                          W(9000), 'proto-v1', rights_valid_to='2030-12-31'),
        )
        self.svc.register_user(User('ent4', '企业四', Role.ENTERPRISE, enterprise_id='e4'))
        self.svc.submit_filing(
            'ent4',
            VarietyFiling('f5', 'e4', 'V1', 1, 'PVP-V1-E4', 'M1', 'F1', '亲本库',
                          W(9000), 'proto-v1', rights_valid_to='2030-12-31'),
        )
        self.svc.register_user(User('ent5', '企业五', Role.ENTERPRISE, enterprise_id='e5'))
        self.svc.submit_filing(
            'ent5',
            VarietyFiling('f6', 'e5', 'V1', 1, 'PVP-V1-E5', 'M1', 'F1', '亲本库',
                          W(9000), 'proto-v1', rights_valid_to='2030-12-31'),
        )
        # 企业六：品种权已到期，用于权利门槛落选。
        self.svc.register_user(User('ent6', '企业六', Role.ENTERPRISE, enterprise_id='e6'))
        self.svc.submit_filing(
            'ent6',
            VarietyFiling('f8', 'e6', 'V1', 1, 'PVP-OLD-E6', 'M1', 'F1', '亲本库',
                          W(9000), 'proto-v1', rights_valid_to='2025-12-31'),
        )
        self.rules = IndexRules(
            variety_code='V1', period_end='2026-09-30',
            min_quantity_kg=W(500), price_floor=W(10), price_ceiling=W(30),
            min_enterprises=3,
        )
        self.office = IndexOffice({(self.PERIOD, 'V1'): self.rules})

    def _quote(self, qid: str, ent: str, price: str, qty: str,
               submitted='2026-09-01T00:00:00') -> Quotation:
        ent_filing = {
            'ent1': ('e1', 'f1'), 'ent2': ('e2', 'f2'), 'ent3': ('e3', 'f3'),
            'ent4': ('e4', 'f5'), 'ent5': ('e5', 'f6'), 'ent6': ('e6', 'f8'),
        }[ent]
        return Quotation(qid, self.PERIOD, ent_filing[0], 'V1', ent_filing[1], 1,
                         W(price), W(qty), submitted)

    def test_published_index_is_anonymized_and_gate_decisions_explainable(self) -> None:
        for q in [
            self._quote('q1', 'ent1', '18.0', '1000'),
            self._quote('q2', 'ent2', '20.0', '2000'),
            self._quote('q3', 'ent3', '22.0', '3000'),
        ]:
            self.office.submit_quote('enterprise', q)
        index = self.office.publish('secretariat', self.PERIOD, 'V1',
                                    '2026-10-05', self.svc.filings)
        self.assertEqual(index.sample_size, 3)
        self.assertEqual(index.anonymized_prices, (W('18.0'), W('20.0'), W('22.0')))
        for qid in ('q1', 'q2', 'q3'):
            self.assertTrue(self.office.decision_of(self.PERIOD, 'V1', qid).accepted)
        # 公开视图只有匿名价格，不含任何报价决策明细。
        public = self.office.published_view(self.PERIOD, 'V1')
        self.assertEqual(public.quote_decisions, {})
        self.assertEqual(public.sample_size, 3)
        # 非秘书处不能发布。
        with self.assertRaises(AuthorizationError):
            self.office.publish('enterprise', self.PERIOD, 'V1', '2026-10-05',
                                self.svc.filings)

    def test_quote_gate_failures_are_reported_per_gate(self) -> None:
        for q in [
            # 通过：企业一、二、三，达到最少家数，指数可发布。
            self._quote('q1', 'ent1', '18.0', '1000',
                        submitted='2026-09-01T00:00:00'),
            self._quote('q2', 'ent2', '20.0', '2000',
                        submitted='2026-09-01T00:00:00'),
            self._quote('q3q', 'ent3', '22.0', '3000',
                        submitted='2026-09-01T00:00:00'),
            # 以下三家各落选一道门槛：
            self._quote('qlow', 'ent4', '18.0', '100',
                        submitted='2026-09-02T00:00:00'),  # 数量不足
            self._quote('qprice', 'ent5', '99.0', '1000',
                        submitted='2026-09-02T00:00:00'),  # 价格超区间
            self._quote('qrights', 'ent6', '18.0', '1000',
                        submitted='2026-09-03T00:00:00'),  # 品种权到期
        ]:
            self.office.submit_quote('enterprise', q)
        published = self.office.publish('secretariat', self.PERIOD, 'V1',
                                        '2026-10-05', self.svc.filings)
        self.assertEqual(published.sample_size, 3)

        # 逐条追问每条落选报价卡在哪道门槛。
        gates = {
            qid: [f.gate for f in self.office
                  .decision_of(self.PERIOD, 'V1', qid).failures]
            for qid in ('qlow', 'qprice', 'qrights')
        }
        self.assertEqual(gates['qlow'], ['min_quantity'])
        self.assertEqual(gates['qprice'], ['price_band'])
        self.assertEqual(gates['qrights'], ['variety_rights'])
        self.assertTrue(all(
            f.reason for f in
            self.office.decision_of(self.PERIOD, 'V1', 'qrights').failures
        ))
        # 落选价格不进入匿名指数。
        self.assertEqual(published.anonymized_prices,
                         (W('18.0'), W('20.0'), W('22.0')))

    def test_duplicate_quote_older_one_is_rejected(self) -> None:
        self.office.submit_quote('enterprise', self._quote('qold', 'ent1', '18.0', '1000',
                                                           submitted='2026-09-01T00:00:00'))
        self.office.submit_quote('enterprise', self._quote('qnew', 'ent1', '19.0', '1200',
                                                           submitted='2026-09-05T00:00:00'))
        self.office.submit_quote('enterprise', self._quote('q2', 'ent2', '20.0', '2000'))
        self.office.submit_quote('enterprise', self._quote('q3', 'ent3', '22.0', '3000'))
        self.office.publish('secretariat', self.PERIOD, 'V1', '2026-10-05',
                            self.svc.filings)
        old = self.office.decision_of(self.PERIOD, 'V1', 'qold')
        self.assertFalse(old.accepted)
        self.assertEqual([f.gate for f in old.failures], ['duplicate'])
        self.assertTrue(self.office.decision_of(self.PERIOD, 'V1', 'qnew').accepted)
        # 旧报价不进入指数价格集合。
        index = self.office.published[self.PERIOD, 'V1']
        self.assertNotIn(W('18.0'), index.anonymized_prices)
        self.assertIn(W('19.0'), index.anonymized_prices)

    def test_too_few_enterprises_blocks_publication(self) -> None:
        self.office.submit_quote('enterprise', self._quote('q1', 'ent1', '18.0', '1000'))
        self.office.submit_quote('enterprise', self._quote('q2', 'ent2', '20.0', '2000'))
        with self.assertRaises(Exception):
            self.office.publish('secretariat', self.PERIOD, 'V1', '2026-10-05',
                                self.svc.filings)


if __name__ == '__main__':
    unittest.main()
