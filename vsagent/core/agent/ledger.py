from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Literal

# 换名在目标文件正被别的进程打开时会被系统拒绝，隔几毫秒重试。
REPLACE_ATTEMPTS = 40
REPLACE_RETRY_DELAY = 0.005

KINDS: tuple[str, ...] = ("receptor", "ligandset", "artifact")
"""登记表里的三类条目。

刻意只有三类，不再细分：**受体会被反复引用（一个），配体集会被增删筛（多个），
产物是它们算出来的东西（更多个）**。领域侧要区分配体集和它的子集，用 `meta` 里的
字段表示，不要新增类型——类型一旦膨胀，模型的判断负担就跟着涨。
"""

Kind = Literal["receptor", "ligandset", "artifact"]

_HANDLE_PATTERN = re.compile(r"^(?P<kind>[a-z]+)\s*#\s*(?P<seq>\d+)$")

FINGERPRINT_LENGTH = 16

# 没有时间戳的类别，模型的判断负担会小一些
_FILE_STATE_NOTES: dict[str, str] = {
    "ok": "文件在，内容与登记时一致",
    "stale": "文件在，但内容与登记时不同",
    "missing": "产物文件不在了",
}

SNAPSHOT_VERSION = 1

# 工作区索引（`SessionLedger.digest`）的限额。
#
# 这段每一步都要发，所以按**行数**卡死而不是按 token 估：一行一个 handle，
# 十行够模型认出「手上现在有哪些编号」；再多就是把 RECENT 的额度挪过来。
DIGEST_MAX_LINES = 10
DIGEST_ARTIFACT_LIMIT = 5
# 一行里摘要的长度上限。索引不是清单：摘要只是帮模型认出「这个是干什么的」，
# 细节用 describe_entry 取。
DIGEST_LINE_CHARS = 60
# 配体集只列 handle、不列摘要，一行最多这么多个。
DIGEST_HANDLE_LIMIT = 5
DIGEST_NOTICE = "（工作区索引：下面是登记表里现有的 handle，照抄，不要按前几步的编号往后推）"


class LedgerError(Exception):
    """登记表自身的错误，调用方只需要处理这一种。"""


# ---------------------------------------------------------------------------
# handle
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Handle:
    """登记表里一个条目的引用。

    **带类型是这个类的全部意义。** 工具签名写成 `LigandSetHandle` 之后，
    「模型把 `receptor#1` 传给一个要配体集的参数」就变成一条可读的参数错误，
    而不是工具内部的一堆判断。

    三种形态（基类与三个子类）都由 `coerce_handle` 产出，字符串与对象都认。
    """

    seq: int

    kind: ClassVar[str] = ""

    def __str__(self) -> str:
        return f"{self.kind}#{self.seq}"

    @property
    def handle(self) -> Handle:
        """让自己也能当条目用，`coerce_handle` 的一条路走这里。"""
        return self


@dataclass(frozen=True, slots=True)
class ReceptorHandle(Handle):
    """受体引用。一个会话里通常只有一个「当前受体」。"""

    kind: ClassVar[str] = "receptor"


@dataclass(frozen=True, slots=True)
class LigandSetHandle(Handle):
    """配体集引用。可能同时存在多个（原始库、粗筛后、去重后……）。"""

    kind: ClassVar[str] = "ligandset"


@dataclass(frozen=True, slots=True)
class ArtifactHandle(Handle):
    """产物引用（打分表、聚类结果、导出清单……）。"""

    kind: ClassVar[str] = "artifact"


_HANDLE_CLASSES: dict[str, type[Handle]] = {
    "receptor": ReceptorHandle,
    "ligandset": LigandSetHandle,
    "artifact": ArtifactHandle,
}


def make_handle(kind: str, seq: int) -> Handle:
    """按类型构造对应的子类实例。

    返回具体子类而不是基类，`isinstance(entry.handle, ReceptorHandle)`
    这类判断才成立。
    """
    cls = _HANDLE_CLASSES.get(kind)
    if cls is None:
        raise LedgerError(f"未知的条目类型 {kind!r}，应当是 {' / '.join(KINDS)} 之一")
    if seq < 1:
        raise LedgerError(f"条目序号应当是正整数，收到 {seq}")
    return cls(seq=seq)


def parse_handle(text: str) -> Handle:
    """把 `"receptor#1"` 解析成具体子类的 handle。

    格式不对时抛 `LedgerError`，它的说明会被写成一条可读的失败回灌给模型。
    """
    if not isinstance(text, str):
        raise LedgerError(f"handle 应当是字符串，收到 {type(text).__name__}")
    match = _HANDLE_PATTERN.match(text.strip())
    if match is None:
        raise LedgerError(
            f"{text!r} 不是合法的 handle。格式为 <类型>#<序号>，"
            f"类型取 {' / '.join(KINDS)} 之一，例如 receptor#1"
        )
    return make_handle(match.group("kind"), int(match.group("seq")))


def coerce_handle(value: Handle | Entry | str) -> Handle:
    """把字符串、handle 或 `Entry` 统一成具体子类的 handle。

    **接受 `Entry` 是刻意的**：`led.add(...)` 返回的就是一个 `Entry`，
    调用方很自然会想直接把它传回去当父项。拦下来报一句
    「handle 应当是字符串，收到 Entry」是把一件本来能猜对的事推回给调用方，
    而它手上那个东西里明明就有 handle。
    """
    if isinstance(value, Handle):
        return make_handle(value.kind, value.seq)
    entry_handle = getattr(value, "handle", None)
    if isinstance(entry_handle, Handle):
        return make_handle(entry_handle.kind, entry_handle.seq)
    return parse_handle(value)


def expect_handle(value: Any, expected: type[Handle], parameter: str) -> Handle:
    """校验一个 handle 的类型，拿错了给一句说清「手上那个是什么」的中文。

    走这一条而不是让调用方各自比较 kind：**类型化 handle 的主要收益就是让这个
    错误可读**，文案含糊等于把收益还回去一半。
    """
    try:
        handle = coerce_handle(value)
    except LedgerError as error:
        raise LedgerError(f"参数 {parameter} {error}") from error
    if not isinstance(handle, expected):
        wanted = _HANDLE_CLASSES_INVERSE[expected]
        raise LedgerError(
            f"参数 {parameter} 要的是 {wanted}，但收到 {handle}（属于 {handle.kind}）。"
            "请检查 handle 是否拿错了——不同类型的条目不能互用"
        )
    return handle


_HANDLE_CLASSES_INVERSE: dict[type[Handle], str] = {
    cls: kind for kind, cls in _HANDLE_CLASSES.items()
}


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Entry:
    """一次「登记进登记表」的完整记录。"""

    handle: Handle
    path: Path | None = None
    """产物写盘位置，可以是文件也可以是目录；纯内存对象为空。

    一组文件（如受体准备的产物）按目录登记，血缘与对账都按整份算。
    """

    summary: str = ""
    """一行说明，用于回灌给模型。"""

    fingerprint: str = ""
    """内容指纹；空表示未计算。

    **这是「同一件事」的判据**——对接一次几十分钟，用户换个排序方式
    不该让它重算。
    """

    file_state: str = "ok"
    """产物文件的当前状态，由 `reconcile()` 现算。

    这是个派生值：写盘只是为了让人翻 JSON 时看得懂，恢复时会重新核对一遍，
    不要当着凭据读。
    """

    parents: tuple[Handle, ...] = ()
    """血缘：本项由哪些项算出来（决定切换受体时它是否失效）。"""

    meta: dict[str, Any] = field(default_factory=dict)
    """其余结构化信息，自由形状。"""

    created_at: float = 0.0

    def brief(self) -> str:
        """给模型看的一行：`ligandset#3 · 粗筛后剩余 1200 个分子`。

        状态提示排在**最后**：模型扫这一列时先认 handle 与摘要，异常信息挂在
        行尾既不影响扫读，也不会被截断的摘要挤没。正常（ok）不显示——
        一屏几十行，那三个字只会把 handle 之间挤乱。
        """
        parts = [str(self.handle)]
        if self.summary:
            parts.append(self.summary)
        if self.parents:
            parts.append("来自 " + ", ".join(str(item) for item in self.parents))
        if self.file_state != "ok":
            parts.append(f"（{_FILE_STATE_NOTES.get(self.file_state, self.file_state)}）")
        return " · ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle": {"kind": self.handle.kind, "seq": self.handle.seq},
            "path": str(self.path) if self.path is not None else "",
            "summary": self.summary,
            "fingerprint": self.fingerprint,
            "file_state": self.file_state,
            "parents": [{"kind": item.kind, "seq": item.seq} for item in self.parents],
            "meta": self.meta,
            "created_at": self.created_at,
        }


def _handle_from_raw(raw: Any) -> Handle:
    """把存档里的一处 handle 还原成具体子类。

    存档里的 handle 是个字典，而 `parents` 里那一串同样是字典——两处必须走
    同一条还原路径，否则会出现「主 handle 修好了、在 parents 上撞同一个跟头」。
    """
    try:
        if isinstance(raw, dict):
            return make_handle(str(raw.get("kind") or ""), int(raw.get("seq") or 0))
        return coerce_handle(raw)
    except (LedgerError, TypeError, ValueError) as error:
        raise LedgerError(f"handle 读不出来：{raw!r}") from error


def entry_from_dict(raw: dict[str, Any]) -> Entry:
    """从存档里还原一条登记项。

    读不懂的条目抛 `LedgerError`，由调用方决定跳过还是整体失败——
    **一条坏记录不该废掉整份索引**。
    """
    raw_path = raw.get("path")
    raw_parents = raw.get("parents")
    return Entry(
        handle=_handle_from_raw(raw.get("handle")),
        path=Path(str(raw_path)) if raw_path else None,
        summary=str(raw.get("summary") or ""),
        fingerprint=str(raw.get("fingerprint") or ""),
        file_state=_coerce_file_state(raw.get("file_state")),
        parents=tuple(
            _handle_from_raw(item)
            for item in (raw_parents if isinstance(raw_parents, list) else [])
            if item is not None
        ),
        meta=dict(raw.get("meta") or {}),
        created_at=float(raw.get("created_at") or 0.0),
    )


def _coerce_file_state(raw: Any) -> str:
    """存档里的状态只当「未知」处理——它本来就是个派生值。

    猜一个状态比留空更坏：猜成 missing 会让一个好好的产物被模型绕开。
    """
    return raw if raw in ("ok", "stale", "missing") else "ok"


# ---------------------------------------------------------------------------
# 内容指纹
# ---------------------------------------------------------------------------
def fingerprint_bytes(data: bytes, *, length: int = FINGERPRINT_LENGTH) -> str:
    return hashlib.sha256(data).hexdigest()[:length]


def fingerprint_text(text: str, *, length: int = FINGERPRINT_LENGTH) -> str:
    return fingerprint_bytes(text.encode("utf-8"), length=length)


def fingerprint_file(
    path: str | Path,
    *,
    length: int = FINGERPRINT_LENGTH,
    chunk_size: int = 1 << 20,
) -> str:
    """按文件内容算指纹（分块读，大文件也不吃内存）。

    不用「路径 + 修改时间」代替：那样复制一份文件就会当成新东西，
    而内容寻址要的恰恰是「换个名字进来，还是同一件事」。
    """
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()[:length]


def fingerprint_path(
    path: str | Path,
    *,
    length: int = FINGERPRINT_LENGTH,
    chunk_size: int = 1 << 20,
) -> str:
    """按内容算指纹：文件走 `fingerprint_file` 那一套，**目录走「清单 + 内容」**。

    条目登记的不一定是单个文件。受体准备的产物天然是一组（结构 + 引擎参数 +
    体检报告），用目录装起来最省事，而 `Entry.path` 本来就是个路径。

    **哈希里必须同时有文件名与内容**：只哈希内容的话，「改了参数文件里的盒子
    但顺手改了文件名」会被算成同一份；只哈希文件名的话，内容被改了却看不出来。
    盒子那件事正是最要命的一类静默错误。

    目录里的**空子目录也算一条**：只把文件列进去的话，往产物目录里加一个空
    `logs/` 不会改变指纹——而「目录结构变了」和「内容变了」在这套口径下应当
    没有区别。断链的符号链接同理，记一条占位而不是悄悄跳过。

    遍历不跟随符号链接，所以自指的链接目录不会转不出来。
    """
    target = Path(path)
    if not target.is_dir():
        return fingerprint_file(target, length=length, chunk_size=chunk_size)

    hasher = hashlib.sha256()
    for item in sorted(target.rglob("*"), key=lambda item: item.as_posix()):
        # 相对路径本身进哈希：换个名字进来就不是同一份产物
        hasher.update(item.relative_to(target).as_posix().encode("utf-8"))
        if item.is_dir():
            hasher.update(b"/\0")
        elif item.is_file():
            hasher.update(b"\0")
            with item.open("rb") as handle:
                while chunk := handle.read(chunk_size):
                    hasher.update(chunk)
            hasher.update(b"\0")
        else:
            # 断链符号链接、设备文件之类：记一条占位，别跳过
            hasher.update(b"!\0")
    return hasher.hexdigest()[:length]


def fingerprint_parts(*parts: object, length: int = FINGERPRINT_LENGTH) -> str:
    """把若干参数规范化后拼起来哈希——用作「同一件事」的缓存键。

    典型用法：`fingerprint_parts(receptor.fingerprint, ligandset.fingerprint, top_n)`。
    字典按键排序、其它类型走 str，同一组参数算出来必然相同。
    """
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return fingerprint_text(payload, length=length)


# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------
class SessionLedger:
    """一个会话的资源登记表：handle、血缘、指纹、对账。

    **登记表本身写盘**（原子换名写整份快照），条目只是引用，真正的产物由
    领域工具写到磁盘上（`Entry.path` 指着它）。

    线程安全：同步工具的 handler 跑在线程池里，同一轮的多个工具会并发读写，
    所以每个公开方法都持锁。用 `RLock` 是因为内部方法之间会互相调用。
    """

    def __init__(
        self,
        *,
        session_id: str = "",
        snapshot_path: Path | None = None,
        link_current_receptor: bool = True,
    ) -> None:
        """
        Args:
            session_id: 哪个会话的登记表。写盘路径默认按它拼出来。
            snapshot_path: 快照文件路径，留空时按 session_id 取默认位置。
            link_current_receptor: 登记非受体条目且没显式给 parents 时，
                自动把当前受体挂为其父项。默认开启——会话里「当前受体」就是
                默认上下文，产物天然属于它；而**没有血缘的产物在切换受体时
                无法被识别为「属于旧受体」**，会悄悄留在结果里。
        """
        self.session_id = session_id
        self.snapshot_path = snapshot_path or default_snapshot_path(session_id)
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._counters: dict[str, int] = dict.fromkeys(KINDS, 0)
        self._archived: set[str] = set()
        self._current_receptor: ReceptorHandle | None = None
        self._link_current_receptor = link_current_receptor

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add(
        self,
        kind: Kind,
        *,
        path: str | Path | None = None,
        summary: str = "",
        fingerprint: str = "",
        parents: list[Handle | str] | tuple[Handle | str, ...] = (),
        meta: dict[str, Any] | None = None,
        link_current_receptor: bool = True,
    ) -> Entry:
        """登记一个新条目并返回它。

        `link_current_receptor=False` 给跨靶点的配体库用：它不属于某个受体，
        换靶点时不该跟着被搁置。对接产物、运行目录这些与受体绑定的条目仍用默认值。
        """
        with self._lock:
            handle = self._allocate(kind)
            resolved = [coerce_handle(item) for item in parents]
            if (
                link_current_receptor
                and self._link_current_receptor
                and kind != "receptor"
                and self._current_receptor is not None
                and not any(isinstance(item, ReceptorHandle) for item in resolved)
            ):
                resolved.append(self._current_receptor)

            entry = Entry(
                handle=handle,
                path=Path(path) if path is not None else None,
                summary=summary,
                fingerprint=fingerprint,
                parents=tuple(resolved),
                meta=dict(meta or {}),
                created_at=time.time(),
            )
            self._entries[str(handle)] = entry
            if isinstance(handle, ReceptorHandle) and self._current_receptor is None:
                self._current_receptor = handle
            self._save()
            return entry

    def remove(self, handle: Handle | str) -> Entry:
        """删掉一个条目，把它挂到别的条目上的父项转接过去。

        **血缘不能无声地断掉**：删掉中间那一条时，直接丢掉会让下游产物的
        「完整血缘」少一环，而它在界面上仍然会显示一条完整的链。
        """
        with self._lock:
            key = str(coerce_handle(handle))
            entry = self._entries.get(key)
            if entry is None:
                raise LedgerError(f"登记表里没有 {key}")
            del self._entries[key]
            self._archived.discard(key)
            for other_key, other in list(self._entries.items()):
                if key not in [str(item) for item in other.parents]:
                    continue
                rewritten = tuple(
                    item for item in other.parents if str(item) != key
                ) + tuple(item for item in entry.parents if str(item) not in
                          [str(existing) for existing in other.parents])
                self._entries[other_key] = replace(other, parents=rewritten)
            if self._current_receptor is not None and str(self._current_receptor) == key:
                self._current_receptor = None
            self._save()
            return entry

    def reset(self) -> None:
        """清空整份登记表。"""
        with self._lock:
            self._entries.clear()
            self._counters = dict.fromkeys(KINDS, 0)
            self._archived.clear()
            self._current_receptor = None
            self._save()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get(self, handle: Handle | str) -> Entry:
        """按 handle 取条目；不存在时抛 `LedgerError`。"""
        with self._lock:
            key = str(coerce_handle(handle))
            entry = self._entries.get(key)
            if entry is None:
                raise LedgerError(
                    f"登记表里没有 {key}。先用 list_workspace 看清有哪些条目，"
                    "不要凭记忆写 handle"
                )
            return entry

    def find(
        self,
        *,
        kind: Kind | None = None,
        fingerprint: str | None = None,
        include_archived: bool = False,
        meta_match: dict[str, Any] | None = None,
    ) -> list[Entry]:
        """按类型 / 指纹 / meta 片段筛选。默认只看当前受体下的条目。"""
        with self._lock:
            found = [
                entry
                for key, entry in self._entries.items()
                if (include_archived or key not in self._archived)
                and (kind is None or entry.handle.kind == kind)
                and (fingerprint is None or entry.fingerprint == fingerprint)
                and _meta_contains(entry.meta, meta_match)
            ]
            return sorted(found, key=lambda entry: (entry.handle.kind, entry.handle.seq))

    def entries(self, *, include_archived: bool = False) -> list[Entry]:
        """全部条目，按类型与序号排序。"""
        return self.find(include_archived=include_archived)

    def lineage(self, handle: Handle | str) -> list[Entry]:
        """回溯血缘，从直接父项到最远祖先。"""
        with self._lock:
            start = self.get(handle)
            seen: set[str] = set()
            ordered: list[Entry] = []
            frontier = list(start.parents)
            while frontier:
                parent = frontier.pop(0)
                key = str(parent)
                if key in seen:
                    continue
                seen.add(key)
                known = self._entries.get(key)
                if known is None:
                    continue
                ordered.append(known)
                frontier.extend(known.parents)
            return ordered

    def current_receptor(self) -> ReceptorHandle | None:
        """当前受体；尚未设定时返回 None。"""
        with self._lock:
            return self._current_receptor

    def is_archived(self, handle: Handle | str) -> bool:
        """这条是不是已被搁置（属于切换掉的旧受体）。"""
        with self._lock:
            return str(coerce_handle(handle)) in self._archived

    def digest(self, *, artifact_limit: int = DIGEST_ARTIFACT_LIMIT) -> str:
        """当前工作区的索引：当前受体 + 最近的几个产物，不超过 `DIGEST_MAX_LINES` 行。

        为什么要有这一段：handle 只在**工具返回的那一刻**出现在上下文里，之后要么
        进了证据层、要么随旧工具结果被清掉。模型要用的时候手里没有索引，只能按前几步
        见过的编号往后推——实测里它就是这么写出 `artifact#2`、而登记表里只有
        `artifact#1` 的（见 `docs/real-session-findings.md` F3）。

        刻意只做三件事：报当前受体、报最近的产物、报配体集的 handle。**不调
        `reconcile()`**：那会把登记过的每个路径都读一遍算指纹，而这段每一步都要
        现算；`file_state` 用上一次对账的结果——不新鲜也不会把编号指错。

        已搁置的条目不算（`find()` 默认就排除）：它们是切换掉的旧受体下面的东西，
        列出来等于让模型去用一个已经被搁置的 handle。

        空登记表返回空串：什么都没登记时塞一句「工作区是空的」只是白占额度。
        """
        with self._lock:
            entries = self.find()
            if not entries:
                return ""
            lines = [DIGEST_NOTICE]
            current = self._current_receptor
            if current is not None and str(current) in self._entries:
                lines.append(_digest_line(self._entries[str(current)], "当前受体："))
            else:
                lines.append("当前受体：未设定（产物还没有归属，先 switch_receptor）")

            ligands = [
                str(entry.handle) for entry in entries if entry.handle.kind == "ligandset"
            ]
            ligand_line = ""
            if ligands:
                listed = "、".join(ligands[:DIGEST_HANDLE_LIMIT])
                more = f" 等 {len(ligands)} 个" if len(ligands) > DIGEST_HANDLE_LIMIT else ""
                ligand_line = f"配体集：{listed}{more}"

            # 行数先算出来再装：产物一多要丢的是**最旧的**那几个，而不是最后
            # 切片——切掉的可能正好是配体集或当前受体那一行。
            room = DIGEST_MAX_LINES - len(lines) - 1 - (1 if ligand_line else 0)
            artifacts = [entry for entry in entries if entry.handle.kind == "artifact"]
            # `keep` 单独算出来再切：写成 `artifacts[-min(...):]` 时，限额为 0
            # 会退化成 `artifacts[0:]`——`-0` 就是 `0`，一个不列变成全列。
            keep = max(0, min(artifact_limit, room))
            shown = artifacts[-keep:] if keep else []
            if shown:
                hidden = len(artifacts) - len(shown)
                tail = f"，只列最近 {len(shown)} 个" if hidden else ""
                lines.append(f"产物（共 {len(artifacts)} 个{tail}）：")
                lines.extend(_digest_line(entry, "- ") for entry in shown)
            if ligand_line:
                lines.append(ligand_line)
            return "\n".join(lines)

    # ------------------------------------------------------------------
    # 受体切换
    # ------------------------------------------------------------------
    def stale_after(self, handle: Handle | str | None) -> list[Entry]:
        """**假如**把当前受体换成 `handle`，哪些条目会失效（含旧受体自身）。

        这是一个**只读的假设查询**，什么都不改——它的存在只为一个目的：
        让「预演」和「真做」拿到同一份清单。`switch_receptor` 是两阶段的，
        第一阶段把影响范围告诉模型、由它决定要不要带 `confirm=true`；
        如果预演自己另算一遍，两份清单就会不一致，而**少报是危险的那一侧**：
        多出来的那些条目静默消失，正是本类要避免的「静默失效」。

        因此这条必须成立：`switch_receptor` 搁置的条目，与同一时刻
        `stale_after`（当前受体）报出来的**正好是同一批**。两处共用
        `_stale_keys`，不各写一份。

        规则由血缘决定，不由名字决定：产物登记时默认挂当前受体为父项，
        所以**血缘能追溯到该受体的都是它的**（含受体自己）。
        """
        with self._lock:
            return [self._entries[key] for key in self._stale_keys(handle)
                    if key in self._entries]

    def switch_receptor(self, handle: Handle | str) -> list[Entry]:
        """切换当前受体，返回**因切换而失效**的旧条目（不删除）。

        返回的必须是切换前那一刻 `stale_after`（原当前受体）的结果。

        **切到当前受体自身是空操作**：不这样判的话，`_stale_keys(previous)`
        会把它自己和它下面的一切都搁置起来，再把同一个 handle 设成当前——
        结果是「当前受体显示为已搁置」，而默认视图（只列当前受体的条目）
        变成空的，模型会以为自己的东西全没了。重新点一次已有的受体是
        完全正常的动作，**不能有第二次点击就把工作区清空的后果**。
        """
        target = expect_handle(handle, ReceptorHandle, "receptor")
        with self._lock:
            self.get(target)
            if self._current_receptor is not None and self._current_receptor == target:
                return []
            previous = self._current_receptor
            affected_keys = self._stale_keys(previous)
            for key in affected_keys:
                self._archived.add(key)
            self._current_receptor = target
            self._save()
            return [self._entries[key] for key in affected_keys if key in self._entries]

    def discard_archived(self) -> list[Entry]:
        """真正丢掉被搁置的条目，返回丢掉的那些。"""
        with self._lock:
            dropped = [self._entries[key] for key in list(self._archived)
                       if key in self._entries]
            for key in list(self._archived):
                self._entries.pop(key, None)
            self._archived.clear()
            self._save()
            return dropped

    def _stale_keys(self, handle: Handle | str | None) -> list[str]:
        """血缘能追溯到 `handle` 的那些条目的 key（含 handle 自身）。

        只写这一处：`stale_after` 与 `switch_receptor` 都调它。
        """
        if handle is None:
            return []
        key = str(coerce_handle(handle))
        if key not in self._entries:
            return []
        return [
            other_key
            for other_key in sorted(self._entries)
            if other_key == key or self._traces_to(self._entries[other_key], key)
        ]

    def _traces_to(self, entry: Entry, key: str) -> bool:
        """这条的血缘里有没有 `key`（连同隔层的）。"""
        seen: set[str] = set()
        stack = list(entry.parents)
        while stack:
            parent = stack.pop()
            parent_key = str(parent)
            if parent_key == key:
                return True
            if parent_key in seen:
                continue
            seen.add(parent_key)
            known = self._entries.get(parent_key)
            if known is not None:
                stack.extend(known.parents)
        return False

    # ------------------------------------------------------------------
    # 对账
    # ------------------------------------------------------------------
    def reconcile(self) -> list[Entry]:
        """核对每个条目的产物文件，返回**状态发生变化**的条目。

        四路规则，**没有一路允许静默**：

        - 没有 path（纯内存对象）→ ok：没有文件可核对
        - 路径不在 → missing。**条目不删**——「它曾经存在」是事实，
          丢掉会让血缘无声地断掉
        - 路径在、登记时没记指纹 → ok：只能确认它还在，没得比
        - 路径在、指纹不一致 → stale。**不静默信任**：内容变了，
          原先算出来的结论就不成立了，而它和正常条目长得一模一样

        路径是文件还是目录都走同一套（见 `fingerprint_path`）。

        只读不写：这是「看」而不是「改」，所以**不写盘**。把它做成一次写盘的话，
        「打开会话」就等于「改动存档」，而且每列一次工作区就重写一遍快照。

        代价要心里有数：**这一步会把登记过的路径逐个读一遍算指纹**。
        几千个小 CSV 是毫秒级，但登记了上百 MB 的对接姿态文件之后会明显变慢。
        真到那时再按大小分层（存在性 vs 内容一致性），而不是现在先猜一个阈值。
        """
        changed: list[Entry] = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                state = self._file_state(entry)
                if state == entry.file_state:
                    continue
                updated = replace(entry, file_state=state)
                self._entries[key] = updated
                changed.append(updated)
        return changed

    @staticmethod
    def _file_state(entry: Entry) -> str:
        """单个条目的状态判定，规则见 `reconcile`。

        一律走 `fingerprint_path` 而不是 `fingerprint_file`：后者对目录会抛
        `IsADirectoryError`，而它是 `OSError` 的子类——上面那个 except 会把
        一个**好好的产物目录**判成 missing，且全程不报错。这是「按目录登记」
        唯一会静默出错的地方，所以判定入口只能有一个。
        """
        if entry.path is None:
            return "ok"
        try:
            if not entry.path.exists():
                return "missing"
        except OSError:
            # 路径非法到连存在性都问不出来（超长、非法字符）——当作找不到
            return "missing"
        if not entry.fingerprint:
            return "ok"
        try:
            actual = fingerprint_path(entry.path)
        except OSError:
            return "missing"
        return "ok" if actual == entry.fingerprint else "stale"

    # ------------------------------------------------------------------
    # 写盘
    # ------------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """整份快照。"""
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "session_id": self.session_id,
                "current_receptor": (
                    {"kind": self._current_receptor.kind, "seq": self._current_receptor.seq}
                    if self._current_receptor is not None
                    else None
                ),
                "counters": dict(self._counters),
                "archived": sorted(self._archived),
                "entries": [self._entries[key].as_dict() for key in sorted(self._entries)],
            }

    def load_snapshot(self, snapshot: dict[str, Any]) -> int:
        """从快照恢复，返回读进来几条。

        读不懂的条目**跳过并计数**，不让一条坏记录废掉整份索引。
        """
        with self._lock:
            self._entries = {}
            restored = 0
            for raw in snapshot.get("entries") or []:
                try:
                    entry = entry_from_dict(raw)
                except LedgerError:
                    continue
                self._entries[str(entry.handle)] = entry
                restored += 1
            self._counters = self._restore_counters(snapshot.get("counters"), self._entries)
            raw_archived = snapshot.get("archived")
            self._archived = {
                str(item) for item in (raw_archived if isinstance(raw_archived, list) else [])
                if str(item) in self._entries
            }
            self._current_receptor = self._restore_current(snapshot.get("current_receptor"))
            return restored

    def save(self) -> Path | None:
        """把快照写到磁盘，返回写入路径（没有 session_id 时不写）。"""
        return self._save()

    def _save(self) -> Path | None:
        """原子换名写整份快照。

        先把内容写进同目录下的临时文件，写完 fsync 再换名到目标位置。
        读的人按名字打开，要么拿到换名前的旧文件，要么拿到换名后的新文件，
        中间不会看到写了一半的内容——**被截断在 JSON 边界上的那份内容仍是
        合法 JSON** 这件事让「写坏」比「写没」难查得多。

        写盘失败不改变「登记已经成功」这个事实，所以只记一条日志。
        """
        target = self.snapshot_path
        if target is None:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(
                f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
            )
            try:
                with staged.open("w", encoding="utf-8") as handle:
                    json.dump(self.as_dict(), handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                _replace_with_retry(staged, target)
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
        except OSError:
            return None
        return target

    def load(self) -> bool:
        """从默认位置读快照，文件不存在或读不出来时返回 False。"""
        target = self.snapshot_path
        if target is None or not target.is_file():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        self.load_snapshot(data)
        return True

    def _restore_counters(
        self, raw: Any, entries: dict[str, Entry]
    ) -> dict[str, int]:
        """恢复序号计数器，并按实际条目抬高下限。

        存档里的计数器写坏了也不能让后续登记撞号，所以取两者的较大值。
        """
        counters = dict.fromkeys(KINDS, 0)
        if isinstance(raw, dict):
            for kind in KINDS:
                value = raw.get(kind)
                if isinstance(value, int) and value >= 0:
                    counters[kind] = value
        for key, entry in entries.items():
            kind = entry.handle.kind
            counters[kind] = max(counters.get(kind, 0), entry.handle.seq)
        return counters

    def _restore_current(self, raw: Any) -> ReceptorHandle | None:
        """恢复当前受体，指向不存在的条目时留空。"""
        if raw is None:
            return None
        try:
            handle = _handle_from_raw(raw)
        except LedgerError:
            return None
        if not isinstance(handle, ReceptorHandle) or str(handle) not in self._entries:
            return None
        return handle

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _allocate(self, kind: Kind) -> Handle:
        if kind not in KINDS:
            raise LedgerError(f"未知的条目类型 {kind!r}，应当是 {' / '.join(KINDS)} 之一")
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return make_handle(kind, self._counters[kind])

    # ------------------------------------------------------------------
    # 容器协议
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        with self._lock:
            current = str(self._current_receptor) if self._current_receptor else "未设定"
            return f"<SessionLedger entries={len(self._entries)} current={current}>"


def _digest_line(entry: Entry, prefix: str = "") -> str:
    """索引里的一行：前缀 + handle + 一行摘要，整行不超过 `DIGEST_LINE_CHARS`。

    `prefix`（`- ` 或 `当前受体：`）算在长度里：按「摘要多长」卡的话，同一段
    文字在两种行里会占不一样宽，而模型看到的是一列。

    不复用 `Entry.brief()`：它把「来自 receptor#1」也写进去，而索引里的产物
    几乎都来自当前受体——那半句每个产物都要重复一遍，白占额度还挤掉摘要。
    """
    text = f"{prefix}{entry.handle}" + (f" · {entry.summary}" if entry.summary else "")
    if entry.file_state != "ok":
        text += f"（{_FILE_STATE_NOTES.get(entry.file_state, entry.file_state)}）"
    flat = " ".join(text.split())
    return flat if len(flat) <= DIGEST_LINE_CHARS else flat[: DIGEST_LINE_CHARS - 1] + "…"


def _meta_contains(meta: dict[str, Any], wanted: dict[str, Any] | None) -> bool:
    """meta 里是否含指定的键值对（浅比较）。"""
    if not wanted:
        return True
    return all(meta.get(key) == value for key, value in wanted.items())


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


def ledger_root() -> Path:
    """登记表快照的默认根目录，与状态文件的位置惯例一致。"""
    return Path.home() / ".opencode_tui" / "ledgers"


def default_snapshot_path(session_id: str) -> Path | None:
    """按会话编号给出默认快照路径；没有会话编号时返回 None（不写盘）。"""
    if not session_id:
        return None
    return ledger_root() / f"{session_id}.json"


def looks_like_ledger(value: object) -> bool:
    """粗检一个对象是否够格当登记表（注入前用，避免装配时传错东西）。

    只看两个核心方法，不做严格 isinstance——领域侧可能用自己继承的
    协议或任意鸭子类型实现。
    """
    return callable(getattr(value, "add", None)) and callable(getattr(value, "get", None))
