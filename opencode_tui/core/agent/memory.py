from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .ledger import LedgerError, fingerprint_path, parse_handle
from .prompt import estimate_tokens
from .tools import Tool, ToolError, ToolResult, ToolSpec

# 项目级记忆目录名，挂在项目根下。
MEMORY_DIR_NAME = ".vsmemory"
# 常驻注入的 token 上限。超限时先砍旧条目，只保留装得下的那几条。
RESIDENT_LIMIT = 2000
# 快照格式版本。
SNAPSHOT_VERSION = 1

# 换名在目标文件正被别的进程打开时会被系统拒绝，隔几毫秒重试。
REPLACE_ATTEMPTS = 40
REPLACE_RETRY_DELAY = 0.005

# 条目引用的文件状态。与登记表用同一套说法，读的人不必分辨这是哪一处的状态。
FILE_STATE_NOTES: dict[str, str] = {
    "ok": "文件在，内容与登记时一致",
    "stale": "文件在，但内容与登记时不同",
    "missing": "引用的文件不在了",
}


class MemoryKind(Enum):
    """记忆的类型。

    本期只有 FACT。打分与具体筛选流程尚未实现，依赖它们的
    PREFERENCE / RUN / RECIPE 等到那一部分做完之后再扩这个枚举。
    """

    FACT = "fact"


class MemoryError(Exception):
    """记忆自身的错误，调用方只需要处理这一种。"""


def memory_root(workspace_root: str | Path) -> Path:
    """项目级记忆目录。"""
    return Path(workspace_root) / MEMORY_DIR_NAME


def new_memory_id(text: str = "") -> str:
    """按时间与内容生成一个短 id。

    不引 uuid：id 要能一眼看出是什么时候写的，出问题时按时间就能对上。
    """
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    tail = hashlib.sha256(
        f"{text}{time.monotonic_ns()}".encode("utf-8")
    ).hexdigest()[:6]
    return f"mem_{stamp}_{tail}"


def _reject_handle(ref: str) -> None:
    """引用里不许出现 handle。

    `receptor#1` 这类编号只在本次会话里有效，换个会话就指向别的东西。
    写进去不会当场报错，只会在下一个会话里用错对象，所以在这里拦住。
    判定的口径直接借登记表的 `parse_handle`：能解析成 handle 的就是 handle。
    """
    try:
        parse_handle(ref)
    except LedgerError:
        return
    raise MemoryError(
        f"引用要写文件路径，不写 handle：{ref!r} 是会话内的编号，换个会话就指向别的东西。"
    )


@dataclass
class MemoryEntry:
    """一条记忆。

    跨会话可用是它的全部意义，所以引用的必须是稳定标识：
    `refs` 里放路径（配 `fingerprint`），**不放会话内的 handle**——
    `receptor#1` 这种编号换个会话就指向别的东西。
    """

    id: str
    kind: MemoryKind = MemoryKind.FACT
    text: str = ""
    refs: list[str] = field(default_factory=list)
    fingerprint: str = ""
    source: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    state: str = "ok"

    def is_live(self, now: float | None = None) -> bool:
        """还没过期、且引用没有丢失时才算可用。

        `stale`（文件变了）仍然算可用：变化本身是个信号，不是作废的理由，
        注入时带一行提醒交给模型判断。`missing` 才算不可用。
        """
        stamp = time.time() if now is None else now
        if self.expires_at and self.expires_at <= stamp:
            return False
        return self.state != "missing"

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 JSON 的字典。"""
        return {
            "id": self.id,
            "kind": self.kind.value,
            "text": self.text,
            "refs": list(self.refs),
            "fingerprint": self.fingerprint,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        """从字典还原一条记忆。读不懂的字段回退到默认值。"""
        try:
            kind = MemoryKind(str(data.get("kind", MemoryKind.FACT.value)))
        except ValueError:
            kind = MemoryKind.FACT
        state = str(data.get("state", "ok"))
        if state not in FILE_STATE_NOTES:
            state = "ok"
        raw_refs = data.get("refs")
        refs = [str(item) for item in raw_refs] if isinstance(raw_refs, list) else []
        return cls(
            id=str(data.get("id") or new_memory_id()),
            kind=kind,
            text=str(data.get("text", "")),
            refs=refs,
            fingerprint=str(data.get("fingerprint", "")),
            source=str(data.get("source", "")),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            expires_at=float(data.get("expires_at") or 0.0),
            state=state,
        )


class MemoryStore:
    """项目级记忆的读写与检索。

    记忆是**跨会话**的知识台账，与登记表（会话内的工作集）分工不同：
    登记表记「这次会话手上有哪些产物」，记忆记「这个项目已知什么」。
    因此记忆里的引用只能是路径一类稳定标识，不能是会话内的 handle。
    """

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root)
        self.root = memory_root(self.workspace_root)
        self.entries: dict[str, MemoryEntry] = {}

    @property
    def entries_path(self) -> Path:
        """条目文件，程序读的那一份，是真相。"""
        return self.root / "entries.json"

    @property
    def memory_path(self) -> Path:
        """记忆正文，给人看、给人改的那一份。"""
        return self.root / "memory.md"

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def load(self) -> bool:
        """从磁盘读条目，文件不存在或读不出来时返回 False。"""
        target = self.entries_path
        if not target.is_file():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        self.load_snapshot(data)
        return True

    def load_snapshot(self, snapshot: dict[str, Any]) -> int:
        """从快照恢复，返回读进来几条。

        读不懂的条目跳过，不让一条坏记录废掉整份台账。
        """
        self.entries = {}
        restored = 0
        for raw in snapshot.get("entries") or []:
            try:
                entry = MemoryEntry.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if not entry.text:
                continue
            self.entries[entry.id] = entry
            restored += 1
        return restored

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def remember(
        self,
        text: str,
        refs: list[str] | None = None,
        *,
        source: str = "agent",
        expires_at: float = 0.0,
    ) -> MemoryEntry:
        """写入一条记忆并写盘，返回写进去的那一条。"""
        cleaned = text.strip()
        if not cleaned:
            raise MemoryError("记忆内容不能为空")
        cleaned_refs = [str(item) for item in (refs or [])]
        for ref in cleaned_refs:
            _reject_handle(ref)
        now = time.time()
        entry = MemoryEntry(
            id=new_memory_id(cleaned),
            kind=MemoryKind.FACT,
            text=cleaned,
            refs=cleaned_refs,
            fingerprint=self._fingerprint_for(cleaned_refs),
            source=source,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        self.entries[entry.id] = entry
        self.save()
        return entry

    def forget(self, entry_id: str) -> bool:
        """删掉一条记忆并写盘，返回是否删掉了。"""
        if entry_id not in self.entries:
            return False
        del self.entries[entry_id]
        self.save()
        return True

    def save(self) -> Path:
        """写盘：条目文件与记忆正文各写一次，返回条目文件路径。

        两份都用原子换名写：读的人要么拿到旧的、要么拿到新的，
        中间不会看到写了一半的内容。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self.entries_path,
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2),
        )
        self._atomic_write(self.memory_path, self.render_markdown())
        return self.entries_path

    def as_dict(self) -> dict[str, Any]:
        """整份快照。"""
        return {
            "version": SNAPSHOT_VERSION,
            "entries": [self.entries[key].to_dict() for key in sorted(self.entries)],
        }

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def recall(self, query: str = "", *, limit: int = 20) -> list[MemoryEntry]:
        """按关键词检索，命中正文或引用的算命中；空查询返回全部。

        不引向量库：条目量级是几十到几百，关键词加时间排序足够用。
        """
        needle = query.strip().lower()
        hits: list[MemoryEntry] = []
        for entry in self._ordered():
            if not entry.is_live():
                continue
            if needle and not self._matches(entry, needle):
                continue
            hits.append(entry)
            if len(hits) >= limit:
                break
        return hits

    def _matches(self, entry: MemoryEntry, needle: str) -> bool:
        """正文或任一引用里含关键词就算命中。"""
        if needle in entry.text.lower():
            return True
        return any(needle in ref.lower() for ref in entry.refs)

    def _ordered(self) -> list[MemoryEntry]:
        """按更新时间倒序，最近的排前面。"""
        return sorted(
            self.entries.values(), key=lambda item: item.updated_at, reverse=True
        )

    # ------------------------------------------------------------------
    # 对账
    # ------------------------------------------------------------------
    def revalidate(self) -> list[MemoryEntry]:
        """重算引用指纹，把变化过的条目标成 stale / missing，返回变化了的条目。

        这是记忆可信的底线：受体被重新准备过之后，旧记忆必须能被发现，
        否则模型会拿一份过期的参数继续往下做。
        """
        changed: list[MemoryEntry] = []
        for entry in self.entries.values():
            state = self._state_for(entry)
            if state != entry.state:
                entry.state = state
                changed.append(entry)
        if changed:
            self.save()
        return changed

    def _state_for(self, entry: MemoryEntry) -> str:
        """一条记忆当前的引用状态。"""
        if not entry.refs:
            return "ok"
        path = self._first_existing(entry.refs)
        if path is None:
            return "missing"
        if not entry.fingerprint:
            return "ok"
        try:
            current = fingerprint_path(path)
        except OSError:
            return "missing"
        return "ok" if current == entry.fingerprint else "stale"

    # ------------------------------------------------------------------
    # 注入
    # ------------------------------------------------------------------
    def resident_text(self, limit: int = RESIDENT_LIMIT) -> str:
        """常驻注入的正文：核心事实，按 token 上限裁剪。

        排序把「有引用的」放前面：引用指向项目里真实存在的产物，
        比一句没有凭据的陈述更值得占用常驻额度。同优先级按更新时间倒序，
        超限时先砍旧的。
        """
        live = [entry for entry in self.entries.values() if entry.is_live()]
        if not live:
            return ""
        ordered = sorted(live, key=lambda item: (not item.refs, -item.updated_at))
        lines: list[str] = []
        used = 0
        for entry in ordered:
            line = self.render_line(entry)
            cost = estimate_tokens(line)
            if used + cost > limit:
                continue
            lines.append(line)
            used += cost
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render_line(self, entry: MemoryEntry) -> str:
        """一行记忆：正文、引用与状态提醒。"""
        line = f"- {entry.text}"
        if entry.refs:
            line += f"（{entry.refs[0]}）"
        if entry.state == "stale":
            line += "（引用文件已变更，可能过期）"
        return line

    def render_markdown(self) -> str:
        """memory.md 的正文。给人看、给人改；真相仍以条目文件为准。"""
        lines = [
            "# 项目记忆",
            "",
            "本文件由记忆工具写出，可以手工编辑；程序以 entries.json 为准。",
            "",
        ]
        ordered = self._ordered()
        if not ordered:
            lines.append("（还没有记忆。）")
        else:
            lines.extend(self.render_line(entry) for entry in ordered)
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _resolve(self, ref: str) -> Path:
        """把引用解析成路径，相对路径按项目根解释。"""
        path = Path(ref)
        return path if path.is_absolute() else self.workspace_root / path

    def _first_existing(self, refs: list[str]) -> Path | None:
        """第一个存在的引用，都不存在时返回 None。"""
        for ref in refs:
            path = self._resolve(ref)
            try:
                if path.exists():
                    return path
            except OSError:
                continue
        return None

    def _fingerprint_for(self, refs: list[str]) -> str:
        """按第一个存在的引用算内容指纹，都不存在时留空串。"""
        path = self._first_existing(refs)
        if path is None:
            return ""
        try:
            return fingerprint_path(path)
        except OSError:
            return ""

    def _atomic_write(self, target: Path, text: str) -> None:
        """原子换名写一个文本文件。

        先写同目录下的临时文件，写完 fsync 再换名到目标位置。直接往目标
        文件里写的话，读的人会看到写了一半的内容——而被截断在 JSON 边界上
        的那份内容仍是合法 JSON，这让「写坏」比「写没」难查得多。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(
            f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        )
        try:
            with staged.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(staged, target)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise


def _replace_with_retry(staged: Path, target: Path) -> None:
    """把临时文件换名到目标位置，遇到占用就稍等再试。"""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(staged, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_DELAY)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class RememberTool(Tool):
    """写一条项目记忆，跨会话保留。

    与领域模块各自定义自己的工具是同一个做法：谁的数据，谁的接口。
    """

    spec = ToolSpec(
        name="remember",
        description=(
            "把一条项目事实写进项目记忆，跨会话保留，下次开会话还能取到。"
            "只记打分与筛选之前就存在的东西：受体、结合位点、配体库、参考活性物。"
            "**引用一律写文件路径**（相对路径按工作目录解释），不要写 handle——"
            "handle 只在本次会话有效，换个会话就指向别的东西。"
            "只在用户明确要求、或一条事实确实值得跨会话复用时才调用，不要随手记。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "一行事实，写清楚是什么、在哪个文件里",
                },
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "这条事实引用的文件路径，可以给多个；不给也行",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """写一条记忆并写盘。"""
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("缺少参数 text")
        raw_refs = arguments.get("refs")
        if raw_refs is None:
            refs: list[str] = []
        elif isinstance(raw_refs, list):
            refs = [str(item) for item in raw_refs if str(item).strip()]
        else:
            raise ToolError("参数 refs 应当是字符串数组")
        try:
            entry = self.store.remember(text, refs, source="agent")
        except MemoryError as error:
            raise ToolError(str(error)) from error
        return ToolResult(output=f"已记住（{entry.id}）：{entry.text}")


class RecallTool(Tool):
    """按关键词检索项目记忆。纯查询，不改动任何东西。"""

    spec = ToolSpec(
        name="recall",
        description=(
            "按关键词检索项目记忆，返回之前记住的项目事实。"
            "在向用户追问「上次那个受体在哪」之前先查一次——"
            "记忆是跨会话的，上一轮做过的事情这里可能已经有记录。"
            "留空 query 则返回全部记忆。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词，例如受体编号或文件名；留空则返回全部",
                },
            },
            "additionalProperties": False,
        },
    )

    # 收证据收集器。
    #
    # 它**不声明 `context_argument`**（纯查询工具不挂登记表，那条约定没变），
    # 但检索结果要进 EVIDENCE 层：记忆是跨会话的知识，RECENT 里本来就没有它，
    # 不给一份到证据层，下一步的 RECENT 一被压缩这段知识就没了。
    evidence_argument = "evidence"

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """检索记忆。"""
        raw = arguments.get("query")
        query = raw if isinstance(raw, str) else ""
        hits = self.store.recall(query)
        if not hits:
            return ToolResult(output="没有匹配的记忆。")
        self._collect(arguments.get("evidence"), hits)
        return ToolResult(
            output="\n".join(self.store.render_line(entry) for entry in hits)
        )

    def _collect(self, collector: Any, hits: list[MemoryEntry]) -> None:
        """把命中的记忆放进证据层。

        只调收集器的 `add`，不认它的类型：`memory.py` 是工具层，
        不该反过来依赖上下文模块。收集器为 None 时什么也不做——
        工具单独被调（判据、脚本里）时没有这一层。

        摘要用正文，正文放引用文件：**一行事实就是这条记忆的全部内容**，
        真正可追溯的凭据是它引用的路径，那才是要留给模型去取的东西。
        """
        if collector is None:
            return
        for entry in hits:
            collector.add(
                ref=f"记忆 {entry.id}",
                summary=entry.text,
                detail="引用文件：" + "、".join(entry.refs) if entry.refs else "",
            )
