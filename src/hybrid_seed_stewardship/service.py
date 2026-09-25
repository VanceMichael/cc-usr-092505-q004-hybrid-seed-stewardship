"""制种受托与质量追溯服务：实现领域不变量。

关键规则：

* 备案与合同只追加版本，已发布结论只能由替代版本纠正；
* 收获、混样、加工、抽样、拆批、合批严格重量守恒，谱系（地块、亲本批次）
  向下传播，不同亲本批次的种子不得混合；
* 并行加工/抽样通过"预约"先占数量，同一数量不能被重复占用；
* 花期冲突、气候风险、受托人资格变化、检验复核只作用于尚未放行的批次；
* 放行人为独立角色，且不得是该批次的生产提交人；
* 企业只能看到本企业明细。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable, Mapping

from .model import (
    EventType,
    FieldInspection,
    Inspection,
    IsolationZone,
    Lot,
    MaterialEvent,
    Package,
    Plot,
    PRODUCTION_ROLES,
    Release,
    ReleaseCorrection,
    Role,
    SeedContract,
    StewardGrade,
    StewardQualification,
    User,
    VarietyFiling,
    Weight,
)

ZERO = Decimal("0")


class DomainError(ValueError):
    """业务规则被违反。"""


class AuthorizationError(DomainError):
    """角色或归属不允许执行该操作。"""


class ConservationError(DomainError):
    """重量不守恒或数量被重复占用。"""


class ConflictError(DomainError):
    """隔离/花期冲突或状态冲突。"""


class StateError(DomainError):
    """批次当前状态不允许该操作（如已放行）。"""


def _overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start <= b_end and b_start <= a_end


class StewardshipService:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self._qualifications: dict[str, list[StewardQualification]] = defaultdict(list)

        self.zones: dict[str, IsolationZone] = {}
        self.plots: dict[str, Plot] = {}
        # 相邻隔离区之间的实际距离（米）：frozenset({zone_a, zone_b}) -> 距离
        self._adjacency: dict[frozenset[str], int] = {}

        self.filings: dict[str, dict[int, VarietyFiling]] = defaultdict(dict)
        self.contracts: dict[str, dict[int, SeedContract]] = defaultdict(dict)
        self.field_inspections: dict[str, FieldInspection] = {}

        self.lots: dict[str, Lot] = {}
        self.events: list[MaterialEvent] = []
        self._lot_events: dict[str, list[str]] = defaultdict(list)
        self._balance: dict[str, Weight] = defaultdict(lambda: ZERO)
        self._holds: dict[str, dict[str, Weight]] = defaultdict(dict)  # lot -> reserve -> 重量
        self._reserve_lots: dict[str, frozenset[str]] = {}
        self._production_actors: dict[str, set[str]] = defaultdict(set)

        self.inspections: dict[str, Inspection] = {}
        self.releases: dict[str, Release] = {}
        self.corrections: list[ReleaseCorrection] = []
        self._lot_release: dict[str, str] = {}        # lot -> 原始放行编号
        self._active_suspensions: dict[str, str] = {}  # lot -> 暂停原因

        self.packages: dict[str, Package] = {}

    # ------------------------------------------------------------------ 用户

    def register_user(self, user: User) -> User:
        if user.user_id in self.users:
            raise StateError("用户已存在")
        self.users[user.user_id] = user
        return user

    def _user(self, user_id: str) -> User:
        try:
            return self.users[user_id]
        except KeyError:
            raise AuthorizationError("用户未登记") from None

    def _require_role(self, user_id: str, roles: Iterable[Role]) -> User:
        user = self._user(user_id)
        if user.role not in frozenset(roles):
            raise AuthorizationError(f"角色 {user.role.value} 无权执行该操作")
        return user

    # ------------------------------------------------------------ 受托人资格

    def record_steward_grade(
        self, user_id: str, steward_id: str, q: StewardQualification
    ) -> None:
        """登记受托人某时段等级。等级变为 REVOKED 时暂停其未放行批次。"""
        self._require_role(user_id, {Role.COUNTY, Role.SECRETARIAT})
        self._qualifications[steward_id].append(q)
        if q.grade is StewardGrade.REVOKED:
            for lot in self._lots_of_steward(steward_id):
                self.suspend_lot(lot, f"受托人资格变化:{q.valid_from}", allow_released=False)

    def grade_at(self, steward_id: str, day: str) -> StewardGrade:
        """返回受托人在指定日期适用的等级（取该日有效的最近一条登记）。"""
        effective = [
            q
            for q in self._qualifications.get(steward_id, ())
            if q.valid_from <= day and (q.valid_to is None or day <= q.valid_to)
        ]
        if not effective:
            raise DomainError(f"受托人 {steward_id} 在 {day} 无有效资格登记")
        effective.sort(key=lambda q: q.valid_from)
        return effective[-1].grade

    def _lots_of_steward(self, steward_id: str) -> list[str]:
        return [lid for lid, lot in self.lots.items() if lot.steward_id == steward_id]

    # ---------------------------------------------------------------- 备案

    def submit_filing(self, user_id: str, filing: VarietyFiling) -> VarietyFiling:
        user = self._require_role(user_id, {Role.ENTERPRISE})
        if user.enterprise_id != filing.enterprise_id:
            raise AuthorizationError("不能为其他企业提交备案")
        if filing.version != 1 or self.filings[filing.filing_id]:
            raise StateError("新备案版本号必须为 1")
        self.filings[filing.filing_id][1] = filing
        return filing

    def amend_filing(
        self, user_id: str, filing_id: str, new_version: VarietyFiling
    ) -> VarietyFiling:
        """备案内容变更（如续展品种权、更换亲本批次）产生新版本。"""
        user = self._require_role(user_id, {Role.ENTERPRISE})
        versions = self.filings.get(filing_id, {})
        if not versions:
            raise StateError("原备案不存在")
        latest = max(versions)
        if user.enterprise_id != versions[latest].enterprise_id:
            raise AuthorizationError("不能修改其他企业的备案")
        if new_version.version != latest + 1 or new_version.supersedes != latest:
            raise StateError("新版本号或替代关系不正确")
        if new_version.filing_id != filing_id:
            raise StateError("备案编号不一致")
        versions[new_version.version] = new_version
        return new_version

    def filing_version(self, filing_id: str, version: int) -> VarietyFiling:
        try:
            return self.filings[filing_id][version]
        except KeyError:
            raise StateError("备案版本不存在") from None

    def latest_filing_version(self, filing_id: str) -> int:
        return max(self.filings[filing_id])

    # -------------------------------------------------------- 隔离区与地块

    def register_zone(self, user_id: str, zone: IsolationZone) -> IsolationZone:
        user = self._require_role(user_id, {Role.COUNTY})
        if user.county_id != zone.county_id:
            raise AuthorizationError("只能登记本县隔离区")
        if zone.zone_id in self.zones:
            raise StateError("隔离区已登记")
        self.zones[zone.zone_id] = zone
        return zone

    def set_zone_adjacency(self, user_id: str, zone_a: str, zone_b: str, meters: int) -> None:
        """登记两个相邻隔离区边缘之间的实际距离。

        县内相邻关系由县级人员登记；跨县相邻关系由联盟秘书处登记，
        因为同一品种可能由不同受托人在邻县相邻区域繁育。
        """
        user = self._require_role(user_id, {Role.COUNTY, Role.SECRETARIAT})
        a, b = self.zones[zone_a], self.zones[zone_b]
        if user.role is Role.COUNTY and (
            user.county_id != a.county_id or a.county_id != b.county_id
        ):
            raise AuthorizationError("跨县相邻关系需由联盟秘书处登记")
        self._adjacency[frozenset({zone_a, zone_b})] = meters

    def register_plot(self, user_id: str, plot: Plot) -> Plot:
        user = self._require_role(user_id, {Role.COUNTY})
        if user.county_id != plot.county_id:
            raise AuthorizationError("只能登记本县地块")
        zone = self.zones.get(plot.zone_id)
        if zone is None or zone.county_id != plot.county_id:
            raise StateError("地块所在隔离区不存在或不属于本县")
        if self.grade_at(plot.steward_id, plot.sow_window[0]) is StewardGrade.REVOKED:
            raise AuthorizationError("受托人资格已暂停，不能绑定地块")
        self.plots[plot.plot_id] = plot
        return plot

    def flowering_conflicts(self) -> list[tuple[str, str]]:
        """返回存在花期/隔离冲突的隔离区对。

        同一品种花期相遇不构成污染；不同品种（异花粉源）在同一隔离区或
        距离小于双方隔离要求时，花期不得重叠。
        """
        conflicts: list[tuple[str, str]] = []
        zones = list(self.zones.values())
        for i in range(len(zones)):
            for j in range(i + 1, len(zones)):
                a, b = zones[i], zones[j]
                if a.variety_code == b.variety_code:
                    continue
                if not _overlap(
                    a.flowering_start, a.flowering_end, b.flowering_start, b.flowering_end
                ):
                    continue
                # 只有登记为相邻（含秘书处登记的跨县相邻关系）才比较隔离距离；
                # 距离小于双方隔离要求即冲突。未登记相邻关系的区域不视为相邻。
                distance = self._adjacency.get(frozenset({a.zone_id, b.zone_id}))
                if distance is None:
                    continue
                required = max(a.isolation_distance_m, b.isolation_distance_m)
                if distance < required:
                    conflicts.append((a.zone_id, b.zone_id))
        return conflicts

    def _lots_in_zone(self, zone_id: str) -> list[str]:
        return [
            lid
            for lid, lot in self.lots.items()
            if any(self.plots[p].zone_id == zone_id for p in lot.plot_ids)
        ]

    def flag_flowering_conflicts(self, at: str) -> list[str]:
        """登记新出现的花期冲突：暂停冲突区内尚未放行的批次。

        已放行批次不受影响，直接跳过。
        """
        affected: list[str] = []
        for zone_a, zone_b in self.flowering_conflicts():
            for lot_id in [*self._lots_in_zone(zone_a), *self._lots_in_zone(zone_b)]:
                if lot_id in affected or lot_id in self._lot_release:
                    continue
                self.suspend_lot(lot_id, f"花期冲突:{zone_a}/{zone_b}@{at}")
                affected.append(lot_id)
        return affected

    # ---------------------------------------------------------------- 合同

    def bind_contract(self, user_id: str, contract: SeedContract) -> SeedContract:
        """把地块、受托人等级、隔离区与播插窗口绑定到合同版本。"""
        user = self._require_role(user_id, {Role.COUNTY})
        versions = self.contracts[contract.contract_id]
        expected_version = 1 if not versions else max(versions) + 1
        if contract.version != expected_version:
            raise StateError("合同必须按顺序追加版本")
        if versions and contract.supersedes != max(versions):
            raise StateError("新版本必须替代当前最新版本")

        filing = self.filing_version(contract.variety_filing_id, contract.filing_version)
        if filing.enterprise_id != contract.enterprise_id:
            raise AuthorizationError("合同企业与备案企业不一致")

        plots = [self._plot(pid, user.county_id) for pid in sorted(contract.plot_ids)]
        stewards = {p.steward_id for p in plots}
        if stewards != {contract.steward_id}:
            raise StateError("合同地块必须全部属于合同受托人")
        if self.grade_at(contract.steward_id, contract.sow_window[0]) != contract.steward_grade:
            raise StateError("绑定的受托人等级与有效资格登记不一致")
        if self.grade_at(contract.steward_id, contract.sow_window[0]) is StewardGrade.REVOKED:
            raise AuthorizationError("受托人资格已暂停")

        # 播插窗口必须落在各地块登记窗口之内。
        for p in plots:
            if not (p.sow_window[0] <= contract.sow_window[0]
                    and contract.sow_window[1] <= p.sow_window[1]):
                raise ConflictError(f"地块 {p.plot_id} 播种窗口不覆盖合同窗口")
            if not (p.transplant_window[0] <= contract.transplant_window[0]
                    and contract.transplant_window[1] <= p.transplant_window[1]):
                raise ConflictError(f"地块 {p.plot_id} 插秧窗口不覆盖合同窗口")

        # 同一地块在同一时间只能挂在一份当前有效合同上。
        for other_id, other_versions in self.contracts.items():
            if other_id == contract.contract_id:
                continue
            other = other_versions[max(other_versions)]
            if other.plot_ids & contract.plot_ids:
                raise ConflictError(f"地块已绑定合同 {other_id}")

        # 隔离区不得存在未解决的花期冲突。
        if self.flowering_conflicts():
            raise ConflictError("隔离区存在花期冲突，不能绑定合同")

        versions[contract.version] = contract
        return contract

    def _plot(self, plot_id: str, county_id: str | None = None) -> Plot:
        plot = self.plots.get(plot_id)
        if plot is None:
            raise StateError(f"地块 {plot_id} 不存在")
        if county_id is not None and plot.county_id != county_id:
            raise AuthorizationError("地块不属于本县")
        return plot

    def contract_version(self, contract_id: str, version: int) -> SeedContract:
        try:
            return self.contracts[contract_id][version]
        except KeyError:
            raise StateError("合同版本不存在") from None

    def latest_contract_version(self, contract_id: str) -> int:
        return max(self.contracts[contract_id])

    def bind_field_inspection(self, user_id: str, inspection: FieldInspection) -> None:
        """田间检查绑定到具体合同版本（历史版本不可改写）。"""
        user = self._require_role(user_id, {Role.COUNTY})
        contract = self.contract_version(inspection.contract_id, inspection.contract_version)
        for plot_id in contract.plot_ids:
            if self.plots[plot_id].county_id != user.county_id:
                raise AuthorizationError("只能绑定本县合同的田间检查")
        if inspection.inspection_id in self.field_inspections:
            raise StateError("田间检查编号重复")
        self.field_inspections[inspection.inspection_id] = inspection

    def field_inspections_of(self, contract_id: str, version: int) -> list[FieldInspection]:
        return [
            f
            for f in self.field_inspections.values()
            if f.contract_id == contract_id and f.contract_version == version
        ]

    def _contract_for_plots(self, plot_ids: frozenset[str]) -> SeedContract:
        found: SeedContract | None = None
        for versions in self.contracts.values():
            latest = versions[max(versions)]
            if plot_ids <= latest.plot_ids:
                if found is not None:
                    raise ConflictError("地块跨多份合同，不能并入同一批次")
                found = latest
        if found is None:
            raise StateError("地块尚未绑定当前合同版本")
        return found

    # ------------------------------------------------------------ 重量预约

    def reserve_quantity(
        self, user_id: str, reserve_id: str, allocations: Mapping[str, Weight]
    ) -> None:
        """为并行加工/抽样预先占用数量，占用后其他操作不可重复使用。"""
        self._require_role(user_id, PRODUCTION_ROLES | {Role.INSPECTOR, Role.SAMPLER})
        if reserve_id in self._reserve_lots:
            raise ConflictError("预约编号已存在")
        # 先全部校验通过，再落占用，避免半成功状态。
        for lot_id, qty in allocations.items():
            if lot_id not in self.lots:
                raise StateError(f"批次 {lot_id} 不存在")
            if qty <= 0:
                raise ConservationError("预约重量必须为正")
            if self.available(lot_id) < qty:
                raise ConservationError(f"批次 {lot_id} 可占用数量不足")
        for lot_id, qty in allocations.items():
            self._holds[lot_id][reserve_id] = qty
        self._reserve_lots[reserve_id] = frozenset(allocations)

    def cancel_reservation(self, reserve_id: str) -> None:
        if reserve_id not in self._reserve_lots:
            raise StateError("预约不存在")
        for lot_id in self._reserve_lots.pop(reserve_id):
            self._holds[lot_id].pop(reserve_id, None)

    def available(self, lot_id: str) -> Weight:
        held = sum(self._holds[lot_id].values(), ZERO)
        return self._balance[lot_id] - held

    # ------------------------------------------------------------ 物料事件

    def harvest(
        self,
        user_id: str,
        event_id: str,
        lot_id: str,
        plot_ids: Iterable[str],
        weight: Weight,
        at: str,
    ) -> Lot:
        """收获：地块产出登记为田头批次，谱系随之确定。"""
        user = self._require_role(user_id, PRODUCTION_ROLES)
        if weight <= 0:
            raise ConservationError("收获重量必须为正")
        if lot_id in self.lots:
            raise StateError("批次编号已存在")
        plot_set = frozenset(plot_ids)
        if not plot_set:
            raise StateError("收获必须关联地块")
        contract = self._contract_for_plots(plot_set)
        if user.role is Role.ENTERPRISE and user.enterprise_id != contract.enterprise_id:
            raise AuthorizationError("不能收获其他企业合同的地块")

        plot_zones = {self.plots[p].zone_id for p in plot_set}
        for zone_a, zone_b in self.flowering_conflicts():
            if zone_a in plot_zones or zone_b in plot_zones:
                raise ConflictError(
                    f"地块所在隔离区 {zone_a}/{zone_b} 存在花期冲突，不能收获"
                )

        checks = self.field_inspections_of(contract.contract_id, contract.version)
        if not checks or not all(c.passed for c in checks):
            raise StateError("合同版本缺少全部通过的田间检查，不能收获")
        if self.grade_at(contract.steward_id, at) is StewardGrade.REVOKED:
            raise AuthorizationError("受托人资格已暂停，不能收获")

        filing = self.filing_version(contract.variety_filing_id, contract.filing_version)
        lot = Lot(
            lot_id=lot_id,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            plot_ids=plot_set,
            steward_id=contract.steward_id,
            variety_code=filing.variety_code,
            parent_male_batch=filing.parent_male_batch,
            parent_female_batch=filing.parent_female_batch,
            created_event=event_id,
        )
        self.lots[lot_id] = lot
        event = MaterialEvent(
            event_id=event_id,
            type=EventType.HARVEST,
            at=at,
            actor_user_id=user_id,
            inputs=frozenset(),
            outputs=frozenset({(lot_id, weight)}),
        )
        self._record_event(event, created_lots={lot_id: lot}, actor=user)
        return lot

    def apply_material_event(
        self, user_id: str, event: MaterialEvent
    ) -> MaterialEvent:
        """应用混样/加工/抽样/拆批/合批事件，全程重量守恒。"""
        user = self._require_role(user_id, PRODUCTION_ROLES | {Role.INSPECTOR, Role.SAMPLER})
        if event.type in {EventType.HARVEST, EventType.PACK}:
            raise StateError("收获与包装请使用专用方法")
        if event.event_id in {e.event_id for e in self.events}:
            raise StateError("事件编号重复")

        inputs = dict(event.inputs)
        outputs = dict(event.outputs)
        if not inputs or any(q <= 0 for q in inputs.values()):
            raise ConservationError("消耗重量必须为正")
        if any(q <= 0 for q in outputs.values()) or event.loss < 0:
            raise ConservationError("产出重量必须为正、损耗不能为负")

        in_total = sum(inputs.values(), ZERO)
        out_total = sum(outputs.values(), ZERO)
        if in_total != out_total + event.loss:
            raise ConservationError(
                f"重量不守恒：消耗 {in_total} != 产出 {out_total} + 损耗 {event.loss}"
            )

        for lot_id in inputs:
            if lot_id not in self.lots:
                raise StateError(f"来源批次 {lot_id} 不存在")
            if lot_id in self._lot_release:
                raise StateError(f"批次 {lot_id} 已放行，不能再移动")
            if lot_id in self._active_suspensions:
                raise StateError(f"批次 {lot_id} 处于暂停中，不能移动或抽样")
        if event.type is EventType.SAMPLE and event.loss <= 0:
            raise ConservationError("抽样必须登记取样重量")

        # 占用与扣减：带预约的事件必须与预约逐条对应。
        if event.reserve_id is not None:
            if event.reserve_id not in self._reserve_lots:
                raise StateError("预约不存在")
            held = {lot: self._holds[lot].get(event.reserve_id) for lot in inputs}
            if any(q is None for q in held.values()) or held != inputs:
                raise ConservationError("事件消耗与预约占用不一致")
        else:
            for lot_id, qty in inputs.items():
                if self.available(lot_id) < qty:
                    raise ConservationError(f"批次 {lot_id} 数量不足或已被并行操作占用")

        parents = self.lots[next(iter(inputs))]
        for lot_id in inputs:
            src = self.lots[lot_id]
            if (src.variety_code, src.parent_male_batch, src.parent_female_batch) != (
                parents.variety_code,
                parents.parent_male_batch,
                parents.parent_female_batch,
            ):
                raise ConflictError(f"批次 {lot_id} 品种或亲本批次不一致，禁止混合，防止谱系混淆")

        created: dict[str, Lot] = {}
        for lot_id, qty in outputs.items():
            if lot_id in self.lots:
                raise StateError(f"产出批次 {lot_id} 已存在")
            derived = Lot(
                lot_id=lot_id,
                contract_id=parents.contract_id,
                contract_version=parents.contract_version,
                plot_ids=frozenset().union(*(self.lots[i].plot_ids for i in inputs)),
                steward_id=parents.steward_id,
                variety_code=parents.variety_code,
                parent_male_batch=parents.parent_male_batch,
                parent_female_batch=parents.parent_female_batch,
                created_event=event.event_id,
            )
            created[lot_id] = derived

        self._record_event(event, created_lots=created, actor=user, consume_reservation=True)
        return event

    def _record_event(
        self,
        event: MaterialEvent,
        created_lots: Mapping[str, Lot],
        actor: User,
        consume_reservation: bool = False,
    ) -> None:
        reserve = event.reserve_id
        # 先校验状态，再改动台账，避免部分扣减后失败。
        for lot_id, _qty in event.inputs:
            if lot_id in self._lot_release:
                raise StateError("已放行批次不可移动")
            if lot_id in self._active_suspensions:
                raise StateError(f"批次处于暂停中：{self._active_suspensions[lot_id]}")
        for lot_id, qty in event.inputs:
            if reserve is not None and consume_reservation:
                self._holds[lot_id].pop(reserve, None)
            self._balance[lot_id] -= qty
            self._lot_events[lot_id].append(event.event_id)
            self._production_actors[lot_id].add(actor.user_id)
        if reserve is not None and consume_reservation:
            self._reserve_lots.pop(reserve, None)

        for lot_id, lot in created_lots.items():
            self.lots[lot_id] = lot
        for lot_id, qty in event.outputs:
            self._balance[lot_id] += qty
            self._lot_events[lot_id].append(event.event_id)
            self._production_actors[lot_id].add(actor.user_id)
            # 谱系参与人随产出传播
            for parent_id in event.inputs:
                self._production_actors[lot_id].update(self._production_actors[parent_id])
        self.events.append(event)

    def pack(
        self,
        user_id: str,
        event_id: str,
        lot_id: str,
        packages: Mapping[str, Weight],
        at: str,
        reserve_id: str | None = None,
    ) -> list[Package]:
        """把批次包装为成品袋，袋重之和必须等于占用数量。"""
        user = self._require_role(user_id, PRODUCTION_ROLES)
        if lot_id not in self.lots:
            raise StateError("批次不存在")
        if lot_id in self._lot_release:
            raise StateError("已放行批次不能重新包装")
        if lot_id in self._active_suspensions:
            raise StateError(f"批次处于暂停中：{self._active_suspensions[lot_id]}")
        if not packages or any(w <= 0 for w in packages.values()):
            raise ConservationError("成品袋重量必须为正")
        if any(code in self.packages for code in packages):
            raise StateError("成品袋编码重复")
        total = sum(packages.values(), ZERO)

        inputs = frozenset({(lot_id, total)})
        if reserve_id is not None:
            if self._holds[lot_id].get(reserve_id) != total:
                raise ConservationError("包装数量与预约不一致")
        elif self.available(lot_id) < total:
            raise ConservationError("批次数量不足或已被并行操作占用")

        event = MaterialEvent(
            event_id=event_id,
            type=EventType.PACK,
            at=at,
            actor_user_id=user_id,
            inputs=inputs,
            outputs=frozenset(),
            reserve_id=reserve_id,
        )
        made = [
            Package(package_code=code, lot_id=lot_id, weight=w, event_id=event_id)
            for code, w in packages.items()
        ]
        self._record_event(event, created_lots={}, actor=user, consume_reservation=True)
        for pkg in made:
            self.packages[pkg.package_code] = pkg
        return made

    # ------------------------------------------------------------ 暂停管理

    def flag_climate_risk(self, at: str, lot_ids: Iterable[str], reason: str) -> list[str]:
        """登记气候风险：只暂停尚未放行的批次，已放行结论保持不变。"""
        affected: list[str] = []
        for lot_id in lot_ids:
            if lot_id not in self.lots:
                raise StateError(f"批次 {lot_id} 不存在")
            if lot_id in self._lot_release:
                continue
            self.suspend_lot(lot_id, f"气候风险:{reason}@{at}")
            affected.append(lot_id)
        return affected

    def suspend_lot(self, lot_id: str, reason: str, allow_released: bool = False) -> None:
        if lot_id not in self.lots:
            raise StateError("批次不存在")
        if lot_id in self._lot_release and not allow_released:
            # 已放行结论不受花期/气候/资格变化影响。
            return
        self._active_suspensions[lot_id] = reason

    def resolve_suspension(self, lot_id: str) -> None:
        self._active_suspensions.pop(lot_id, None)

    def suspension_reason(self, lot_id: str) -> str | None:
        return self._active_suspensions.get(lot_id)

    # ---------------------------------------------------------------- 检验

    def record_inspection(self, user_id: str, inspection: Inspection) -> Inspection:
        self._require_role(user_id, {Role.INSPECTOR})
        if inspection.inspection_id in self.inspections:
            raise StateError("检验编号重复")
        if inspection.lot_id not in self.lots:
            raise StateError("被检批次不存在")
        self.inspections[inspection.inspection_id] = inspection
        self._lot_events[inspection.lot_id].append("inspection:" + inspection.inspection_id)
        return inspection

    def review_inspection(
        self, user_id: str, review: Inspection
    ) -> Inspection:
        """检验复核产生新记录。未放行批次以最新复核为准；已放行批次需走替代放行。"""
        self._require_role(user_id, {Role.INSPECTOR})
        if review.review_of is None:
            raise StateError("复核必须指向原检验")
        original = self.inspections.get(review.review_of)
        if original is None:
            raise StateError("原检验不存在")
        if review.lot_id != original.lot_id:
            raise StateError("复核批次必须与原检验一致")
        if review.inspection_id in self.inspections:
            raise StateError("检验编号重复")
        self.inspections[review.inspection_id] = review
        self._lot_events[review.lot_id].append("inspection:" + review.inspection_id)
        # 复核不通过只暂停尚未放行的批次。
        if not review.passed and review.lot_id not in self._lot_release:
            self.suspend_lot(review.lot_id, f"检验复核不通过:{review.inspection_id}")
        elif review.passed and review.lot_id not in self._lot_release:
            reason = self._active_suspensions.get(review.lot_id, "")
            if reason.startswith("检验复核不通过:"):
                self.resolve_suspension(review.lot_id)
        return review

    def latest_inspection(self, lot_id: str) -> Inspection | None:
        chain = [
            ins
            for ins in self.inspections.values()
            if ins.lot_id == lot_id
        ]
        if not chain:
            return None
        chain.sort(key=lambda i: i.at)
        return chain[-1]

    # ---------------------------------------------------------------- 放行

    def release_lot(
        self,
        user_id: str,
        release: Release,
    ) -> Release:
        user = self._require_role(user_id, {Role.RELEASE_OFFICER})
        lot = self.lots.get(release.lot_id)
        if lot is None:
            raise StateError("批次不存在")
        if release.lot_id in self._lot_release:
            raise StateError("批次已有放行结论，请使用替代版本纠正")
        if user_id in self._production_actors[release.lot_id]:
            raise AuthorizationError("放行人不得是该批次的生产提交人")

        inspection = self.inspections.get(release.inspection_id)
        if inspection is None or inspection.lot_id != release.lot_id:
            raise StateError("放行依据的检验不存在或不属于该批次")
        latest = self.latest_inspection(release.lot_id)
        if latest is not None and latest.inspection_id != inspection.inspection_id:
            raise StateError("存在更新的检验/复核，放行必须依据最新结论")
        if release.released and not inspection.passed:
            raise StateError("检验未通过，不能放行")
        contract_checks = self.field_inspections_of(lot.contract_id, lot.contract_version)
        if release.released and not all(c.passed for c in contract_checks):
            raise StateError("合同版本存在未通过的田间检查")
        if release.released and release.lot_id in self._active_suspensions:
            raise StateError(
                f"批次处于暂停中：{self._active_suspensions[release.lot_id]}"
            )

        self.releases[release.release_id] = release
        self._lot_release[release.lot_id] = release.release_id
        self._active_suspensions.pop(release.lot_id, None)
        return release

    def correct_release(
        self, user_id: str, correction: ReleaseCorrection
    ) -> ReleaseCorrection:
        """对已发布放行结论发布替代版本，原结论保留可溯。"""
        self._require_role(user_id, {Role.RELEASE_OFFICER})
        original = self.releases.get(correction.original_release_id)
        if original is None:
            raise StateError("原放行结论不存在")
        if user_id in self._production_actors[correction.lot_id]:
            raise AuthorizationError("替代放行人不得是该批次的生产提交人")
        if correction.lot_id != original.lot_id:
            raise StateError("纠正批次必须一致")
        new_inspection = self.inspections.get(correction.new_inspection_id)
        if new_inspection is None or new_inspection.lot_id != correction.lot_id:
            raise StateError("替代依据检验不存在")
        if any(c.original_release_id == correction.original_release_id for c in self.corrections):
            raise StateError("该放行已有替代版本")
        self.corrections.append(correction)
        # 同步登记一条放行记录，保留纠正链。
        self.releases["release-of-" + correction.correction_id] = Release(
            release_id="release-of-" + correction.correction_id,
            lot_id=correction.lot_id,
            at=correction.at,
            officer_user_id=correction.officer_user_id,
            inspection_id=correction.new_inspection_id,
            released=correction.released,
            remark=correction.reason,
            corrected_by=None,
        )
        return correction

    def release_status(self, lot_id: str) -> dict:
        release_id = self._lot_release.get(lot_id)
        if release_id is None:
            return {"released": False}
        original = self.releases[release_id]
        chain = [c for c in self.corrections if c.original_release_id == release_id]
        return {"released": True, "original": original, "corrections": chain}

    # ---------------------------------------------------------------- 追溯

    def trace_lot(self, lot_id: str) -> dict:
        """返回批次完整谱系：地块、亲本、事件链、检验与放行。"""
        lot = self.lots[lot_id]
        lineage = self._lineage_events(lot_id)
        plots = sorted(
            {
                p
                for e in lineage
                for lid in (dict(e.inputs) | dict(e.outputs))
                for p in self.lots[lid].plot_ids
            }
        )
        related_lots = {
            lid for e in lineage for lid in (dict(e.inputs) | dict(e.outputs))
        }
        return {
            "lot_id": lot_id,
            "variety_code": lot.variety_code,
            "parent_male_batch": lot.parent_male_batch,
            "parent_female_batch": lot.parent_female_batch,
            "steward_id": lot.steward_id,
            "contract_id": lot.contract_id,
            "contract_version": lot.contract_version,
            "plot_ids": plots,
            "zones": sorted({self.plots[p].zone_id for p in plots}),
            "counties": sorted({self.plots[p].county_id for p in plots}),
            "events": [
                {
                    "event_id": e.event_id,
                    "type": e.type.value,
                    "at": e.at,
                    "inputs": sorted((l, str(w)) for l, w in e.inputs),
                    "outputs": sorted((l, str(w)) for l, w in e.outputs),
                    "loss": str(e.loss),
                }
                for e in lineage
            ],
            "inspections": [
                {
                    "inspection_id": i.inspection_id,
                    "at": i.at,
                    "passed": i.passed,
                    "review_of": i.review_of,
                    "items": dict(i.items),
                }
                for i in self.inspections.values()
                if i.lot_id in related_lots
            ],
            "release": self.release_status(lot_id),
            "suspension": self._active_suspensions.get(lot_id),
            "balance": str(self._balance[lot_id]),
        }

    def _lineage_events(self, lot_id: str) -> list[MaterialEvent]:
        """沿事件图向上追溯到收获，并返回谱系上所有批次的全部相关事件。

        包括祖先批次上的抽样、加工等事件（重量虽未流入后代，仍是其履历）。
        """
        related: set[str] = {lot_id}
        stack = [lot_id]
        while stack:
            current = stack.pop()
            for e in self.events:
                if current not in dict(e.outputs):
                    continue
                for parent in dict(e.inputs):
                    if parent not in related:
                        related.add(parent)
                        stack.append(parent)
        chosen = [
            e
            for e in self.events
            if (dict(e.inputs).keys() | dict(e.outputs).keys()) & related
        ]
        chosen.sort(key=lambda e: (e.at, e.event_id))
        return chosen

    def trace_package(self, package_code: str) -> dict:
        """抽查人员扫码：看到成品袋来自哪些地块、亲本，历经哪些检验与放行。"""
        pkg = self.packages.get(package_code)
        if pkg is None:
            raise StateError("成品袋不存在")
        trace = self.trace_lot(pkg.lot_id)
        trace["package_code"] = package_code
        trace["package_weight"] = str(pkg.weight)
        return trace

    # ------------------------------------------------------------ 可见性

    def enterprise_lots(self, user_id: str) -> list[str]:
        """企业只能看到本企业合同下的批次，看不到竞争者明细。"""
        user = self._require_role(user_id, {Role.ENTERPRISE})
        visible: list[str] = []
        for lot_id, lot in self.lots.items():
            versions = self.contracts.get(lot.contract_id, {})
            if not versions:
                continue
            contract = versions[max(versions)]
            if contract.enterprise_id == user.enterprise_id:
                visible.append(lot_id)
        return visible

    def assert_can_read_lot(self, user_id: str, lot_id: str) -> None:
        user = self._user(user_id)
        if user.role in {Role.SECRETARIAT, Role.SAMPLER, Role.INSPECTOR,
                         Role.RELEASE_OFFICER, Role.COUNTY}:
            return
        if lot_id not in self.enterprise_lots(user_id):
            raise AuthorizationError("不能查看其他企业的批次明细")

    # ---------------------------------------------------------------- 校核

    def total_weight_check(self) -> None:
        """全库守恒自检：收获净产出 == 在库 + 抽样/损耗 + 成品袋重量。"""
        harvested = sum(
            (sum(dict(e.outputs).values(), ZERO) for e in self.events
             if e.type is EventType.HARVEST),
            ZERO,
        )
        on_hand = sum(self._balance.values(), ZERO)
        packed = sum((p.weight for p in self.packages.values()), ZERO)
        lost = sum((e.loss for e in self.events), ZERO)
        if harvested != on_hand + packed + lost:
            raise ConservationError(
                f"台账不平衡：收获 {harvested} != 在库 {on_hand} + 成品 {packed} + 损耗 {lost}"
            )
