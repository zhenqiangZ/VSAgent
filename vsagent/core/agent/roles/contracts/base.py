"""协议层的共用部分：版本号、错误类型与几个校验函数。

这一层**不依赖运行期**：不 import `models.py`、`AgentLoop` 或任何领域工具。
协议先于实现固定下来，下游模块照着它填数据；反过来让它依赖运行期，就会出现
"改一个工具顺带改协议"的连锁。

校验一律抛 `ContractError`，并且**报出字段路径**。协议错误最常见的形态是
"某条记录少一个字段"，只报"校验失败"会让人回头逐字段比对。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 7
"""当前协议版本。

2：`IntentSpec` 增加 `turn_mode`（Phase 3，轮次类型并进同一次解析）。
3：`AgentReport` 增加 `evidence`（子会话交回的证据对象），`evidence_refs` 保留。
4：`AgentReport` 增加 `evidence` 之外的结构化字段，`AgentTask` 增加权限。
5：意图改成五维字段与两轴；任务子目标带 `agent` 与 `stage`；能力计划改成角色计划。
6：`TaskSpec` 的交付物、确认点、证据要求从对象数组改成字符串数组，与 `AgentTask`
   同名同类型。
7：`AgentTask` 增加 `background`：父任务目标只作背景，不再混进本角色的任务目标。
旧数据没有这个字段，读取时按空列表处理——升版本是为了让"读到更新版本的
数据"能被拒绝，不是为了让旧数据读不进来。第 6 版例外：旧的 `task_spec` 形状
对不上，`Session.from_dict` 会直接丢弃它，不写迁移分支。"""


class ContractError(ValueError):
    """协议校验失败。消息里带字段路径，调用方可以直接展示。"""


def check_version(value: Any, *, name: str) -> int:
    """读 `schema_version`。缺省按 1 处理（第一版协议没有这个字段）。"""
    if value is None:
        return SCHEMA_VERSION
    try:
        version = int(value)
    except (TypeError, ValueError) as error:
        raise ContractError(f"{name}.schema_version 应当是整数，收到 {value!r}") from error
    if version > SCHEMA_VERSION:
        raise ContractError(
            f"{name}.schema_version={version} 比程序支持的 {SCHEMA_VERSION} 新："
            "这份数据可能来自更新的版本，不要按旧字段解释它"
        )
    return version


def reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], *, name: str) -> None:
    """拒绝不认识的字段。

    宁可让写入方改字段名，也不要静默丢弃：静默丢弃会让"写进去了但没生效"
    看起来像下游 bug，而这条数据其实根本没被保存。
    """
    known = set(allowed)
    unknown = sorted(key for key in data if key not in known)
    if unknown:
        raise ContractError(f"{name} 里有不认识的字段：{'、'.join(unknown)}")


def text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    """取一段文本；缺省或类型不对都算错，除非明确允许空。"""
    if value is None:
        if allow_empty:
            return ""
        raise ContractError(f"{field} 不能为空")
    if not isinstance(value, str):
        raise ContractError(f"{field} 应当是字符串，收到 {type(value).__name__}")
    result = value.strip()
    if not result and not allow_empty:
        raise ContractError(f"{field} 不能是空白")
    return result


def texts(value: Any, field: str) -> tuple[str, ...]:
    """取一组文本：去空白、去重、保持顺序。字符串会被当成单个元素。"""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{field} 应当是字符串数组，收到 {type(value).__name__}")
    out: list[str] = []
    for item in value:
        word = text(item, field)
        if word not in out:
            out.append(word)
    return tuple(out)


def mapping(value: Any, field: str) -> dict[str, Any]:
    """取一个对象。键必须是字符串。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} 应当是一个对象，收到 {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise ContractError(f"{field} 的键应当是字符串，收到 {key!r}")
    return dict(value)


def records(value: Any, field: str) -> list[dict[str, Any]]:
    """取一组对象，每一项都必须是对象。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{field} 应当是数组，收到 {type(value).__name__}")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ContractError(f"{field}[{index}] 应当是对象，收到 {type(item).__name__}")
        out.append(dict(item))
    return out


def number(value: Any, field: str, *, low: float | None = None, high: float | None = None) -> float:
    """取一个数，并检查范围。布尔值不算数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} 应当是数字，收到 {type(value).__name__}")
    result = float(value)
    if low is not None and result < low:
        raise ContractError(f"{field}={result} 小于下限 {low}")
    if high is not None and result > high:
        raise ContractError(f"{field}={result} 大于上限 {high}")
    return result


def choice(value: Any, allowed: Iterable[str], field: str) -> str:
    """取一个枚举值。错误里列出可用取值。"""
    options = tuple(allowed)
    word = text(value, field)
    if word not in options:
        raise ContractError(f"{field} 只能是 {'、'.join(options)}，收到 {word!r}")
    return word
