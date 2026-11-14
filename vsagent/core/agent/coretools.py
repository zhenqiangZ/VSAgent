"""常驻核心工具的那一份装配清单。

**顺序就是请求体里的顺序**，模型按它挑工具，所以这里只排一次、由应用与判据
共同读取，不各写一份。加一个常驻工具只改这里与 `toolprofiles.py`。

与领域工具的分界：这些工具一步都不缺席；`domain/` 下的工具由 `FAMILIES`
分组、按族装入（见 `toolset.py`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .domain.campaign import AssessCampaignTool
from .domain.export import OpenViewerTool, WriteRunTool
from .domain.survey import SurveyStructuresTool
from .event_log import domain_tools as event_tools
from .history import SearchHistoryTool
from .hypothesis import HypothesisTool
from .memory import RecallTool, RememberTool
from .spawn import AgentStatusTool, DispatchCampaignTool
from .task import TaskTool
from .tools import BashTool, GrepTool, PatchTool, ReadTool, ReadTurnTool, Tool, WriteTool
from .toolset import LoadToolsTool


def build_core_tools(
    *,
    root: Path,
    memory: Any,
    sessions: Any,
    bash_timeout: int,
    allow_agents: bool = True,
) -> list[Tool]:
    """按装配顺序返回全部常驻工具。

    `allow_agents` 只决定编排那一对在不在：子 agent 的注册表里没有
    `dispatch_campaign` 与 `agent_status`，模型连名字都看不到。
    其余工具两者的清单相同，差异由档位在装配处过滤。
    """
    tools: list[Tool] = [
        BashTool(cwd=root, timeout=bash_timeout),
        ReadTool(cwd=root),
        WriteTool(cwd=root),
        PatchTool(cwd=root),
        GrepTool(cwd=root),
        RememberTool(memory),
        RecallTool(memory),
        # 任务状态与假设都挂在会话上，由内核每次调用现取，不需要传东西进来。
        TaskTool(),
        HypothesisTool(),
        # 开工前的盘点与结构全景：常驻、不登记族。它决定这一轮装哪几族，
        # 一个「决定装哪族」的工具本身要先装族才看得见，顺序就反了。
        AssessCampaignTool(),
        SurveyStructuresTool(),
        ReadTurnTool(),
        SearchHistoryTool(sessions),
    ]
    tools.extend(event_tools(sessions))
    tools.extend([LoadToolsTool(), WriteRunTool(), OpenViewerTool()])
    if allow_agents:
        tools.extend([AgentStatusTool(), DispatchCampaignTool()])
    return tools
