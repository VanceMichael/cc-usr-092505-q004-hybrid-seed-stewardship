"""匿名价格指数编制。

报价先逐道过门槛（品种权利有效、品种与备案一致、数量下限、价格区间、
同期同企业同品种唯一），全部通过才进入当期指数；指数发布结果只含匿名
价格，不携带企业身份。每期还必须达到最少报价家数才发布。每条报价的
进入/未进入结论都可被逐条追问。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .model import (
    GateFailure,
    PublishedIndex,
    QuoteDecision,
    Quotation,
    VarietyFiling,
)
from .service import AuthorizationError, DomainError, StateError


@dataclass(frozen=True)
class IndexRules:
    """某品种当期指数的编制门槛。"""

    variety_code: str
    period_end: str               # 品种权有效期至少覆盖到该日
    min_quantity_kg: Decimal      # 最小报价数量
    price_floor: Decimal          # 价格区间下限（元/千克）
    price_ceiling: Decimal       # 价格区间上限（元/千克）
    min_enterprises: int = 3      # 发布所需最少不同企业数

    def evaluate(
        self,
        quote: Quotation,
        filings: Mapping[str, Mapping[int, VarietyFiling]],
    ) -> list[GateFailure]:
        failures: list[GateFailure] = []

        if quote.variety_code != self.variety_code:
            failures.append(GateFailure("variety", "报价品种与指数品种不符"))

        versions = filings.get(quote.filing_id)
        filing = versions.get(quote.filing_version) if versions else None
        if filing is None:
            failures.append(GateFailure("filing", "报价引用的备案版本不存在"))
        else:
            if filing.variety_code != quote.variety_code:
                failures.append(GateFailure("variety", "报价品种与备案品种不一致"))
            if filing.enterprise_id != quote.enterprise_id:
                failures.append(GateFailure("filing", "报价企业与备案企业不一致"))
            if filing.rights_valid_to < self.period_end:
                failures.append(
                    GateFailure(
                        "variety_rights",
                        f"品种权在 {filing.rights_valid_to} 到期，未覆盖期末 {self.period_end}",
                    )
                )

        if quote.quantity < self.min_quantity_kg:
            failures.append(
                GateFailure(
                    "min_quantity",
                    f"报价数量 {quote.quantity} 低于下限 {self.min_quantity_kg}",
                )
            )
        if not (self.price_floor <= quote.price_per_kg <= self.price_ceiling):
            failures.append(
                GateFailure(
                    "price_band",
                    f"报价 {quote.price_per_kg} 超出允许区间"
                    f"[{self.price_floor}, {self.price_ceiling}]",
                )
            )
        return failures


class IndexOffice:
    """联盟秘书处使用的指数编制办公室。"""

    def __init__(self, rules_by_period: Mapping[tuple[str, str], IndexRules]) -> None:
        self._rules = dict(rules_by_period)
        self._quotes: dict[str, list[Quotation]] = {}
        self.published: dict[tuple[str, str], PublishedIndex] = {}

    # ------------------------------------------------------------ 报价收报

    def submit_quote(self, acting_role: str, quote: Quotation) -> None:
        """企业（或秘书处代录）提交报价；秘书处之外的角色不得代收。"""
        if acting_role not in {"enterprise", "secretariat"}:
            raise AuthorizationError("只有企业或秘书处可以提交报价")
        rule_key = (quote.period, quote.variety_code)
        if rule_key not in self._rules:
            raise StateError("该期该品种未开放指数编制")
        self._quotes.setdefault(quote.period, []).append(quote)

    # ------------------------------------------------------------ 门槛评估

    def _decide(
        self,
        period: str,
        variety_code: str,
        filings: Mapping[str, Mapping[int, VarietyFiling]],
    ) -> tuple[IndexRules, list[Quotation], dict[str, QuoteDecision], list[Quotation]]:
        rules = self._rules[(period, variety_code)]
        quotes = [
            q
            for q in self._quotes.get(period, [])
            if q.variety_code == variety_code
        ]
        # 同企业同品种同期：按提交时间保留最新一条参与门槛，其余直接落选。
        latest: dict[tuple[str, str], Quotation] = {}
        for q in sorted(quotes, key=lambda q: q.submitted_at):
            latest[(q.enterprise_id, q.variety_code)] = q

        decisions: dict[str, QuoteDecision] = {}
        accepted: list[Quotation] = []
        for q in quotes:
            failures = list(rules.evaluate(q, filings))
            if q is not latest.get((q.enterprise_id, q.variety_code)):
                failures.append(
                    GateFailure("duplicate", "同期同企业同品种已有更新报价，本条被覆盖")
                )
            decisions[q.quote_id] = QuoteDecision(
                quote_id=q.quote_id, accepted=not failures, failures=failures
            )
            if not failures:
                accepted.append(q)
        return rules, quotes, decisions, accepted

    # ------------------------------------------------------------ 发布指数

    def publish(
        self,
        acting_role: str,
        period: str,
        variety_code: str,
        published_at: str,
        filings: Mapping[str, Mapping[int, VarietyFiling]],
    ) -> PublishedIndex:
        if acting_role != "secretariat":
            raise AuthorizationError("只有联盟秘书处可以发布指数")
        if (period, variety_code) in self.published:
            raise StateError("该期指数已发布")
        rules, _quotes, decisions, accepted = self._decide(
            period, variety_code, filings
        )
        distinct = {q.enterprise_id for q in accepted}
        if len(distinct) < rules.min_enterprises:
            raise DomainError(
                f"通过门槛报价来自 {len(distinct)} 家企业，"
                f"少于发布下限 {rules.min_enterprises}，本期不予发布"
            )
        prices = tuple(sorted(q.price_per_kg for q in accepted))
        total = sum(prices, Decimal("0"))
        mean = (total / Decimal(len(prices))).quantize(Decimal("0.01"))
        index = PublishedIndex(
            period=period,
            variety_code=variety_code,
            published_at=published_at,
            anonymized_prices=prices,
            sample_size=len(accepted),
            mean=mean,
            quote_decisions=decisions,
        )
        self.published[(period, variety_code)] = index
        return index

    # ------------------------------------------------------------ 门槛追问

    def decision_of(self, period: str, variety_code: str, quote_id: str) -> QuoteDecision:
        """追问某条报价进入或未进入当期指数的具体门槛结论。"""
        published = self.published.get((period, variety_code))
        if published is None:
            raise StateError("该期指数尚未发布")
        try:
            return published.quote_decisions[quote_id]
        except KeyError:
            raise StateError("该报价不属于本期") from None

    def published_view(self, period: str, variety_code: str) -> PublishedIndex:
        """对外视图：只有匿名价格与样本量，无法回溯到企业。"""
        published = self.published.get((period, variety_code))
        if published is None:
            raise StateError("该期指数尚未发布")
        return PublishedIndex(
            period=published.period,
            variety_code=published.variety_code,
            published_at=published.published_at,
            anonymized_prices=published.anonymized_prices,
            sample_size=published.sample_size,
            mean=published.mean,
            quote_decisions={},  # 公开视图不暴露任何报价明细
        )
