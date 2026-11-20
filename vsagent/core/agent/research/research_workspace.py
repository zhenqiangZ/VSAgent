"""科研工作区：按根会话存一份对象表，父子会话共用。

存的是**对象与引用**，不是文件内容。结构与对接结果仍在 `runs/` 下，这里只记
"有哪些对象、各自引用哪个 handle 或文件、是谁在哪一步登记的"。

三件事决定了这个模块的形状：

- **按家族存，不按会话存。** 子 Agent 与父会话是同一件事的两个执行者：结构
  Agent 登记的口袋，机制 Agent 要能按同一个 ID 找到。各存一份的话，两边会用
  同一个 ID 指向两条不同的记录。
- **落盘沿用登记表的做法**：整份快照、原子换名、读坏了跳过坏条目。科研对象
  不像 handle 那样每一步都被引用，但丢一条会让"这个结论的依据是哪条证据"
  断掉，所以读的时候宁可少一条，也不要整份读不出来。
- **写盘失败不影响内存里的登记。** 与登记表同一条理由：这一刻的事实已经成立，
  落盘是下一件事；把异常抛给调用方只会让它以为对象没登记上。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from vsagent.core import atomic
from vsagent.core.paths import config_home
from vsagent.core.agent.research.research_objects import (
    KINDS,
    ObjectError,
    ResearchObject,
    make_id,
    split_id,
)

SNAPSHOT_VERSION = 1
DIGEST_LIMIT = 12
"""工作区索引最多几行。与登记表索引同一个理由：它每一步都要发。"""


def research_root() -> Path:
    return config_home() / "research"


def default_path(session_id: str) -> Path | None:
    if not session_id:
        return None
    return research_root() / f"{session_id}.json"


class ResearchWorkspace:
    """一份对象表。同一个文件可以有多个实例，但同一时刻只该有一个在写。"""

    def __init__(
        self,
        *,
        session_id: str = "",
        path: Path | None = None,
    ) -> None:
        self.session_id = session_id
        self.path = path or default_path(session_id)
        self._objects: dict[str, ResearchObject] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def load(self) -> bool:
        """从磁盘读一份。文件不存在或读不出来返回 False（当作空表）。"""
        target = self.path
        if target is None or not target.is_file():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        with self._lock:
            self._objects = {}
            for raw in data.get("objects") or []:
                if not isinstance(raw, dict):
                    continue
                try:
                    obj = ResearchObject.from_dict(raw)
                except (ObjectError, KeyError, TypeError, ValueError):
                    # 一条坏记录不该废掉整份索引：跳过它，剩下的照样能用。
                    continue
                self._objects[obj.id] = obj
        return True

    def save(self) -> Path | None:
        """原子换名写整份快照。失败只返回 None，不抛。"""
        target = self.path
        if target is None:
            return None
        with self._lock:
            payload = {
                "version": SNAPSHOT_VERSION,
                "session_id": self.session_id,
                "objects": [obj.to_dict() for obj in self._objects.values()],
            }
        try:
            atomic.write_text(
                target, json.dumps(payload, ensure_ascii=False, indent=1)
            )
        except OSError:
            return None
        return target

    # ------------------------------------------------------------------
    # 登记与查询
    # ------------------------------------------------------------------
    def register(
        self,
        kind: str,
        key: str,
        *,
        label: str = "",
        source: str = "",
        refs: Iterable[str] = (),
        evidence_class: str = "",
        supports: Iterable[str] = (),
        refutes: Iterable[str] = (),
        task_id: str = "",
        agent_role: str = "",
    ) -> ResearchObject:
        """登记一个对象。**同一个身份键再登记一次是更新，不是新增。**

        更新时只做三件事：追加新引用、补空字段、版本加一。已有的标签与来源
        不覆盖——先登记的那次通常最接近事实（比如 PDB 标题），后一次的
        "顺手起个名"不该把它冲掉。
        """
        obj_id = make_id(kind, key)
        fresh = tuple(str(ref).strip() for ref in refs if str(ref).strip())
        support = tuple(str(item).strip() for item in supports if str(item).strip())
        refute = tuple(str(item).strip() for item in refutes if str(item).strip())
        with self._lock:
            current = self._objects.get(obj_id)
            if current is None:
                obj = ResearchObject(
                    id=obj_id,
                    kind=kind,
                    key=key.strip() if kind not in ("structure", "target") else key.strip().upper(),
                    label=label.strip(),
                    source=source.strip(),
                    evidence_class=evidence_class.strip(),
                    supports=support,
                    refutes=refute,
                    refs=fresh,
                    task_id=task_id.strip(),
                    agent_role=agent_role.strip(),
                )
                self._objects[obj_id] = obj
                return obj
            if evidence_class and current.evidence_class and evidence_class != current.evidence_class:
                raise ObjectError(
                    f"{obj_id} 已经按 {current.evidence_class} 登记，"
                    f"不能再按 {evidence_class} 记：同一份证据只能有一个等级"
                )
            merged = current.with_refs(fresh, source=source.strip())
            if label.strip() and not merged.label:
                merged = _replace(merged, label=label.strip())
            if evidence_class.strip() and not merged.evidence_class:
                merged = _replace(merged, evidence_class=evidence_class.strip())
            if support or refute:
                merged = _replace(
                    merged,
                    supports=_merge_texts(merged.supports, support),
                    refutes=_merge_texts(merged.refutes, refute),
                )
            if task_id.strip() and not merged.task_id:
                merged = _replace(merged, task_id=task_id.strip())
            if agent_role.strip() and not merged.agent_role:
                merged = _replace(merged, agent_role=agent_role.strip())
            self._objects[obj_id] = merged
            return merged

    def get(self, obj_id: str) -> ResearchObject | None:
        with self._lock:
            return self._objects.get(str(obj_id or "").strip())

    def find(self, kind: str | None = None) -> list[ResearchObject]:
        """按类别列对象，按登记时间升序。类别不认识时抛错，不返回空表。"""
        if kind is not None and kind not in KINDS:
            raise ObjectError(f"不认识的类别：{kind}")
        with self._lock:
            items = [
                obj for obj in self._objects.values() if kind is None or obj.kind == kind
            ]
        return sorted(items, key=lambda obj: (obj.created_at, obj.id))

    def link(self, obj_id: str, *refs: str) -> ResearchObject:
        """给已有对象追加引用（handle 或文件路径）。对象不存在时抛错。"""
        with self._lock:
            current = self._objects.get(str(obj_id or "").strip())
            if current is None:
                raise ObjectError(f"工作区里没有这个对象：{obj_id}")
            merged = current.with_refs(tuple(str(ref) for ref in refs))
            self._objects[current.id] = merged
            return merged

    def retire(self, obj_id: str) -> ResearchObject:
        """标记为不再使用。**不删**：结论里引用过它，删掉就断链了。"""
        with self._lock:
            current = self._objects.get(str(obj_id or "").strip())
            if current is None:
                raise ObjectError(f"工作区里没有这个对象：{obj_id}")
            retired = current.retired()
            self._objects[current.id] = retired
            return retired

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for obj in self.find():
            out[obj.kind] = out.get(obj.kind, 0) + 1
        return out

    def __len__(self) -> int:
        return len(self._objects)

    def digest(self, limit: int = DIGEST_LIMIT) -> str:
        """工作区索引：一行一个对象。没有对象时返回空串，不留一句废话。"""
        items = [obj for obj in self.find() if obj.status == "active"]
        if not items:
            return ""
        lines = [obj.line() for obj in items[:limit]]
        if len(items) > limit:
            lines.append(f"另有 {len(items) - limit} 个对象，用工作区工具查")
        return "\n".join(lines)


def _merge_texts(current: Iterable[str], fresh: Iterable[str]) -> tuple[str, ...]:
    """追加去重，保持原有顺序。"""
    out = list(current)
    for item in fresh:
        if item and item not in out:
            out.append(item)
    return tuple(out)


def _replace(obj: ResearchObject, **changes: Any) -> ResearchObject:
    """补字段并升一版。只在值真的变了时升版本。"""
    from dataclasses import replace as dataclass_replace

    changed = {key: value for key, value in changes.items() if getattr(obj, key) != value}
    if not changed:
        return obj
    return dataclass_replace(
        obj, version=obj.version + 1, updated_at=time.time(), **changed
    )


def workspace_for(session: Any) -> ResearchWorkspace:
    """按会话取工作区。子 agent 与父会话共用根会话那一份。"""
    family = getattr(session, "family_id", "") or getattr(session, "id", "")
    workspace = ResearchWorkspace(session_id=family)
    workspace.load()
    return workspace


def check_id(obj_id: str) -> tuple[str, str]:
    """外部给的对象 ID 先过一遍，错误信息比 `KeyError` 好读。"""
    return split_id(obj_id)
