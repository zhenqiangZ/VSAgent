"""协议层：意图、任务、能力计划、Agent 任务与报告。

这一层只定义**形状**，不执行任何动作，也不依赖运行期模块。下游按它填数据，
上游按它做校验；改形状要同步升 `SCHEMA_VERSION`。

    IntentSpec     用户想做什么
    TaskSpec       这次任务交付什么、怎么验收
    CapabilityPlan 谁来做、用什么、谁等谁
    AgentTask      派给一个专业 Agent 的一份任务
    AgentReport    它交回来的结构化结果
"""

from .agent import AGENT_TASK_STATUSES, REPORT_STATUSES, AgentReport, AgentTask
from .base import SCHEMA_VERSION, ContractError
from .capability import CapabilityPlan
from .intent import INTENT_TYPES, TURN_MODES, IntentSpec
from .task import ACCEPTANCE_KINDS, TASK_STATUSES, TaskSpec

__all__ = [
    "ACCEPTANCE_KINDS",
    "AGENT_TASK_STATUSES",
    "AgentReport",
    "AgentTask",
    "CapabilityPlan",
    "ContractError",
    "INTENT_TYPES",
    "IntentSpec",
    "REPORT_STATUSES",
    "SCHEMA_VERSION",
    "TASK_STATUSES",
    "TURN_MODES",
    "TaskSpec",
]
