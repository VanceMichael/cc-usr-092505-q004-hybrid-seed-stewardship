"""杂交稻制种受托与质量追溯的领域结构。

所有结构按"追加事件 + 版本纠正"的方式组织：已经发布的结论不就地修改，
而是通过更高版本或替代记录纠正；业务事件只读取不可变快照，重量一律以
``decimal.Decimal`` 千克记录，避免浮点误差破坏守恒校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import FrozenSet, Mapping, Sequence

#: 重量统一使用千克，保留三位小数（克级精度）。
Weight = Decimal


class Role(str, Enum):
    """参与方角色。检验与生产分属不同角色，放行与提交相互独立。"""

    ENTERPRISE = "enterprise"          # 种业企业：提交备案与报价
    COUNTY = "county"                  # 县级管理人员：绑定地块、隔离区与田间检查
    STEWARD = "steward"                # 制种受托人：承担田间繁育
    INSPECTOR = "inspector"            # 种子检验人员：检验复核
    RELEASE_OFFICER = "release_officer"  # 质量放行人：与生产提交人相互独立
    SECRETARIAT = "secretariat"        # 联盟秘书处：运营服务、编制指数
    SAMPLER = "sampler"                # 抽查人员：扫码追溯，不参与生产放行


#: 生产提交角色与放行角色必须互不相同。
PRODUCTION_ROLES = frozenset({Role.ENTERPRISE, Role.COUNTY, Role.STEWARD})


class StewardGrade(str, Enum):
    """受托人等级，等级变化通过有效期版本记录。"""

    A = "A"
    B = "B"
    C = "C"
    REVOKED = "REVOKED"  # 资格被暂停/撤销，不能再绑定新合同批次


class EventType(str, Enum):
    """保持重量守恒的物料事件类型。"""

    HARVEST = "harvest"      # 收获：地块产出登记为田头批次
    MIX = "mix"              # 混样：多批混合（保留取样量）
    PROCESS = "process"      # 加工：清理/烘干/精选，损耗必须显式登记
    SAMPLE = "sample"        # 抽样：从批次取走检验用样
    SPLIT = "split"          # 拆批：一批拆成多批
    MERGE = "merge"          # 合批：多批合成一批
    PACK = "pack"            # 包装：批次封装为可扫码成品袋
    LOSS = "loss"            # 显式损耗登记（加工损耗等）


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: Role
    enterprise_id: str | None = None  # 企业角色归属，用于明细可见性隔离
    county_id: str | None = None


@dataclass(frozen=True)
class StewardQualification:
    """受托人资格在某个时间区间内的等级快照。"""

    valid_from: str
    valid_to: str | None  # None 表示持续有效
    grade: StewardGrade


@dataclass(frozen=True)
class VarietyFiling:
    """企业提交的品种备案（品种权利、亲本来源、目标数量、技术规程）。

    备案内容的任何修改都产生新版本；合同与批次始终引用具体版本号。
    """

    filing_id: str
    enterprise_id: str
    variety_code: str
    version: int
    variety_right_no: str          # 品种权编号（权利有效性是报价门槛之一）
    parent_male_batch: str         # 父本亲本批次
    parent_female_batch: str       # 母本亲本批次
    parent_source: str             # 亲本来源说明
    target_quantity: Weight        # 目标制种数量（千克）
    protocol: str                  # 技术规程标识
    rights_valid_to: str           # 品种权到期日
    supersedes: int | None = None  # 被替代的备案版本


@dataclass(frozen=True)
class IsolationZone:
    """隔离区：同一品种（或异品种花粉源）在隔离距离/花期上不得冲突。"""

    zone_id: str
    county_id: str
    variety_code: str
    isolation_distance_m: int
    flowering_start: str
    flowering_end: str


@dataclass(frozen=True)
class Plot:
    """制种地块，绑定到隔离区与受托人。"""

    plot_id: str
    county_id: str
    zone_id: str
    steward_id: str
    area_mu: Decimal
    sow_window: tuple[str, str]   # 播种窗口（起、止）
    transplant_window: tuple[str, str]  # 插秧窗口


@dataclass(frozen=True)
class FieldInspection:
    """田间检查记录，由县级人员绑定到合同版本。"""

    inspection_id: str
    contract_id: str
    contract_version: int
    at: str
    inspector_user_id: str
    passed: bool
    remark: str = ""


@dataclass(frozen=True)
class SeedContract:
    """制种受托合同的某个版本。

    地块、受托人等级、隔离区、播插窗口与田间检查均绑定到具体版本，
    后续变更产生新版本而不改动历史版本。
    """

    contract_id: str
    enterprise_id: str
    variety_filing_id: str
    filing_version: int
    version: int
    steward_id: str
    steward_grade: StewardGrade
    plot_ids: FrozenSet[str]
    zone_ids: FrozenSet[str]
    sow_window: tuple[str, str]
    transplant_window: tuple[str, str]
    inspections: FrozenSet[str] = field(default_factory=frozenset)
    supersedes: int | None = None


@dataclass(frozen=True)
class MaterialEvent:
    """一次重量守恒的物料移动。

    ``inputs`` 为被消耗的 (批次, 重量)，``outputs`` 为新产生的 (批次, 重量)，
    ``loss`` 为显式登记损耗，``reserve_id`` 标识并行加工/抽样的数量预约。
    守恒要求：消耗重量之和 == 产出重量之和 + 损耗。
    """

    event_id: str
    type: EventType
    at: str
    actor_user_id: str
    inputs: FrozenSet[tuple[str, Weight]]
    outputs: FrozenSet[tuple[str, Weight]]
    loss: Weight = Decimal("0")
    reserve_id: str | None = None
    derived_lots: FrozenSet[str] = field(default_factory=frozenset)
    note: str = ""


@dataclass(frozen=True)
class Inspection:
    """实验室/田间检验结论。复核只产生新的检验记录。"""

    inspection_id: str
    lot_id: str
    at: str
    inspector_user_id: str
    items: Mapping[str, str]            # 检验项 -> 结论值
    passed: bool
    review_of: str | None = None        # 若是复核，指向原检验编号
    replaced_by: str | None = None      # 放行后被替代版本指向的新检验


@dataclass(frozen=True)
class Release:
    """质量放行结论。已放行结论不可撤回，只能由替代版本纠正。"""

    release_id: str
    lot_id: str
    at: str
    officer_user_id: str
    inspection_id: str
    released: bool
    remark: str = ""
    corrected_by: str | None = None     # 后续替代放行编号


@dataclass(frozen=True)
class ReleaseCorrection:
    """对已发布放行结论的替代版本。"""

    correction_id: str
    original_release_id: str
    lot_id: str
    at: str
    officer_user_id: str
    new_inspection_id: str
    released: bool
    reason: str


@dataclass(frozen=True)
class Lot:
    """批次的不可变登记信息；在库重量由服务依据事件维护。"""

    lot_id: str
    contract_id: str
    contract_version: int
    plot_ids: FrozenSet[str]
    steward_id: str
    variety_code: str
    parent_male_batch: str
    parent_female_batch: str
    created_event: str
    released: bool = False


@dataclass(frozen=True)
class Package:
    """一袋可扫码成品：重量与来源批次固定。"""

    package_code: str
    lot_id: str
    weight: Weight
    event_id: str


@dataclass(frozen=True)
class Quotation:
    """企业提交的制种报价（价格指数的原始输入）。"""

    quote_id: str
    period: str
    enterprise_id: str
    variety_code: str
    filing_id: str
    filing_version: int
    price_per_kg: Decimal
    quantity: Weight
    submitted_at: str


@dataclass(frozen=True)
class GateFailure:
    """某条报价在某个门槛上的落选原因，供企业逐条追问。"""

    gate: str
    reason: str


@dataclass(frozen=True)
class QuoteDecision:
    quote_id: str
    accepted: bool
    failures: Sequence[GateFailure]


@dataclass(frozen=True)
class PublishedIndex:
    """某期匿名价格指数：只含通过门槛的报价，发布后不携带企业身份。"""

    period: str
    variety_code: str
    published_at: str
    anonymized_prices: tuple[Decimal, ...]  # 升序价格序列，保留重复报价
    sample_size: int
    mean: Decimal
    quote_decisions: Mapping[str, QuoteDecision]
