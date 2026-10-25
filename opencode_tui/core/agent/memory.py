from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..file_log import record as log_read
from .ledger import LedgerError, fingerprint_path, parse_handle
from .prompt import estimate_tokens
from .tools import Tool, ToolError, ToolResult, ToolSpec

# 项目级记忆目录名，挂在项目根下。
MEMORY_DIR_NAME = ".vsmemory"
# 常驻注入的 token 上限。超限时先砍旧的，再按相关度补。
#
# 从 2000 降到 1200：常驻的那一段每一步都要发，而「相关的几条」比「全部旧条目」
# 更有用。其余的仍然可以用 `recall` 取到，不是删了。
RESIDENT_LIMIT = 1200
# 快照格式版本。
SNAPSHOT_VERSION = 1
# 单条记忆的字数上限。超了拒绝并让调用方拆条：
# 一条几百字的记忆会挤掉好几条短事实，而它本身又常常只是「一份流程」。
MEMORY_TEXT_LIMIT = 500
# 磁盘上最多保留多少条，超出的归档到 `archive.json`（不再注入，`/memory --all` 可查）。
MAX_ENTRIES = 500
# 用户写入时引用可以不存在（那时标 unverified，注入时排在后面）；
# agent 写入时引用必须存在——它有能力先确认路径，写错就是它的问题。
# 出现这些词时认为新值在**更新**同一主题：同 refs 的旧条目因此失效。
SUPERSEDE_MARKERS: tuple[str, ...] = ("改为", "改成", "现在", "不再", "替换", "已经换")
# 时间衰减的半衰期（天）。三个月前的事实不是错，只是没那么该先看到。
DECAY_DAYS = 90.0

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


def _terms(query: str) -> list[str]:
    """把查询切成可比的关键词：英文按词，中文按 bigram。

    中文不分词的话，「受体文件」整句只能整句匹配，换个语序就查不到；
    bigram 不引入词典，代价是极短查询切不出来，所以两个字以内按整词留。
    """
    terms: list[str] = []
    for token in re.split(r"[\s,，。;；:：/()（）]+", query):
        if not token:
            continue
        terms.extend(re.findall(r"[a-z0-9_.\-]+", token))
        cjk = "".join(char for char in token if not char.isascii())
        if len(cjk) <= 2:
            if cjk:
                terms.append(cjk)
        else:
            terms.extend(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return list(dict.fromkeys(terms))


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
    # ---- 版本与来源 ----
    #
    # 记忆是「项目已知什么」，而项目会变：换了受体、改了参数之后，旧那条不是错的，
    # 只是不再有效。硬删会让审计与回滚断掉，所以用 valid_to / superseded_by 标失效，
    # 条目本身留着。
    supersedes: list[str] = field(default_factory=list)  # 新条目取代了哪些旧条目
    superseded_by: str = ""                              # 被哪条取代；非空即不再生效
    valid_from: float = 0.0
    valid_to: float = 0.0
    # 最近一次被显式 `recall` 命中的时间，用来判断「核心事实」。
    last_used_at: float = 0.0
    # 用户写入但引用不存在的条目：可以留着，但注入时排在后面。
    unverified: bool = False
    confidence: float = 1.0
    session_id: str = ""
    turn: int = 0

    def is_live(self, now: float | None = None) -> bool:
        """还没过期、引用没丢、也没被新版本取代时才算可用。

        `stale`（文件变了）仍然算可用：变化本身是个信号，不是作废的理由，
        注入时带一行提醒交给模型判断。`missing` 才算不可用。
        """
        stamp = time.time() if now is None else now
        if self.expires_at and self.expires_at <= stamp:
            return False
        if self.superseded_by:
            return False
        if self.valid_to and self.valid_to <= stamp:
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
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "last_used_at": self.last_used_at,
            "unverified": self.unverified,
            "confidence": self.confidence,
            "session_id": self.session_id,
            "turn": self.turn,
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
        raw_supersedes = data.get("supersedes")
        supersedes = (
            [str(item) for item in raw_supersedes]
            if isinstance(raw_supersedes, list)
            else []
        )
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
            supersedes=supersedes,
            superseded_by=str(data.get("superseded_by") or ""),
            valid_from=float(data.get("valid_from") or 0.0),
            valid_to=float(data.get("valid_to") or 0.0),
            last_used_at=float(data.get("last_used_at") or 0.0),
            unverified=bool(data.get("unverified")),
            confidence=float(data.get("confidence") or 1.0),
            session_id=str(data.get("session_id") or ""),
            turn=int(data.get("turn") or 0),
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

    @property
    def archive_path(self) -> Path:
        """超出容量之后归档到的那一份。不注入，`/memory --all` 才看。"""
        return self.root / "archive.json"

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def load(self) -> bool:
        """从磁盘读条目，文件不存在或读不出来时返回 False。"""
        target = self.entries_path
        if not target.is_file():
            log_read(target, purpose="memory.load", ok=False, detail="文件不存在")
            return False
        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as error:
            # 读不出来与「没有记忆」在行为上一样，日志里要分开。
            log_read(target, purpose="memory.load", ok=False, detail=f"{type(error).__name__}：{error}")
            return False
        log_read(target, purpose="memory.load", size=len(raw.encode("utf-8")))
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
        supersedes: list[str] | None = None,
        session_id: str = "",
        turn: int = 0,
        ttl_days: float = 0.0,
    ) -> MemoryEntry:
        """写入一条记忆并写盘，返回写进去的那一条。

        写入不是「追加一行」那么简单，四道处理按顺序来：

        1. **精确去重**：同一句话再写一次只更新时间，不新建 id。模型读到自己
           刚写过的内容又"想起来"时很常见，不去重的话台账很快就全是重复。
        2. **引用校验**：agent 写入的引用必须存在；用户写入的允许不存在（标
           `unverified`，注入时排在后面）。能先确认路径的一方就该先确认。
        3. **同主题失效**：显式给了 `supersedes`，或新文本带「改为 / 不再 / 替换」
           这类更新词且与旧条目共用引用时，旧条目标 `valid_to`，不硬删。
        4. **容量**：超过 `MAX_ENTRIES` 时把最旧、已失效的挪进归档文件。
        """
        cleaned = " ".join(text.split())
        if not cleaned:
            raise MemoryError("记忆内容不能为空")
        if len(cleaned) > MEMORY_TEXT_LIMIT:
            raise MemoryError(
                f"一条记忆最多 {MEMORY_TEXT_LIMIT} 字，当前 {len(cleaned)} 字；"
                "请拆成几条各自成立的事实"
            )
        cleaned_refs = [str(item).strip() for item in (refs or []) if str(item).strip()]
        for ref in cleaned_refs:
            _reject_handle(ref)
        now = time.time()
        if ttl_days > 0 and not expires_at:
            expires_at = now + ttl_days * 86400

        existing = next(
            (item for item in self.entries.values() if item.text == cleaned), None
        )
        if existing is not None and existing.is_live(now):
            existing.updated_at = now
            for ref in cleaned_refs:
                if ref not in existing.refs:
                    existing.refs.append(ref)
            existing.fingerprint = self._fingerprint_for(existing.refs)
            self.save()
            return existing

        missing = [ref for ref in cleaned_refs if self._first_existing([ref]) is None]
        unverified = False
        if missing:
            if source != "user":
                raise MemoryError(
                    "引用的文件不存在：" + "、".join(missing) + "。请写真实存在的路径，"
                    "或者不写引用。"
                )
            unverified = True

        targets = self._supersede_targets(cleaned, cleaned_refs, supersedes, now)
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
            supersedes=[item.id for item in targets],
            valid_from=now,
            unverified=unverified,
            confidence=0.5 if unverified else 1.0,
            session_id=session_id,
            turn=turn,
        )
        for target in targets:
            target.valid_to = now
            target.superseded_by = entry.id
        self.entries[entry.id] = entry
        self._archive_overflow()
        self.save()
        return entry

    def _supersede_targets(
        self,
        text: str,
        refs: list[str],
        supersedes: list[str] | None,
        now: float,
    ) -> list[MemoryEntry]:
        """这次写入要取代哪些旧条目。

        - 显式给了编号：就按编号来，模型自己判断过哪条旧了。
        - 否则看「同主题」：新文本带更新词，且与旧条目共用引用。
          只共用引用还不够——同一份受体上可以同时有多条互不相干的事实，
          见到共用的引用就判失效会把它们一起清掉。
        """
        if supersedes:
            return [
                self.entries[item]
                for item in supersedes
                if item in self.entries and self.entries[item].is_live(now)
            ]
        if not refs or not any(marker in text for marker in SUPERSEDE_MARKERS):
            return []
        shared = set(refs)
        return [
            item
            for item in self.entries.values()
            if item.is_live(now) and item.refs and shared & set(item.refs)
        ]

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

    def _archive_overflow(self) -> int:
        """超过上限时把最该淘汰的挪进归档文件，返回挪了几条。

        先淘汰已失效的（被取代 / 过期 / 引用丢了），再淘汰最旧的活条目。
        归档不删：`/memory --all` 还能查，审计与回滚都不断。
        """
        overflow = len(self.entries) - MAX_ENTRIES
        if overflow <= 0:
            return 0
        ordered = sorted(
            self.entries.values(),
            key=lambda item: (item.is_live(), item.updated_at),
        )
        archive = self._read_archive()
        for entry in ordered[:overflow]:
            del self.entries[entry.id]
            archive.append(entry.to_dict())
        self._atomic_write(
            self.archive_path,
            json.dumps({"entries": archive}, ensure_ascii=False, indent=2),
        )
        return overflow

    def _read_archive(self) -> list[dict[str, Any]]:
        """读归档文件；不存在或读不出来时当空。"""
        target = self.archive_path
        if not target.is_file():
            return []
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def recall(
        self,
        query: str = "",
        *,
        limit: int = 20,
        touch: bool = False,
    ) -> list[MemoryEntry]:
        """按关键词检索，命中正文或引用的算命中；空查询按更新时间排。

        打分 = 字段权重 × 时间衰减：正文命中 2 分、引用命中 1 分，
        每 90 天衰减一半。不做全库向量——条目量级是几十到几百，
        中文 bigram 加时间衰减已经够用，而向量要额外的模型与索引。

        `touch` 只有**显式 recall** 才打开：常驻注入内部也会调它，
        那条路径如果也算「用过」，所有条目都会永远算「最近用过」。
        """
        needle = query.strip().lower()
        terms = _terms(needle)
        now = time.time()
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in self.entries.values():
            if not entry.is_live(now):
                continue
            score = self._score(entry, terms, now)
            if terms and score <= 0:
                continue
            scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].updated_at))
        hits = [entry for _, entry in scored[:limit]]
        if touch:
            for entry in hits:
                entry.last_used_at = now
        return hits

    def _score(self, entry: MemoryEntry, terms: list[str], now: float) -> float:
        """一条记忆对这次查询的得分。空查询不打分，一律 1。"""
        if not terms:
            return 1.0
        text = entry.text.lower()
        refs = [ref.lower() for ref in entry.refs]
        score = 0.0
        for term in terms:
            if term in text:
                score += 2.0
            if any(term in ref for ref in refs):
                score += 1.0
        if entry.unverified:
            # 用户写进来但引用不存在的：可能对，但排在有凭据的后面。
            score *= 0.5
        age_days = max(0.0, (now - entry.updated_at) / 86400)
        return score * math.exp(-age_days / DECAY_DAYS)

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
    def resident_text(self, limit: int = RESIDENT_LIMIT, query: str = "") -> str:
        """常驻注入的正文：核心事实 + 与当前问题相关的 + 最近更新的。

        三段按优先级拼，按 id 去重，填满 `limit` 为止：

        1. core：有引用、且近 30 天被显式 `recall` 命中过，最多 5 条；
        2. relevant：用当前问题检索出来的，最多 5 条；
        3. recent：最近更新的，最多 3 条；
        4. 还有余量就按「有引用优先、新的先」继续填。

        先前是「全部活条目按更新时间排，装不下就跳过」：内容一多，注入的永远是
        最近改过的那几条，与当前问题无关。相关度优先之后，同样 1200 token
        能命中的东西多得多；没被选中的仍可用 `recall` 取到。
        """
        now = time.time()
        live = [entry for entry in self.entries.values() if entry.is_live(now)]
        if not live:
            return ""
        picked: list[MemoryEntry] = []
        seen: set[str] = set()

        def take(entries: list[MemoryEntry], cap: int) -> None:
            count = 0
            for entry in entries:
                if count >= cap:
                    break
                if entry.id in seen:
                    continue
                seen.add(entry.id)
                picked.append(entry)
                count += 1

        recent_window = 30 * 86400
        core = [
            entry
            for entry in sorted(live, key=lambda item: -item.last_used_at)
            if entry.refs and entry.last_used_at and now - entry.last_used_at <= recent_window
        ]
        take(core, 5)
        if query.strip():
            take(self.recall(query, limit=10), 5)
        # 最近更新的那几条，有引用的优先：引用指向项目里真实存在的产物，
        # 比一句没有凭据的陈述更值得占常驻额度。
        take(sorted(live, key=lambda item: (not item.refs, -item.updated_at)), 3)
        take(sorted(live, key=lambda item: (not item.refs, -item.updated_at)), len(live))

        lines: list[str] = []
        used = 0
        for entry in picked:
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
        elif entry.state == "missing":
            line += "（引用的文件不在了）"
        if entry.unverified:
            line += "（引用未确认）"
        if entry.superseded_by:
            line += f"（已被 {entry.superseded_by} 取代）"
        return line

    def describe_line(self, entry: MemoryEntry) -> str:
        """`/memory` 用的两行：正文那一行 + 来源、时间与状态。"""
        stamp = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(entry.updated_at or entry.created_at)
        )
        bits = [f"来源 {entry.source or '未知'}", f"更新 {stamp}"]
        if entry.refs:
            bits.append("引用 " + "、".join(entry.refs))
        bits.append(FILE_STATE_NOTES.get(entry.state, entry.state))
        if entry.superseded_by:
            bits.append(f"已失效（被 {entry.superseded_by} 取代）")
        elif entry.expires_at:
            bits.append(
                "到期 " + time.strftime("%Y-%m-%d", time.localtime(entry.expires_at))
            )
        return self.render_line(entry) + "\n　　" + "　".join(bits)

    def all_entries(self, include_revoked: bool = False) -> list[MemoryEntry]:
        """全部条目，按更新时间倒序；`include_revoked` 为真时连失效的一起给。"""
        now = time.time()
        return [
            entry
            for entry in self._ordered()
            if include_revoked or entry.is_live(now)
        ]

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
                "ttl_days": {
                    "type": "number",
                    "description": "多少天后自动过期；不给就是长期有效。适合会变的临时结论",
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
        raw_ttl = arguments.get("ttl_days") or 0
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
            raise ToolError("参数 ttl_days 应当是数字")
        try:
            entry = self.store.remember(
                text, refs, source="agent", ttl_days=float(raw_ttl)
            )
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
        # 检索之前对一次账：上一轮可能刚重新准备过受体，
        # 引用变了的那条要带着提醒交出去，而不是等下一次启动才发现。
        self.store.revalidate()
        hits = self.store.recall(query, touch=True)
        if not hits:
            return ToolResult(output="没有匹配的记忆。")
        # 记下「这几条被用过」：常驻注入据此判断哪些是核心事实。
        # 这是唯一会写盘的地方，常驻注入内部那次 recall 不算。
        self.store.save()
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
