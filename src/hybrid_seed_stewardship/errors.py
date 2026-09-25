"""制种受托与质量追溯服务的领域异常。"""


class DomainError(Exception):
    """所有业务规则冲突的基类。"""


class ValidationError(DomainError):
    """实体内容不完整或取值非法。"""


class ContractError(DomainError):
    """合同不存在或已过期，或变更绕过版本机制。"""


class EligibilityError(DomainError):
    """受托人等级不满足品种规程。"""


class IsolationError(DomainError):
    """隔离距离或播插窗口（花期）冲突。"""


class LedgerError(DomainError):
    """谱系断裂、重量不守恒或数量被重复占用。"""


class InspectionError(DomainError):
    """检验数据非法或样品谱系不可追踪。"""


class ReleaseError(DomainError):
    """放行条件不满足，或放行人与提交人不独立。"""


class SealedVersionError(DomainError):
    """已发布结论不可修改，只能以替代版本纠正。"""


class QuotationError(DomainError):
    """报价不可见、重复或未通过门槛。"""
