from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# 项目级记忆目录名，挂在项目根下。
MEMORY_DIR_NAME = ".vsmemory"
# 常驻注入的 token 上限。超限时先砍旧的，再按相关度补。
#
# 从 2000 降到 1200：常驻的那一段每一步都要发，而「相关的几条」比「全部旧条目」
# 更有用。其余的仍然可以用 `recall` 取到，不是删了。
RESIDENT_LIMIT = 1200
# 快照格式版本。字段只增不减，旧文件缺键按默认值读回，所以不必跟着涨。
SNAPSHOT_VERSION = 1
# 单条记忆的字数上限。超了拒绝并让调用方分成几条：
# 一条几百字的记忆会挤掉好几条短事实，而它本身又常常只是「一份流程」。
MEMORY_TEXT_LIMIT = 500
# 磁盘上最多保留多少条，超出的归档到 `archive.json`（不再注入，`/memory --all` 可查）。
MAX_ENTRIES = 500
# 出现这些词时认为新值在**更新**同一主题，还不足以判定是哪一条旧条目。
SUPERSEDE_MARKERS: tuple[str, ...] = ("改为", "改成", "现在", "不再", "替换", "已经换")
# 隐式取代的重合度下限。低于它说明更新词说的是另一件事：
# 「盒子改为 24A」与「受体改为 v2.pdb」共用一个引用、都带更新词，却互不相干。
#
# 0.20 是量出来的：同一件事的更新对（改一个数值、换一个文件名）在 0.267～0.800 之间，
# 不相干的事实对在 0.000～0.045 之间。数字类事实改一个值，二元组重合掉得比想象快，
# 门槛定高了会把「口袋中心改为 14,8,22」判成另一件事。
SUPERSEDE_OVERLAP = 0.20
# 时间衰减的半衰期（天）。三个月前的事实不是错，只是没那么该先看到。
DECAY_DAYS = 90.0
# 经验衰减得更慢：一次失败换来的判断，半年之后仍然成立。
EXPERIENCE_DECAY_DAYS = 180.0
# 用户习惯占多少常驻额度。习惯与当前问的是哪个靶点无关，所以它不参与相关度，
# 只能按先来后到占一块固定的额度；两块加起来不能超过 RESIDENT_LIMIT。
PREFERENCE_LIMIT = 200
# 习惯条数上限。习惯是每一条都要常驻的，攒到几十条时 200 token 装不下，
# 而那时注入的一半内容与当前任务无关。
MAX_PREFERENCES = 8

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

    三类各有自己的行为：是否常驻、按多长衰减、要不要引用、能不能被自动取代。
    事件层已经有 Topic → Step 的历史记录，会话状态已经有 `TaskState`，
    项目规则已经在 `AGENTS.md`，这三件不再各存一份记忆。
    """

    FACT = "fact"
    PREFERENCE = "preference"
    EXPERIENCE = "experience"


KIND_NAMES: dict[MemoryKind, str] = {
    MemoryKind.FACT: "事实",
    MemoryKind.PREFERENCE: "习惯",
    MemoryKind.EXPERIENCE: "经验",
}


class MemoryError(Exception):
    """记忆自身的错误，调用方只需要处理这一种。"""


def parse_kind(value: object) -> MemoryKind:
    """把外部传来的类型名解析成枚举。认不出来时抛出，不做回退。"""
    if isinstance(value, MemoryKind):
        return value
    try:
        return MemoryKind(str(value))
    except ValueError:
        known = "、".join(item.value for item in MemoryKind)
        raise MemoryError(f"不认识的记忆类型：{value!r}；可用 {known}")


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
    # 与哪几条互相矛盾。两条都保持有效：矛盾本身是要交给模型判断的信息，
    # 自动判谁对谁错会猜错（数值更新与真正冲突在字面上分不开）。
    conflicts: list[str] = field(default_factory=list)
    # 这一条按什么判据被取代。写的是被取代的那一方：事后查账要回答的是
    # 「它为什么失效」，答案在旧条目上。
    superseded_rule: str = ""
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
            "superseded_rule": self.superseded_rule,
            "conflicts": list(self.conflicts),
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
        raw_conflicts = data.get("conflicts")
        conflicts = (
            [str(item) for item in raw_conflicts]
            if isinstance(raw_conflicts, list)
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
            superseded_rule=str(data.get("superseded_rule") or ""),
            conflicts=conflicts,
            valid_from=float(data.get("valid_from") or 0.0),
            valid_to=float(data.get("valid_to") or 0.0),
            last_used_at=float(data.get("last_used_at") or 0.0),
            unverified=bool(data.get("unverified")),
            confidence=float(data.get("confidence") or 1.0),
            session_id=str(data.get("session_id") or ""),
            turn=int(data.get("turn") or 0),
        )
