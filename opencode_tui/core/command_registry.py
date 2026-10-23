from __future__ import annotations

import difflib
from dataclasses import dataclass, field

# 命令只有一种入口：在输入框里打 /命令，或者 ctrl+p 打开命令面板再把名字敲进去。
# 键位不挂在命令上：快捷键那一层由 `KeybindManager` 单独维护，两边各管各的。
# 这么分是因为成熟的做法就是这样——快捷键只留最常按的那几个，其余全靠 /命令 记。
COMMANDS: dict[str, dict[str, object]] = {
    "agent": {"desc": "派发子 agent", "aliases": ["spawn"]},
    "agents": {"desc": "打开子 agent 列表", "aliases": []},
    "back": {"desc": "从子 agent 返回", "aliases": []},
    "compact": {"desc": "压缩当前会话", "aliases": ["summarize"]},
    "connect": {"desc": "添加提供商", "aliases": []},
    "delete": {"desc": "删除当前会话", "aliases": ["remove"]},
    "details": {"desc": "切换工具执行详情", "aliases": []},
    "editor": {"desc": "打开外部编辑器", "aliases": []},
    "effort": {"desc": "轮换推理强度", "aliases": []},
    "exit": {"desc": "退出", "aliases": ["quit", "q"]},
    "export": {"desc": "导出对话为 Markdown", "aliases": []},
    "forget": {"desc": "删除一条记忆", "aliases": []},
    "help": {"desc": "显示帮助", "aliases": []},
    "init": {"desc": "创建或更新 AGENTS.md", "aliases": []},
    "log": {"desc": "显示事件日志路径", "aliases": []},
    "memory": {"desc": "列出项目记忆", "aliases": []},
    "model": {"desc": "配置模型接口", "aliases": []},
    "new": {"desc": "新会话", "aliases": ["clear"]},
    "redo": {"desc": "重做撤销的消息", "aliases": []},
    "remember": {"desc": "记住一条项目事实", "aliases": []},
    "rename": {"desc": "重命名当前会话", "aliases": ["title"]},
    "sessions": {"desc": "列出会话", "aliases": ["resume", "continue"]},
    "share": {"desc": "分享会话", "aliases": []},
    "sidebar": {"desc": "切换上下文侧栏", "aliases": []},
    "task": {"desc": "查看或更新任务状态", "aliases": []},
    "themes": {"desc": "列出主题", "aliases": []},
    "thinking": {"desc": "切换思考块可见性", "aliases": []},
    "tools": {"desc": "管理工具开关", "aliases": []},
    "undo": {"desc": "撤销最后一条消息", "aliases": []},
    "unshare": {"desc": "取消分享", "aliases": []},
}


@dataclass
class Command:
    """一条斜杠命令。"""

    name: str
    desc: str
    aliases: list[str] = field(default_factory=list)

    def display(self) -> str:
        """命令面板与补全弹窗中的展示文本。

        名字那一段定宽：名字长短差一半（`/exit` 到 `/thinking`），只留两个空格的话
        说明列会参差不齐，二十条排下来看不出哪条对哪条。
        """
        return f"/{self.name:<9}{self.desc}"

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
                aliases=list(meta.get("aliases") or []),  # type: ignore[arg-type]
            )

    def all(self) -> list[Command]:
        """按命令名排序返回全部命令。"""
        return [self._commands[name] for name in sorted(self._commands)]

    def filtered(self, allowed: frozenset[str] | set[str]) -> "CommandRegistry":
        """只保留白名单里的命令，返回一个新的注册表。

        子 agent 界面用它：`/agent`、`/new`、`/sessions` 这些会改动父会话
        或再往下派发的命令根本不该出现在面板里，而不是选了之后才被拒。
        """
        subset = {
            name: {"desc": command.desc, "aliases": list(command.aliases)}
            for name, command in self._commands.items()
            if name in allowed
        }
        return CommandRegistry(subset)

    def get(self, name: str) -> Command | None:
        """按名字或别名取命令。"""
        cleaned = name.lstrip("/").strip()
        if cleaned in self._commands:
            return self._commands[cleaned]
        for command in self._commands.values():
            if cleaned in command.aliases:
                return command
        return None

    def is_exact(self, name: str) -> bool:
        """这一段输入已经是某条命令的完整名字或完整别名。

        补全弹窗靠这个判断"还有没有补全的必要"：名字打全之后弹窗该收起来，
        否则回车会被弹窗截走变成"采纳候选"，命令反而执行不了。别名同样算打全——
        `/clear` 就是 `/new`，打全了按回车就该去执行，不该被改写成 `/new` 停在那里。
        """
        cleaned = name.lstrip("/").strip()
        command = self.get(cleaned)
        if command is None:
            return False
        return command.name == cleaned or cleaned in command.aliases

    def complete(self, query: str) -> list[Command]:
        """命令补全用的候选：名字前缀 > 名字里含 > 别名或说明里含，同档内按名字排。

        补全与命令面板要的不是同一种排序。面板里敲的是"大概是这么个意思"，模糊匹配
        能把说明里带那个词的命令一起捞出来；补全里敲的是命令名的开头几个字母，
        打 hel 却列出十几条（模糊匹配的实测结果）只会把人挡在外面。
        """
        cleaned = query.lstrip("/").strip().lower()
        if not cleaned:
            return self.all()
        prefix: list[Command] = []
        inside: list[Command] = []
        loose: list[Command] = []
        for command in self.all():
            name = command.name.lower()
            if name.startswith(cleaned):
                prefix.append(command)
            elif cleaned in name:
                inside.append(command)
            elif any(cleaned in alias for alias in command.aliases) or cleaned in command.desc.lower():
                loose.append(command)
        return prefix + inside + loose

    def fuzzy_match(self, query: str, limit: int = 12) -> list[Command]:
        """对命令做模糊匹配，返回按相关度降序的结果。

        limit 默认 12 是命令面板那一屏放得下的条数；补全弹窗要列出全部命令时
        自己传一个更大的上限进来。
        """
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
