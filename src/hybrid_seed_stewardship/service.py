"""制种受托与质量追溯应用服务。

服务在内存中维护五类台账并强制全部业务不变量：

1. 合同版本链：企业提交 v1，县级绑定派生新版本，旧版本永不改写；
2. 资格、隔离与花期：绑定时校验等级、距离与播插窗口，跨合同相邻
   地块异品种花期重叠即冲突；
3. 谱系台账：收获 / 混样 / 拆批 / 合批 / 加工全程重量守恒，
   批次余量被并发事件原子占用，抽样同样扣减；
4. 检验与放行：复核只作用于未放行批次，放行后只能发布替代版本；
   放行人与生产提交人必须相互独立；
5. 风险与资格变化：只挂起尚未放行的批次，已放行批次不受影响；
6. 匿名报价指数：只有通过门槛的匿名报价进入指数，每条报价的
   纳入 / 排除理由可被追问。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from .errors import (
    ContractError,
    EligibilityError,
    InspectionError,
    IsolationError,
    LedgerError,
    QuotationError,
    ReleaseError,
    SealedVersionError,
    ValidationError,
)
from .models import (
    BagTraceView,
    ContractSubmission,
    ContractVersion,
    IndexPublication,
    InspectionConclusion,
    LineageNode,
    NodeKind,
    PlotBinding,
    PlotOrigin,
    Quotation,
    ReleaseRecord,
    ReleaseState,
    SampleRecord,
    ThresholdRule,
    Verdict,
)

WEIGHT_TOLERANCE = 1e-6


class Role(str, Enum):
    ENTERPRISE = "enterprise"
    COUNTY = "county"
    INSPECTOR = "inspector"
    SECRETARIAT = "secretariat"


@dataclass(frozen=True)
class RiskFlag:
    """花期冲突、气候风险或资格变化对批次的挂起标记。"""

    risk_id: str
    plot_ids: tuple[str, ...]
    reason: str
    held_batches: tuple[str, ...]
    unaffected_released: tuple[str, ...]


@dataclass
class _BatchState:
    batch_id: str
    produced_by: str | None          # 产出该批的谱系节点
    available_kg: float              # 尚未被下游事件或抽样占用的重量
    release_state: ReleaseState = ReleaseState.PENDING
    holds: list[str] = field(default_factory=list)   # 生效中的风险标记


class StewardshipService:
    def __init__(self) -> None:
        # 账户与权限
        self._roles: dict[str, Role] = {}
        self._employer: dict[str, str] = {}          # 企业账户 -> 企业名

        # 合同
        self._versions: dict[str, list[ContractVersion]] = {}

        # 谱系台账
        self._nodes: dict[str, LineageNode] = {}
        self._node_order: list[str] = []
        self._batches: dict[str, _BatchState] = {}
        self._samples: dict[str, SampleRecord] = {}
        self._grower_grades: dict[str, int] = {}    # 受托人当前等级（资格变化）
        self._bags: dict[str, tuple[str, float, str]] = {}   # 袋号 -> (批次, 重量, 包装人)
        self._packaged_batches: set[str] = set()    # 已包装成品的批次，谱系冻结

        # 检验与放行
        self._conclusions: dict[str, list[InspectionConclusion]] = {}
        self._releases: dict[str, list[ReleaseRecord]] = {}

        # 风险
        self._risks: list[RiskFlag] = []

        # 报价与指数
        self._quotes: dict[str, Quotation] = {}
        self._thresholds: dict[tuple[str, str], ThresholdRule] = {}
        self._publications: dict[tuple[str, str], IndexPublication] = {}

    # ------------------------------------------------------------ 账户

    def register_actor(self, actor: str, role: Role, employer: str = "") -> None:
        if actor in self._roles:
            raise ValidationError(f"账户 {actor} 已注册")
        self._roles[actor] = role
        if role is Role.ENTERPRISE:
            if not employer:
                raise ValidationError("企业账户必须登记所属企业")
            self._employer[actor] = employer

    def _require_role(self, actor: str, role: Role) -> None:
        if self._roles.get(actor) is not role:
            raise PermissionError(f"{actor} 不具备 {role.value} 角色")

    # ------------------------------------------------------------ 合同

    def submit_contract(self, submission: ContractSubmission) -> ContractVersion:
        """企业提交品种权利、亲本来源、目标数量与技术规程，形成合同 v1。"""
        submission.validate()
        self._require_role(submission.submitted_by, Role.ENTERPRISE)
        if self._employer.get(submission.submitted_by) != submission.enterprise:
            raise PermissionError("提交人只能为本企业提交合同")
        contract_id = f"C-{submission.enterprise}-{submission.rights.variety}-{len(self._versions) + 1:03d}"
        version = ContractVersion(contract_id, 1, submission)
        self._versions[contract_id] = [version]
        return version

    def current_contract(self, contract_id: str) -> ContractVersion:
        history = self._versions.get(contract_id)
        if not history:
            raise ContractError(f"合同 {contract_id} 不存在")
        return history[-1]

    def contract_history(self, contract_id: str) -> list[ContractVersion]:
        history = self._versions.get(contract_id)
        if not history:
            raise ContractError(f"合同 {contract_id} 不存在")
        return list(history)

    def bind_plots(
        self,
        contract_id: str,
        bindings: Sequence[PlotBinding],
        by: str,
        note: str = "",
    ) -> ContractVersion:
        """县级人员把地块、受托人等级、隔离区、播插窗口及田间检查绑定到新合同版本。"""
        self._require_role(by, Role.COUNTY)
        current = self.current_contract(contract_id)
        if not bindings:
            raise ValidationError("至少绑定一个地块")

        protocol = current.submission.protocol
        plot_ids: set[str] = set()
        for binding in bindings:
            if binding.plot_id in plot_ids:
                raise ValidationError(f"地块 {binding.plot_id} 在本版本中重复绑定")
            plot_ids.add(binding.plot_id)
            current_grade = self._grower_grades.get(binding.grower_id, binding.grower_grade)
            if current_grade < protocol.min_grower_grade:
                raise EligibilityError(
                    f"受托人 {binding.grower_id} 等级 {current_grade} "
                    f"低于品种要求 {protocol.min_grower_grade}"
                )
            if binding.isolation_m < protocol.min_isolation_m:
                raise IsolationError(
                    f"地块 {binding.plot_id} 隔离 {binding.isolation_m}m "
                    f"小于规程 {protocol.min_isolation_m}m"
                )
            if not protocol.window_covers(binding.sowing_day):
                raise IsolationError(
                    f"地块 {binding.plot_id} 播期 {binding.sowing_day} 不在允许窗口 "
                    f"{protocol.sowing_window}"
                )
            self._check_flowering_conflicts(binding, current.variety)

        new_version = current.with_bindings(bindings, note)
        self._versions[contract_id].append(new_version)
        return new_version

    def _check_flowering_conflicts(self, binding: PlotBinding, variety: str) -> None:
        """与所有其他合同已绑定地块比对：异品种、花期重叠且距离不足即冲突。"""
        for contract_id, history in self._versions.items():
            other_contract = history[-1]
            if not other_contract.is_bound or other_contract.variety == variety:
                continue
            required = max(
                other_contract.submission.protocol.min_isolation_m,
                self.current_contract(contract_id).submission.protocol.min_isolation_m,
            )
            for other in other_contract.bindings:
                if not binding.flowering_overlaps(other):
                    continue
                distance = binding.distance_m_to(other)
                if distance < required:
                    raise IsolationError(
                        f"地块 {binding.plot_id} 与相邻异品种 {other_contract.variety} "
                        f"地块 {other.plot_id} 花期重叠且相距约 {distance:.0f}m，"
                        f"小于隔离要求 {required}m"
                    )

    def add_field_checks(
        self, contract_id: str, plot_id: str, checks: Sequence[str], by: str
    ) -> ContractVersion:
        """田间检查追加到地块记录，合同派生新版本。"""
        self._require_role(by, Role.COUNTY)
        current = self.current_contract(contract_id)
        updated: list[PlotBinding] = []
        found = False
        for binding in current.bindings:
            if binding.plot_id == plot_id:
                found = True
                values = {**binding.__dict__, "field_checks": (*binding.field_checks, *checks)}
                binding = PlotBinding(**values)
            updated.append(binding)
        if not found:
            raise ContractError(f"地块 {plot_id} 未绑定到 {contract_id}")
        new_version = current.with_bindings(updated, f"田间检查更新：{plot_id}")
        self._versions[contract_id].append(new_version)
        return new_version

    # ------------------------------------------------------------ 谱系台账

    def record_harvest(
        self,
        node_id: str,
        contract_id: str,
        contract_version: int,
        plot_id: str,
        batch_id: str,
        weight_kg: float,
        by: str,
    ) -> LineageNode:
        """登记收获：地块产出首次进入台账。"""
        version = self._contract_version_at(contract_id, contract_version)
        plot = next((b for b in version.bindings if b.plot_id == plot_id), None)
        if plot is None:
            raise ContractError(f"地块 {plot_id} 不属于合同 {contract_id} v{contract_version}")
        if weight_kg <= 0:
            raise ValidationError("收获重量必须为正")
        node = LineageNode(
            node_id=node_id,
            kind=NodeKind.HARVEST,
            inputs=(),
            outputs=(batch_id,),
            weights_in={},
            weights_out={batch_id: weight_kg},
            contract_id=contract_id,
            contract_version=contract_version,
            recorded_by=by,
            plots=(plot_id,),
        )
        self._commit_node(node)
        return node

    def record_transformation(
        self,
        node_id: str,
        kind: NodeKind,
        contract_id: str,
        contract_version: int,
        inputs: Mapping[str, float],
        outputs: Mapping[str, float],
        by: str,
        processing_loss_kg: float = 0.0,
        note: str = "",
    ) -> LineageNode:
        """登记混样 / 拆批 / 合批 / 加工，强制重量守恒与余量占用。"""
        if kind is NodeKind.HARVEST:
            raise LedgerError("收获必须使用 record_harvest 登记")
        if not inputs or not outputs:
            raise LedgerError("转化事件必须同时有输入批与输出批")
        if processing_loss_kg < 0:
            raise LedgerError("加工损耗不可为负")
        if processing_loss_kg and kind is not NodeKind.PROCESS:
            raise LedgerError("只有加工环节可以登记损耗")
        self._contract_version_at(contract_id, contract_version)
        node = LineageNode(
            node_id=node_id,
            kind=kind,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            weights_in=dict(inputs),
            weights_out=dict(outputs),
            contract_id=contract_id,
            contract_version=contract_version,
            recorded_by=by,
            processing_loss_kg=processing_loss_kg,
            note=note,
        )
        self._commit_node(node)
        return node

    def _contract_version_at(self, contract_id: str, number: int) -> ContractVersion:
        history = self._versions.get(contract_id)
        if not history:
            raise ContractError(f"合同 {contract_id} 不存在")
        for version in history:
            if version.version == number:
                return version
        raise ContractError(f"合同 {contract_id} 不存在版本 {number}")

    def _commit_node(self, node: LineageNode) -> None:
        """校验通过后原子提交：守恒、未占用余量、放行冻结、输出唯一。"""
        if node.node_id in self._nodes:
            raise LedgerError(f"谱系事件 {node.node_id} 重复登记")
        for batch_id in (*node.inputs, *node.outputs):
            if batch_id in self._packaged_batches:
                raise LedgerError(f"批次 {batch_id} 已包装为成品，不可再变动")

        # 守恒
        if node.kind is not NodeKind.HARVEST:
            expected_out = node.total_in() - round(node.processing_loss_kg, 6)
            if abs(expected_out - node.total_out()) > WEIGHT_TOLERANCE:
                raise LedgerError(
                    f"{node.node_id} 重量不守恒：输入 {node.total_in()} - "
                    f"损耗 {node.processing_loss_kg} != 输出 {node.total_out()}"
                )
        elif node.total_in() != 0 or node.total_out() <= 0:
            raise LedgerError("收获事件只允许有正的输出重量")

        # 余量占用（并行事件不得重复占用同一数量）
        for batch_id, weight in node.weights_in.items():
            state = self._batches.get(batch_id)
            if state is None:
                raise LedgerError(f"输入批 {batch_id} 不存在谱系来源")
            if state.release_state is ReleaseState.RELEASED:
                raise SealedVersionError(f"批次 {batch_id} 已放行冻结，不能再被占用")
            if weight <= 0:
                raise LedgerError(f"占用 {batch_id} 的重量必须为正")
            if weight - state.available_kg > WEIGHT_TOLERANCE:
                raise LedgerError(
                    f"批次 {batch_id} 余量 {state.available_kg}kg 不足，"
                    f"本事件申请占用 {weight}kg，数量不得被并行加工或抽样重复占用"
                )
        for batch_id in node.outputs:
            if batch_id in self._batches:
                raise LedgerError(f"输出批 {batch_id} 已由其他事件产生")

        # 全部校验通过后才落账
        for batch_id, weight in node.weights_in.items():
            self._batches[batch_id].available_kg = round(
                self._batches[batch_id].available_kg - weight, 6
            )
        for batch_id, weight in node.weights_out.items():
            self._batches[batch_id] = _BatchState(batch_id, node.node_id, weight)
        self._nodes[node.node_id] = node
        self._node_order.append(node.node_id)

    def draw_sample(
        self, sample_id: str, batch_id: str, weight_kg: float, node_id: str, by: str
    ) -> SampleRecord:
        """从批次抽取检验样品；样品重量同样占用批次数量。"""
        if sample_id in self._samples:
            raise InspectionError(f"样品 {sample_id} 重复登记")
        state = self._batches.get(batch_id)
        if state is None:
            raise InspectionError(f"批次 {batch_id} 不存在")
        if node_id not in self._nodes:
            raise InspectionError(f"谱系事件 {node_id} 不存在，样品无法挂接台账")
        node = self._nodes[node_id]
        if batch_id not in node.outputs and batch_id not in node.inputs:
            raise InspectionError(f"样品批次 {batch_id} 与谱系事件 {node_id} 无关")
        if state.release_state is ReleaseState.RELEASED:
            raise SealedVersionError(f"批次 {batch_id} 已放行冻结，不得再抽样")
        if weight_kg <= 0:
            raise InspectionError("样品重量必须为正")
        if weight_kg - state.available_kg > WEIGHT_TOLERANCE:
            raise LedgerError(
                f"批次 {batch_id} 余量 {state.available_kg}kg 不足以抽取 {weight_kg}kg 样品"
            )
        state.available_kg = round(state.available_kg - weight_kg, 6)
        record = SampleRecord(sample_id, batch_id, weight_kg, by, node_id)
        self._samples[sample_id] = record
        return record

    def package_bag(self, bag_id: str, batch_id: str, weight_kg: float, by: str) -> None:
        """从已放行成品批包装一袋可扫码追溯的成品。"""
        if bag_id in self._bags:
            raise LedgerError(f"成品袋 {bag_id} 已存在")
        state = self._batches.get(batch_id)
        if state is None:
            raise LedgerError(f"批次 {batch_id} 不存在")
        if state.release_state is not ReleaseState.RELEASED:
            raise ReleaseError(f"批次 {batch_id} 尚未放行，不得包装成品")
        if weight_kg <= 0 or weight_kg - state.available_kg > WEIGHT_TOLERANCE:
            raise LedgerError(f"成品批 {batch_id} 余量不足，无法包装 {weight_kg}kg")
        state.available_kg = round(state.available_kg - weight_kg, 6)
        self._bags[bag_id] = (batch_id, weight_kg, by)
        self._packaged_batches.add(batch_id)

    # ------------------------------------------------------------ 检验与放行

    def record_inspection(
        self,
        sample_id: str,
        inspector: str,
        verdict: Verdict,
        indicators: Mapping[str, float],
        note: str = "",
    ) -> InspectionConclusion:
        self._require_role(inspector, Role.INSPECTOR)
        sample = self._samples.get(sample_id)
        if sample is None:
            raise InspectionError(f"样品 {sample_id} 不存在")
        if not indicators:
            raise InspectionError("检验指标不可为空")
        state = self._batches[sample.batch_id]
        if state.release_state is ReleaseState.RELEASED:
            raise SealedVersionError(
                f"批次 {sample.batch_id} 已放行，不能改写结论；请使用 correct_conclusion"
            )
        if self._conclusions.get(sample.batch_id):
            raise SealedVersionError("该批次已有结论，复核请使用 review_inspection")
        conclusion = InspectionConclusion(
            sample_id=sample_id,
            batch_id=sample.batch_id,
            inspector=inspector,
            verdict=verdict,
            indicators=dict(indicators),
            note=note,
        )
        self._conclusions[sample.batch_id] = [conclusion]
        return conclusion

    def review_inspection(
        self,
        sample_id: str,
        inspector: str,
        verdict: Verdict,
        indicators: Mapping[str, float],
        note: str = "",
    ) -> InspectionConclusion:
        """检验复核：仅允许作用于尚未放行的批次，以新版本覆盖待放行结论。"""
        self._require_role(inspector, Role.INSPECTOR)
        sample = self._samples.get(sample_id)
        if sample is None:
            raise InspectionError(f"样品 {sample_id} 不存在")
        history = self._conclusions.get(sample.batch_id)
        if not history:
            raise InspectionError("该批次尚无初检结论")
        if self._batches[sample.batch_id].release_state is ReleaseState.RELEASED:
            raise SealedVersionError(
                "批次已放行，复核不能改变已发布结论；请使用 correct_conclusion"
            )
        new = InspectionConclusion(
            sample_id=sample_id,
            batch_id=sample.batch_id,
            inspector=inspector,
            verdict=verdict,
            indicators=dict(indicators),
            version=history[-1].version + 1,
            supersedes=history[-1].version,
            note=note or "检验复核",
        )
        history.append(new)
        return new

    def correct_conclusion(
        self,
        batch_id: str,
        inspector: str,
        verdict: Verdict,
        indicators: Mapping[str, float],
        note: str = "",
    ) -> InspectionConclusion:
        """对已放行结论发布替代版本；历史结论保留，不合格时撤销放行。"""
        self._require_role(inspector, Role.INSPECTOR)
        history = self._conclusions.get(batch_id)
        if not history:
            raise InspectionError("该批次尚无检验结论")
        state = self._batches[batch_id]
        if state.release_state is not ReleaseState.RELEASED:
            raise ReleaseError("替代版本只用于纠正已放行结论；未放行批次请走复核")
        corrected = InspectionConclusion(
            sample_id=history[-1].sample_id,
            batch_id=batch_id,
            inspector=inspector,
            verdict=verdict,
            indicators=dict(indicators),
            version=history[-1].version + 1,
            supersedes=history[-1].version,
            note=note or "放行后替代版本纠正",
        )
        history.append(corrected)
        if verdict is not Verdict.QUALIFIED:
            state.release_state = ReleaseState.REVOKED
            self._releases[batch_id].append(
                ReleaseRecord(
                    batch_id=batch_id,
                    released_by=inspector,
                    producer=self._producer_of(batch_id),
                    conclusion_version=corrected.version,
                    released=False,
                    note="替代版本撤销原放行",
                )
            )
        return corrected

    def release_batch(self, batch_id: str, released_by: str, note: str = "") -> ReleaseRecord:
        """质量放行：最新结论合格、无挂起风险、受托人资格仍满足，且放行人独立于提交人。"""
        state = self._batches.get(batch_id)
        if state is None:
            raise ReleaseError(f"批次 {batch_id} 不存在")
        # 独立性先于角色核对：生产提交人即便兼任检验角色也不得自行放行
        producer = self._producer_of(batch_id)
        if released_by == producer:
            raise ReleaseError("质量放行人与生产提交人必须相互独立")
        self._require_role(released_by, Role.INSPECTOR)
        if state.release_state is ReleaseState.REVOKED:
            raise ReleaseError(f"批次 {batch_id} 已被替代版本撤销")
        if state.release_state is ReleaseState.RELEASED:
            raise SealedVersionError(f"批次 {batch_id} 已放行")
        if state.holds:
            raise ReleaseError(f"批次 {batch_id} 存在未解除的风险挂起：{state.holds}")

        history = self._conclusions.get(batch_id, [])
        if not history:
            raise ReleaseError(f"批次 {batch_id} 尚无检验结论")
        latest = history[-1]
        if latest.verdict is not Verdict.QUALIFIED:
            raise ReleaseError(f"批次 {batch_id} 最新结论为 {latest.verdict.value}，不得放行")

        # 受托人资格可能已变化：放行时按当前等级重新核对
        self._assert_growers_still_eligible(batch_id)

        record = ReleaseRecord(batch_id, released_by, producer, latest.version, True, note)
        state.release_state = ReleaseState.RELEASED
        self._releases.setdefault(batch_id, []).append(record)
        return record

    def _producer_of(self, batch_id: str) -> str:
        contract_id = self._origin_contract(batch_id)
        return self.current_contract(contract_id).submission.submitted_by

    def _assert_growers_still_eligible(self, batch_id: str) -> None:
        for origin in self.plot_origins(batch_id):
            contract = self.current_contract(origin.contract_id)
            binding = next(b for b in contract.bindings if b.plot_id == origin.plot_id)
            current_grade = self._grower_grades.get(binding.grower_id, binding.grower_grade)
            if current_grade < contract.submission.protocol.min_grower_grade:
                raise EligibilityError(
                    f"批次 {batch_id} 来源受托人 {binding.grower_id} 资格已降为 "
                    f"{current_grade}，低于品种要求，放行被阻止"
                )

    # ------------------------------------------------------------ 风险与资格

    def change_grower_grade(self, grower_id: str, new_grade: int, by: str) -> RiskFlag:
        """受托人等级变化：只挂起尚未放行的批次；已放行批次列入不受影响。"""
        self._require_role(by, Role.COUNTY)
        self._grower_grades[grower_id] = new_grade
        held: list[str] = []
        unaffected: list[str] = []
        touched_plots: set[str] = set()
        for batch_id, state in self._batches.items():
            origins = self.plot_origins(batch_id)
            bindings = [
                (o, self._binding_of(o.contract_id, o.plot_id)) for o in origins
            ]
            if not any(b.grower_id == grower_id for _, b in bindings):
                continue
            touched_plots.update(o.plot_id for o, b in bindings if b.grower_id == grower_id)
            if state.release_state is ReleaseState.RELEASED:
                unaffected.append(batch_id)
            else:
                held.append(batch_id)
        risk = self._new_risk(tuple(sorted(touched_plots)),
                              f"受托人 {grower_id} 等级变更为 {new_grade}")
        for batch_id in held:
            self._batches[batch_id].holds.append(risk.risk_id)
        return RiskFlag(risk.risk_id, risk.plot_ids, risk.reason,
                        tuple(held), tuple(unaffected))

    def flag_risk(self, plot_ids: Sequence[str], reason: str, by: str) -> RiskFlag:
        """登记花期冲突或气候风险：来源批次未放行则挂起，已放行的不动。"""
        self._require_role(by, Role.COUNTY)
        plot_set = set(plot_ids)
        held: list[str] = []
        unaffected: list[str] = []
        for batch_id, state in self._batches.items():
            touched = any(o.plot_id in plot_set for o in self.plot_origins(batch_id))
            if not touched:
                continue
            if state.release_state is ReleaseState.RELEASED:
                unaffected.append(batch_id)
            else:
                held.append(batch_id)
        risk = self._new_risk(tuple(plot_ids), reason)
        for batch_id in held:
            self._batches[batch_id].holds.append(risk.risk_id)
        return RiskFlag(risk.risk_id, risk.plot_ids, risk.reason,
                        tuple(held), tuple(unaffected))

    def _new_risk(self, plot_ids: tuple[str, ...], reason: str) -> RiskFlag:
        risk = RiskFlag(f"R-{len(self._risks) + 1:04d}", plot_ids, reason, (), ())
        self._risks.append(risk)
        return risk

    def clear_risk(self, risk_id: str, by: str) -> None:
        """县级人员确认风险解除后，解除对未放行批次的挂起。"""
        self._require_role(by, Role.COUNTY)
        if not any(r.risk_id == risk_id for r in self._risks):
            raise ContractError(f"风险标记 {risk_id} 不存在")
        for state in self._batches.values():
            if risk_id in state.holds:
                state.holds.remove(risk_id)

    def _binding_of(self, contract_id: str, plot_id: str) -> PlotBinding:
        contract = self.current_contract(contract_id)
        return next(b for b in contract.bindings if b.plot_id == plot_id)

    # ------------------------------------------------------------ 谱系追溯

    def plot_origins(self, batch_id: str) -> list[PlotOrigin]:
        """沿谱系回溯批次的全部来源地块与亲本批次。"""
        acc: dict[tuple[str, str], PlotOrigin] = {}
        self._collect_origins(batch_id, acc)
        return list(acc.values())

    def _collect_origins(
        self, batch_id: str, acc: dict[tuple[str, str], PlotOrigin]
    ) -> None:
        state = self._batches.get(batch_id)
        if state is None or state.produced_by is None:
            return
        node = self._nodes[state.produced_by]
        if node.kind is NodeKind.HARVEST:
            contract = self._contract_version_at(node.contract_id, node.contract_version)
            rights = contract.submission.rights
            for plot_id in node.plots:
                binding = next(b for b in contract.bindings if b.plot_id == plot_id)
                acc[(contract.contract_id, plot_id)] = PlotOrigin(
                    contract_id=contract.contract_id,
                    contract_version=contract.version,
                    plot_id=plot_id,
                    county=binding.county,
                    grower_id=binding.grower_id,
                    grower_grade=binding.grower_grade,
                    parent_female_batch=rights.parent_female_batch,
                    parent_male_batch=rights.parent_male_batch,
                    parent_source=rights.parent_source,
                )
        for upstream in node.inputs:
            self._collect_origins(upstream, acc)

    def _origin_contract(self, batch_id: str) -> str:
        origins = self.plot_origins(batch_id)
        if not origins:
            raise LedgerError(f"批次 {batch_id} 缺少收获来源")
        return origins[0].contract_id

    def _lineage_chain(self, batch_id: str) -> list[LineageNode]:
        """从成品批沿第一输入回溯到收获的事件链（按发生顺序返回）。"""
        chain: list[LineageNode] = []
        cursor = batch_id
        seen: set[str] = set()
        while cursor not in seen:
            seen.add(cursor)
            state = self._batches.get(cursor)
            if state is None or state.produced_by is None:
                break
            node = self._nodes[state.produced_by]
            chain.append(node)
            if not node.inputs:
                break
            cursor = node.inputs[0]
        return list(reversed(chain))

    def _family_batches(self, batch_id: str) -> list[str]:
        """成品批及其全部上游批（用于汇集检验历程）。"""
        result = [batch_id]
        state = self._batches.get(batch_id)
        if state is not None and state.produced_by is not None:
            node = self._nodes[state.produced_by]
            for upstream in node.inputs:
                result.extend(self._family_batches(upstream))
        return result

    def scan_bag(self, bag_id: str, by: str) -> BagTraceView:
        """抽查人员扫描成品袋：查看来源地块、亲本批次、谱系事件与检验放行历程。"""
        self._require_role(by, Role.INSPECTOR)
        if bag_id not in self._bags:
            raise LedgerError(f"成品袋 {bag_id} 不存在")
        batch_id, _, _ = self._bags[bag_id]
        origins = self.plot_origins(batch_id)
        if not origins:
            raise LedgerError(f"成品袋 {bag_id} 的谱系不完整")

        inspections: list[InspectionConclusion] = []
        for member in self._family_batches(batch_id):
            inspections.extend(self._conclusions.get(member, []))
        state = self._batches[batch_id]
        return BagTraceView(
            bag_id=bag_id,
            final_batch=batch_id,
            origins=tuple(origins),
            node_chain=tuple(self._lineage_chain(batch_id)),
            inspections=tuple(inspections),
            releases=tuple(self._releases.get(batch_id, [])),
            release_state=state.release_state,
        )

    # ------------------------------------------------------------ 可见性

    def view_contract(self, contract_id: str, actor: str) -> ContractVersion:
        """企业只能看本企业合同明细；县级、检验与秘书处可见全部。"""
        contract = self.current_contract(contract_id)
        role = self._roles.get(actor)
        if role in (Role.COUNTY, Role.SECRETARIAT, Role.INSPECTOR):
            return contract
        if role is Role.ENTERPRISE and self._employer.get(actor) == contract.submission.enterprise:
            return contract
        raise PermissionError("企业不可查看竞争者的合同明细")

    # ------------------------------------------------------------ 报价与指数

    def set_threshold(self, rule: ThresholdRule, by: str) -> None:
        """秘书处设定本期本品种指数的准入门槛。"""
        self._require_role(by, Role.SECRETARIAT)
        self._thresholds[(rule.period, rule.variety)] = rule

    def submit_quotation(self, quote: Quotation, by: str) -> None:
        """企业提交报价；同一企业同一期同一品种只允许一条。"""
        self._require_role(by, Role.ENTERPRISE)
        if self._employer.get(by) != quote.enterprise:
            raise PermissionError("只能以本企业名义提交报价")
        quote.validate()
        if quote.quote_id in self._quotes:
            raise QuotationError(f"报价 {quote.quote_id} 重复提交")
        for existing in self._quotes.values():
            if (
                existing.enterprise == quote.enterprise
                and existing.period == quote.period
                and existing.variety == quote.variety
            ):
                raise QuotationError("同一企业对本期本品种只能提交一条报价")
        self._quotes[quote.quote_id] = quote

    def publish_index(self, period: str, variety: str, by: str) -> IndexPublication:
        """秘书处编制指数：仅匿名使用通过门槛的报价，保留逐条门槛判定。"""
        self._require_role(by, Role.SECRETARIAT)
        key = (period, variety)
        if key in self._publications:
            raise QuotationError("本期指数已发布")
        rule = self._thresholds.get(key)
        if rule is None:
            raise QuotationError("本期本品种尚未设定准入门槛")

        included: list[str] = []
        excluded: dict[str, str] = {}
        weighted_sum = 0.0
        total_quantity = 0.0
        for quote_id, quote in self._quotes.items():
            accepted, reason = rule.accepts(quote)
            if accepted:
                included.append(quote_id)
                weighted_sum += quote.price_per_kg * quote.quantity_kg
                total_quantity += quote.quantity_kg
            else:
                excluded[quote_id] = reason

        if len(included) < rule.min_quotes:
            publication = IndexPublication(
                period=period,
                variety=variety,
                index_value=None,
                included=tuple(included),
                excluded=excluded,
                threshold=rule,
                published=False,
            )
        else:
            publication = IndexPublication(
                period=period,
                variety=variety,
                index_value=round(weighted_sum / total_quantity, 4),
                included=tuple(included),
                excluded=excluded,
                threshold=rule,
                published=True,
            )
        self._publications[key] = publication
        return publication

    def explain_quote(self, period: str, variety: str, quote_id: str, by: str) -> str:
        """追问某条报价进入或未进入当期指数的具体门槛。"""
        role = self._roles.get(by)
        if role not in (Role.SECRETARIAT, Role.INSPECTOR, Role.COUNTY):
            quote = self._quotes.get(quote_id)
            if quote is None or self._employer.get(by) != quote.enterprise:
                raise PermissionError("只能追问本企业报价的门槛判定")
        publication = self._publications.get((period, variety))
        if publication is None:
            raise QuotationError("本期指数尚未编制")
        return publication.reason_for(quote_id)
