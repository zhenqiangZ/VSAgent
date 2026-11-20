"""协议层：意图、任务、角色计划、Agent 任务与报告。

这一层只定义**形状**，不执行任何动作，也不依赖运行期模块。下游按它填数据，
上游按它做校验；改形状要同步升 `SCHEMA_VERSION`。

    IntentSpec   用户想做什么、与当前任务什么关系
    TaskSpec     这次任务交付什么、怎么验收
    AgentPlan    谁来做、谁等谁
    AgentTask    派给一个专业 Agent 的一份任务
    AgentReport  它交回来的结构化结果
"""

from vsagent.core.agent.roles.contracts.agent import AGENT_TASK_STATUSES, REPORT_STATUSES, AgentReport, AgentTask
from vsagent.core.agent.roles.contracts.base import SCHEMA_VERSION, ContractError
from vsagent.core.agent.roles.contracts.intent import (
    CONVERSATION_ACTIONS,
    OBJECT_ORIGINS,
    OBJECT_TYPES,
    OPERATIONS,
    TASK_RELATIONS,
    IntentSpec,
    ObjectRef,
)
from vsagent.core.agent.roles.contracts.plan import AgentPlan
from vsagent.core.agent.roles.contracts.task import ACCEPTANCE_KINDS, TASK_STATUSES, TaskSpec

__all__ = [
    "ACCEPTANCE_KINDS",
    "AGENT_TASK_STATUSES",
    "AgentPlan",
    "AgentReport",
    "AgentTask",
    "CONVERSATION_ACTIONS",
    "ContractError",
    "IntentSpec",
    "OBJECT_ORIGINS",
    "OBJECT_TYPES",
    "OPERATIONS",
    "ObjectRef",
    "REPORT_STATUSES",
    "SCHEMA_VERSION",
    "TASK_RELATIONS",
    "TASK_STATUSES",
    "TaskSpec",
]
