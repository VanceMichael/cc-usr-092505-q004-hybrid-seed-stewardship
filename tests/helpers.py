"""测试用场景工厂：构造两个企业、两个品种的相邻制种场景。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_seed_stewardship.models import (
    ContractSubmission,
    PlotBinding,
    TechnicalProtocol,
    VarietyRights,
    Verdict,
)
from hybrid_seed_stewardship.service import Role, StewardshipService

ENTERPRISE_A = "武夷种业"
ENTERPRISE_B = "稻花种业"
VARIETY_A = "V两优1号"
VARIETY_B = "V两优2号"

ALICE = "alice"   # 甲企业生产提交人
BOB = "bob"       # 乙企业生产提交人
CAROL = "carol"   # 县级管理人员
DAVE = "dave"     # 种子检验人员 / 放行人
FRANK = "frank"   # 联盟秘书处

PERIOD = "2026-Q3"


def build_service() -> StewardshipService:
    svc = StewardshipService()
    svc.register_actor(ALICE, Role.ENTERPRISE, ENTERPRISE_A)
    svc.register_actor(BOB, Role.ENTERPRISE, ENTERPRISE_B)
    svc.register_actor(CAROL, Role.COUNTY)
    svc.register_actor(DAVE, Role.INSPECTOR)
    svc.register_actor(FRANK, Role.SECRETARIAT)
    return svc


def rights(enterprise: str, variety: str) -> VarietyRights:
    return VarietyRights(
        variety=variety,
        rights_holder=enterprise,
        parent_female_batch=f"F-{variety}-2026-01",
        parent_male_batch=f"M-{variety}-2026-07",
        parent_source="联盟亲本繁制中心",
    )


def protocol(variety: str, *, min_grade: int = 2, isolation: int = 100,
             window: tuple[int, int] = (100, 140), target: float = 20000) -> TechnicalProtocol:
    return TechnicalProtocol(
        variety=variety,
        min_grower_grade=min_grade,
        min_isolation_m=isolation,
        sowing_window=window,
        target_quantity_kg=target,
    )


def submit(svc: StewardshipService, enterprise: str, variety: str, submitter: str,
           **proto_kwargs) -> str:
    submission = ContractSubmission(
        enterprise=enterprise,
        rights=rights(enterprise, variety),
        target_quantity_kg=proto_kwargs.pop("target", 20000),
        protocol=protocol(variety, **proto_kwargs),
        submitted_by=submitter,
    )
    return svc.submit_contract(submission).contract_id


def plot(plot_id: str, grower: str = "G1", grade: int = 3, *, isolation: int = 200,
         sowing: int = 110, flowering: tuple[int, int] = (200, 210),
         loc: tuple[float, float] = (0.0, 0.0), county: str = "建宁县",
         checks: tuple[str, ...] = ()) -> PlotBinding:
    return PlotBinding(
        plot_id=plot_id,
        county=county,
        grower_id=grower,
        grower_grade=grade,
        isolation_m=isolation,
        sowing_day=sowing,
        transplanting_day=sowing + 20,
        flowering_start=flowering[0],
        flowering_end=flowering[1],
        location_x_km=loc[0],
        location_y_km=loc[1],
        field_checks=checks,
    )


def grow_release_ready_batch(svc: StewardshipService, *, batch: str = "B1",
                             plot_id: str = "P1", weight: float = 1000.0):
    """收获、抽样、检验合格并放行一条成品批，返回 (合同号, 收获节点, 样品号)。"""
    contract = submit(svc, ENTERPRISE_A, VARIETY_A, ALICE)
    svc.bind_plots(contract, [plot(plot_id)], CAROL)
    node = f"N-H-{batch}"
    svc.record_harvest(node, contract, 2, plot_id, batch, weight, ALICE)
    sample = f"S-{batch}"
    svc.draw_sample(sample, batch, 1.0, node, DAVE)
    svc.record_inspection(sample, DAVE, Verdict.QUALIFIED,
                          {"纯度": 99.0, "发芽率": 90.0})
    svc.release_batch(batch, DAVE)
    return contract, node, sample
