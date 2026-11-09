from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..file_log import record as log_read
from .lifecycle import MemoryLifecycle, _reject_handle
from .models import (
    MAX_ENTRIES,
    MAX_PREFERENCES,
    MEMORY_TEXT_LIMIT,
    REPLACE_ATTEMPTS,
    REPLACE_RETRY_DELAY,
    SNAPSHOT_VERSION,
    MemoryEntry,
    MemoryError,
    MemoryKind,
    memory_root,
    new_memory_id,
)
from .retriever import MemoryRetriever


class MemoryStore(MemoryRetriever, MemoryLifecycle):
    """项目级记忆的读写与检索。

    记忆是**跨会话**的知识台账，与登记表（会话内的工作集）分工不同：
    登记表记「这次会话手上有哪些产物」，记忆记「这个项目已知什么」。
    因此记忆里的引用只能是路径一类稳定标识，不能是会话内的 handle。

    三个模块分工：本文件管载入、写盘与容量；`retriever` 管召回与渲染；
    `lifecycle` 管失效判定与引用对账。
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
        kind: MemoryKind = MemoryKind.FACT,
        source: str = "agent",
        expires_at: float = 0.0,
        supersedes: list[str] | None = None,
        conflicts: list[str] | None = None,
        session_id: str = "",
        turn: int = 0,
        ttl_days: float = 0.0,
    ) -> MemoryEntry:
        """写入一条记忆并写盘，返回写进去的那一条。

        写入不是「追加一行」那么简单，四道处理按顺序来：

        1. **精确去重**：同一句话再写一次只更新时间，不新建 id。模型读到自己
           刚写过的内容又"想起来"时很常见，不去重的话台账很快就全是重复。
           类型算在判重键里：同一句文字可以作为习惯与事实各存一条。
        2. **引用校验**：agent 写入的引用必须存在；用户写入的允许不存在（标
           `unverified`，注入时排在后面）。能先确认路径的一方就该先确认。
           用户习惯没有对应文件，这一道对它不适用。
        3. **同主题失效**：显式给了 `supersedes`，或新文本带「改为 / 不再 / 替换」
           这类更新词且与旧条目共用引用时，**最多**把最相近的一条标 `valid_to`，
           不硬删。几条互不相干的事实共用一个引用时，只让最像的那条失效。
           取代只在同类型之间发生。
        4. **冲突标记**：`conflicts` 里的编号写成双向关系，两条都保持有效——
           哪一条可信要交给模型结合证据判断，程序不替它选。
        5. **容量**：超过 `MAX_ENTRIES` 时把最旧、已失效的挪进归档文件；
           用户习惯另有 `MAX_PREFERENCES` 的条数上限。
        """
        cleaned = " ".join(text.split())
        if not cleaned:
            raise MemoryError("记忆内容不能为空")
        if len(cleaned) > MEMORY_TEXT_LIMIT:
            raise MemoryError(
                f"一条记忆最多 {MEMORY_TEXT_LIMIT} 字，当前 {len(cleaned)} 字；"
                "请分成几条各自成立的事实"
            )
        cleaned_refs = [str(item).strip() for item in (refs or []) if str(item).strip()]
        for ref in cleaned_refs:
            _reject_handle(ref)
        now = time.time()
        if ttl_days > 0 and not expires_at:
            expires_at = now + ttl_days * 86400
        conflict_ids = [str(item).strip() for item in (conflicts or []) if str(item).strip()]
        unknown = [
            item
            for item in conflict_ids
            if item not in self.entries or not self.entries[item].is_live(now)
        ]
        if unknown:
            raise MemoryError(
                "冲突标记指向不存在或已失效的记忆：" + "、".join(unknown)
            )

        existing = next(
            (
                item
                for item in self.entries.values()
                if item.text == cleaned and item.kind is kind
            ),
            None,
        )
        if existing is not None and existing.is_live(now):
            existing.updated_at = now
            for ref in cleaned_refs:
                if ref not in existing.refs:
                    existing.refs.append(ref)
            existing.fingerprint = self._fingerprint_for(existing.refs)
            self._link_conflicts(existing, conflict_ids)
            self.save()
            return existing

        unverified = False
        if kind is not MemoryKind.PREFERENCE:
            missing = [ref for ref in cleaned_refs if self._first_existing([ref]) is None]
            if missing:
                if source != "user":
                    raise MemoryError(
                        "引用的文件不存在：" + "、".join(missing) + "。请写真实存在的路径，"
                        "或者不写引用。"
                    )
                unverified = True

        if supersedes:
            wrong = [
                self.entries[item]
                for item in supersedes
                if item in self.entries and self.entries[item].kind is not kind
            ]
            if wrong:
                detail = "、".join(f"{item.id}（{item.kind.value}）" for item in wrong)
                raise MemoryError(
                    f"不能跨类型取代：{detail} 与这次写入的类型不同。"
                    "要删掉旧的那条用 forget。"
                )

        targets, rule = self._supersede_targets(
            cleaned, cleaned_refs, supersedes, now, kind
        )
        if kind is MemoryKind.PREFERENCE:
            doomed = {item.id for item in targets}
            remaining = [
                item
                for item in self.entries.values()
                if item.kind is MemoryKind.PREFERENCE
                and item.is_live(now)
                and item.id not in doomed
            ]
            if len(remaining) >= MAX_PREFERENCES:
                raise MemoryError(
                    f"用户习惯最多 {MAX_PREFERENCES} 条，当前已有 {len(remaining)} 条；"
                    "请先用 forget 删掉旧的，或把新要求并进已有的那一条。"
                )
        entry = MemoryEntry(
            id=new_memory_id(cleaned),
            kind=kind,
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
            target.superseded_rule = rule
        self.entries[entry.id] = entry
        self._link_conflicts(entry, conflict_ids)
        self._archive_overflow()
        self.save()
        return entry

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

    def _link_conflicts(self, entry: MemoryEntry, conflicts: list[str]) -> None:
        """把冲突关系写成双向的。

        只记在一条上的话，从另一条看过去「它和我矛盾」这件事就丢了；
        而模型读到的往往是任意一条，两边都得能看见对方。
        """
        for item in conflicts:
            if item == entry.id:
                continue
            if item not in entry.conflicts:
                entry.conflicts.append(item)
            other = self.entries[item]
            if entry.id not in other.conflicts:
                other.conflicts.append(entry.id)

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

    def archived_entries(self) -> list[MemoryEntry]:
        """归档里的条目，按写入顺序。读不懂的跳过，与 `load_snapshot` 同一口径。"""
        entries: list[MemoryEntry] = []
        for raw in self._read_archive():
            try:
                entry = MemoryEntry.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if entry.text:
                entries.append(entry)
        return entries

    def archive(self, entry_id: str) -> MemoryEntry:
        """把一条挪进归档文件。**不删内容**，`restore` 能原样取回。

        与 `_archive_overflow` 的差别只在触发方式：那个在容量超限时自动做，
        这个由用户点名。两步的写盘顺序是「先写归档、再删条目」——中途失败时
        条目还在，多一份副本比少一条记忆安全。
        """
        entry = self.entries.get(entry_id)
        if entry is None:
            raise MemoryError(f"没有这条记忆：{entry_id}")
        archive = self._read_archive()
        if any(item.get("id") == entry_id for item in archive):
            raise MemoryError(f"归档里已经有这一条：{entry_id}")
        archive.append(entry.to_dict())
        self._atomic_write(
            self.archive_path,
            json.dumps({"entries": archive}, ensure_ascii=False, indent=2),
        )
        del self.entries[entry_id]
        self.save()
        return entry

    def restore(self, entry_id: str) -> MemoryEntry:
        """把归档里的一条取回。条目已存在或容量已满时拒绝。

        写盘顺序与 `archive` 相反：先把条目写进 `entries.json`，再改归档文件。
        中途失败时两边都有这一条，`restore` 再点一次会明确说「已经在记忆里」，
        不会静默丢掉。
        """
        if entry_id in self.entries:
            raise MemoryError(f"这条已经在记忆里了：{entry_id}")
        if len(self.entries) >= MAX_ENTRIES:
            raise MemoryError(
                f"记忆已经有 {len(self.entries)} 条，上限 {MAX_ENTRIES}；"
                "先归档几条再恢复"
            )
        archive = self._read_archive()
        found = next((item for item in archive if item.get("id") == entry_id), None)
        if found is None:
            raise MemoryError(f"归档里没有这一条：{entry_id}")
        entry = MemoryEntry.from_dict(found)
        self.entries[entry.id] = entry
        self.save()
        self._atomic_write(
            self.archive_path,
            json.dumps(
                {"entries": [item for item in archive if item.get("id") != entry_id]},
                ensure_ascii=False,
                indent=2,
            ),
        )
        return entry

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
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
