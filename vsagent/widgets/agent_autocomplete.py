from __future__ import annotations

from .file_autocomplete import AutocompletePopup

AGENTS = ["Build", "Plan"]


class AgentAutocomplete(AutocompletePopup):
    """Agent 补全弹窗，按 tab 触发。"""

    def __init__(self, agents: list[str] | None = None, **kwargs: object) -> None:
        self._agents = list(agents or AGENTS)
        super().__init__(id="agent-autocomplete", title="切换 Agent", **kwargs)

    def open_list(self) -> None:
        """打开弹窗并列出全部 Agent。"""
        self.set_items(self._agents)

    @property
    def agents(self) -> list[str]:
        """可用的 Agent 名称。"""
        return list(self._agents)
