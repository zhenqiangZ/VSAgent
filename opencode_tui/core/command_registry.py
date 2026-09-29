from __future__ import annotations

import difflib
from dataclasses import dataclass, field

# keybind 为 None 表示该命令只能通过命令面板触发。
# connect 与 compact 在原规格里都写成了 ctrl+x c，这里把 connect 改到 ctrl+x o，避免前导键冲突。
COMMANDS: dict[str, dict[str, object]] = {
    "compact": {"keybind": "ctrl+x c", "desc": "压缩当前会话", "aliases": ["summarize"]},
    "connect": {"keybind": "ctrl+x o", "desc": "添加提供商", "aliases": []},
    "details": {"keybind": "ctrl+x d", "desc": "切换工具执行详情", "aliases": []},
    "editor": {"keybind": "ctrl+x e", "desc": "打开外部编辑器", "aliases": []},
    "effort": {"keybind": None, "desc": "轮换推理强度", "aliases": []},
    "exit": {"keybind": "ctrl+x q", "desc": "退出", "aliases": ["quit", "q"]},
    "export": {"keybind": "ctrl+x x", "desc": "导出对话为 Markdown", "aliases": []},
    "help": {"keybind": "ctrl+x h", "desc": "显示帮助", "aliases": []},
    "init": {"keybind": "ctrl+x i", "desc": "创建或更新 AGENTS.md", "aliases": []},
    "models": {"keybind": "ctrl+x m", "desc": "列出可用模型", "aliases": []},
    "new": {"keybind": "ctrl+x n", "desc": "新会话", "aliases": ["clear"]},
    "redo": {"keybind": "ctrl+x r", "desc": "重做撤销的消息", "aliases": []},
    "sessions": {"keybind": "ctrl+x l", "desc": "列出会话", "aliases": ["resume", "continue"]},
    "share": {"keybind": "ctrl+x s", "desc": "分享会话", "aliases": []},
    "sidebar": {"keybind": "ctrl+x b", "desc": "切换上下文侧栏", "aliases": []},
    "themes": {"keybind": "ctrl+x t", "desc": "列出主题", "aliases": []},
    "thinking": {"keybind": None, "desc": "切换思考块可见性", "aliases": []},
    "undo": {"keybind": "ctrl+x u", "desc": "撤销最后一条消息", "aliases": []},
    "unshare": {"keybind": None, "desc": "取消分享", "aliases": []},
}


@dataclass
class Command:
    """一条斜杠命令。"""

    name: str
    desc: str
    keybind: str | None = None
    aliases: list[str] = field(default_factory=list)

    def display(self) -> str:
        """命令面板中的展示文本。"""
        keybind = self.keybind or "-"
        return f"/{self.name}  [{keybind}]  {self.desc}"

    def search_text(self) -> str:
        """参与模糊匹配的文本。"""
        return " ".join([self.name, self.desc, *self.aliases])


class CommandRegistry:
    """命令注册表，负责命令查询与模糊匹配。"""

    def __init__(self, commands: dict[str, dict[str, object]] | None = None) -> None:
        raw = commands if commands is not None else COMMANDS
        self._commands: dict[str, Command] = {}
        for name, meta in raw.items():
            self._commands[name] = Command(
                name=name,
                desc=str(meta.get("desc", "")),
                keybind=meta.get("keybind") or None,  # type: ignore[arg-type]
                aliases=list(meta.get("aliases") or []),  # type: ignore[arg-type]
            )

    def all(self) -> list[Command]:
        """按命令名排序返回全部命令。"""
        return [self._commands[name] for name in sorted(self._commands)]

    def get(self, name: str) -> Command | None:
        """按名字或别名取命令。"""
        cleaned = name.lstrip("/").strip()
        if cleaned in self._commands:
            return self._commands[cleaned]
        for command in self._commands.values():
            if cleaned in command.aliases:
                return command
        return None

    def by_keybind(self, keybind: str) -> Command | None:
        """按完整 keybind 字符串反查命令，例如 ctrl+x n。"""
        for command in self._commands.values():
            if command.keybind == keybind:
                return command
        return None

    def by_leader_key(self, second_key: str) -> Command | None:
        """按前导键之后的第二击反查命令，例如 n。"""
        return self.by_keybind(f"ctrl+x {second_key}")

    def leader_keys(self) -> dict[str, Command]:
        """返回第二击到命令的映射。"""
        mapping: dict[str, Command] = {}
        for command in self._commands.values():
            if command.keybind and command.keybind.startswith("ctrl+x "):
                mapping[command.keybind.split(" ", 1)[1]] = command
        return mapping

    def fuzzy_match(self, query: str, limit: int = 12) -> list[Command]:
        """对命令做模糊匹配，返回按相关度降序的结果。"""
        cleaned = query.lstrip("/").strip().lower()
        if not cleaned:
            return self.all()[:limit]
        scored: list[tuple[float, Command]] = []
        for command in self.all():
            name = command.name.lower()
            ratio = difflib.SequenceMatcher(None, cleaned, name).ratio()
            if cleaned in name:
                ratio += 0.5
            if name.startswith(cleaned):
                ratio += 0.3
            haystack = command.search_text().lower()
            ratio = max(ratio, difflib.SequenceMatcher(None, cleaned, haystack).ratio() * 0.8)
            scored.append((ratio, command))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [command for score, command in scored[:limit] if score > 0.2]
