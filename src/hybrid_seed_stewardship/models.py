"""制种受托与质量追溯的领域模型。

模型全部为不可变值对象：企业资料、合同版本、地块绑定、谱系节点
（收获 / 混样 / 拆批 / 合批 / 加工）、检验结论与放行记录、
匿名报价与价格指数。任何修改都通过新增版本或追加台账事件表达。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, Sequence


# ---------------------------------------------------------------- 基础概念


class ReleaseState(str, Enum):
    """质量批次的生命周期状态。"""

    PENDING = "pending"        # 尚未放行，可被新事件影响
    RELEASED = "released"      # 已放行，结论封存，只能以替代版本纠正
    REVOKED = "revoked"        # 被替代版本纠正，历史结论仍保留可追溯


class NodeKind(str, Enum):
    """谱系台账中的事件类型。"""

    HARVEST = "harvest"          # 收获：地块产出进入台账
    BLEND = "blend"             # 混样：多批小量合并为检验样品
    SPLIT = "split"            # 拆批：一批按重量拆成多批
    MERGE = "merge"            # 合批：多批合并为一批
    PROCESS = "process"        # 加工：一批加工为成品批


class Verdict(str, Enum):
    """检验结论。"""

    PENDING = "pending"
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"


# ---------------------------------------------------------------- 企业与品种


@dataclass(frozen=True)
class VarietyRights:
    """企业对品种的权利证明与亲本来源。"""

    variety: str
    rights_holder: str
    parent_female_batch: str   # 母本亲本批次
    parent_male_batch: str     # 父本亲本批次
    parent_source: str         # 亲本来源说明


@dataclass(frozen=True)
class TechnicalProtocol:
    """品种技术规程：对受托人等级、隔离和窗口的硬性要求。"""

    variety: str
    min_grower_grade: int                  # 受托人最低等级（数字越大等级越高）
    min_isolation_m: int                   # 与同花期异品种水稻的最小隔离距离（米）
    sowing_window: tuple[int, int]         # 允许播插窗口，以年内日序表示
    target_quantity_kg: float              # 目标数量（千克）

    def window_covers(self, day_of_year: int) -> bool:
        start, end = self.sowing_window
        if start <= end:
            return start <= day_of_year <= end
        # 跨年窗口（如双季晚稻）
        return day_of_year >= start or day_of_year <= end


# ---------------------------------------------------------------- 合同版本


@dataclass(frozen=True)
class ContractSubmission:
    """企业提交的受托生产资料。"""

    enterprise: str
    rights: VarietyRights
    target_quantity_kg: float
    protocol: TechnicalProtocol
    submitted_by: str

    def validate(self) -> None:
        if not self.enterprise or not self.submitted_by:
            raise ValueError("企业与提交人不可为空")
        if self.rights.variety != self.protocol.variety:
            raise ValueError("品种权利与技术规程的品种不一致")
        if self.target_quantity_kg <= 0 or self.protocol.target_quantity_kg <= 0:
            raise ValueError("目标数量必须为正")
        if self.submitted_by == self.enterprise:
            raise ValueError("提交人须为企业账户下的具体自然人")
        if not self.rights.parent_female_batch or not self.rights.parent_male_batch:
            raise ValueError("父母本批次必须登记")


@dataclass(frozen=True)
class PlotBinding:
    """县级人员绑定到合同版本的地块与生产安排。"""

    plot_id: str
    county: str
    grower_id: str
    grower_grade: int
    isolation_m: int                 # 与最近异品种水稻的实际隔离距离
    sowing_day: int                  # 播种日（年内日序）
    transplanting_day: int          # 插秧日（年内日序）
    flowering_start: int            # 始花期（年内日序）
    flowering_end: int              # 齐花期（年内日序）
    location_x_km: float = 0.0      # 地块平面坐标（千米），用于相邻地块距离计算
    location_y_km: float = 0.0
    field_checks: tuple[str, ...] = field(default_factory=tuple)

    def distance_m_to(self, other: "PlotBinding") -> float:
        return (
            math.hypot(
                self.location_x_km - other.location_x_km,
                self.location_y_km - other.location_y_km,
            )
            * 1000
        )

    def flowering_overlaps(self, other: "PlotBinding") -> bool:
        """两个地块花期是否重叠（用于相邻区域异品种冲突判定）。"""
        return self.flowering_start <= other.flowering_end and other.flowering_start <= self.flowering_end


@dataclass(frozen=True)
class ContractVersion:
    """合同的一个不可变版本。

    企业资料形成版本 1；县乡绑定形成后续版本。变更必须派生新版本，
    已被台账或检验引用的版本永不改写。
    """

    contract_id: str
    version: int
    submission: ContractSubmission
    bindings: tuple[PlotBinding, ...] = field(default_factory=tuple)
    supersedes: int | None = None
    note: str = ""

    @property
    def variety(self) -> str:
        return self.submission.rights.variety

    @property
    def is_bound(self) -> bool:
        return len(self.bindings) > 0

    def with_bindings(self, bindings: Sequence[PlotBinding], note: str = "") -> "ContractVersion":
        return replace(
            self,
            version=self.version + 1,
            bindings=tuple(bindings),
            supersedes=self.version,
            note=note,
        )


# ---------------------------------------------------------------- 谱系台账


@dataclass(frozen=True)
class LineageNode:
    """谱系台账中的一次数量移动事件。

    守恒规则：非收获事件的输入重量之和必须等于输出重量之和
    （允许加工环节登记经登记的损耗）。每个输入批只能被一个
    未作废事件占用。
    """

    node_id: str
    kind: NodeKind
    inputs: tuple[str, ...]            # 上游批次号
    outputs: tuple[str, ...]           # 本事件产生的批次号
    weights_out: Mapping[str, float]   # 每个输出批的重量
    weights_in: Mapping[str, float]    # 每个输入批被占用的重量
    contract_id: str
    contract_version: int
    recorded_by: str
    processing_loss_kg: float = 0.0
    plots: tuple[str, ...] = field(default_factory=tuple)  # 收获事件对应的地块
    note: str = ""

    def total_in(self) -> float:
        return round(sum(self.weights_in.values()), 6)

    def total_out(self) -> float:
        return round(sum(self.weights_out.values()), 6)


@dataclass(frozen=True)
class SampleRecord:
    """抽样记录：从某批次抽取检验样品，样品重量同样占用台账数量。"""

    sample_id: str
    batch_id: str
    weight_kg: float
    drawn_by: str
    node_id: str               # 抽样所依附的谱系事件


# ---------------------------------------------------------------- 检验与放行


@dataclass(frozen=True)
class InspectionConclusion:
    """一次检验（或复核）的结论。

    复核只针对尚未放行的批次；批次放行后只能发布替代结论版本。
    """

    sample_id: str
    batch_id: str
    inspector: str
    verdict: Verdict
    indicators: Mapping[str, float]
    version: int = 1
    supersedes: int | None = None
    note: str = ""

    @property
    def is_review(self) -> bool:
        return self.supersedes is not None


@dataclass(frozen=True)
class ReleaseRecord:
    """质量放行记录。放行人必须独立于生产提交人。"""

    batch_id: str
    released_by: str
    producer: str                  # 生产提交人，记录在案用于独立性核对
    conclusion_version: int
    released: bool
    note: str = ""


# ---------------------------------------------------------------- 报价与指数


@dataclass(frozen=True)
class Quotation:
    """企业提交的匿名报价候选。"""

    quote_id: str
    enterprise: str            # 仅秘书处权限可见，指数计算只用匿名值
    variety: str
    price_per_kg: float
    quantity_kg: float
    period: str

    def validate(self) -> None:
        if self.price_per_kg <= 0 or self.quantity_kg <= 0:
            raise ValueError("报价价格与数量必须为正")


@dataclass(frozen=True)
class ThresholdRule:
    """指数准入门槛。"""

    period: str
    variety: str
    min_quantity_kg: float
    min_quotes: int
    price_floor: float
    price_ceiling: float

    def accepts(self, quote: Quotation) -> tuple[bool, str]:
        if quote.period != self.period or quote.variety != self.variety:
            return False, "报价不属于本期本品种"
        if quote.quantity_kg < self.min_quantity_kg:
            return False, f"数量 {quote.quantity_kg}kg 低于门槛 {self.min_quantity_kg}kg"
        if not (self.price_floor <= quote.price_per_kg <= self.price_ceiling):
            return False, f"价格 {quote.price_per_kg} 超出区间 [{self.price_floor}, {self.price_ceiling}]"
        return True, "通过"


@dataclass(frozen=True)
class IndexPublication:
    """一期价格指数及其可审计的门槛判定明细。"""

    period: str
    variety: str
    index_value: float | None
    included: tuple[str, ...]
    excluded: Mapping[str, str]   # quote_id -> 未进入原因
    threshold: ThresholdRule
    published: bool

    def reason_for(self, quote_id: str) -> str:
        """追问某条报价进入或未进入当期指数的具体门槛。"""
        if quote_id in self.included:
            return f"报价 {quote_id} 通过全部门槛，按数量加权计入指数"
        if quote_id in self.excluded:
            return f"报价 {quote_id} 未计入：{self.excluded[quote_id]}"
        return f"报价 {quote_id} 未参与本期指数编制"


@dataclass(frozen=True)
class PlotOrigin:
    """批次谱系回溯到的一个来源地块及其亲本批次。"""

    contract_id: str
    contract_version: int
    plot_id: str
    county: str
    grower_id: str
    grower_grade: int
    parent_female_batch: str
    parent_male_batch: str
    parent_source: str


@dataclass(frozen=True)
class BagTraceView:
    """扫描成品袋时呈现的追溯视图：地块、亲本、谱系、检验与放行。"""

    bag_id: str
    final_batch: str
    origins: tuple[PlotOrigin, ...]
    node_chain: tuple[LineageNode, ...]
    inspections: tuple[InspectionConclusion, ...]
    releases: tuple[ReleaseRecord, ...]
    release_state: ReleaseState
