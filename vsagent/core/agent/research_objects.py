"""科研对象：结构、口袋、配体、位姿、评分、证据、假设、报告。

**对象不是登记表的第四种 handle。** 登记表管的是"这一轮算出来的东西在哪"
（`receptor#1`、`ligandset#2`、`artifact#3`），它回答"用哪个"；科研对象管的是
"我们在研究什么"，它回答"这是不是同一个东西"。两者用 `refs` 连起来：
一个 `structure:4HJO` 可以引用 `receptor#1` 这个 handle，反过来不成立。

**ID 由身份决定，不由注册顺序决定。** 这是"同一结构、口袋、配体在不同 Agent
之间用同一个 ID"的实现方式：`structure:4HJO` 无论是结构 Agent 还是机制 Agent
登记，都是同一个字符串。用自增编号做不到这一点——两个 Agent 各自从 1 开始数，
得到两个不同的号，说的是同一个晶体结构。

身份键要挑**稳定且唯一**的那一个：

- 结构：PDB 编号（`4HJO`）；
- 靶点：UniProt 登录号（`O00141`）；
- 配体：ChEMBL / PubChem 编号，没有编号时才用规范化 SMILES；
- 口袋：受体 ID + 口袋序号（`receptor#1:p1`）——口袋没有全局身份，
  只有"在哪个受体上的第几个"；
- 位姿 / 评分：由输入身份拼出来（`ligand:CHEMBL1@structure:4HJO`）。

**大文件不进对象。** 结构坐标、对接结果留在 `runs/` 下，对象里只放引用。
把几兆的坐标塞进对象文件，下一次读它就要几秒，而读它发生在每一步。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

KINDS: tuple[str, ...] = (
    "target",
    "structure",
    "pocket",
    "ligand",
    "pose",
    "score",
    "evidence",
    "hypothesis",
    "report",
)

KIND_LABELS: dict[str, str] = {
    "target": "靶点",
    "structure": "结构",
    "pocket": "口袋",
    "ligand": "配体",
    "pose": "位姿",
    "score": "评分",
    "evidence": "证据",
    "hypothesis": "假设",
    "report": "报告",
}

EVIDENCE_LABELS: dict[str, str] = {
    "experimental_activity": "实测活性",
    "experimental_structure": "实验结构",
    "database": "数据库记录",
    "literature": "文献证据",
    "user_data": "用户提供",
    "prediction": "计算预测",
    "inference": "模型推断",
}
"""证据类别的中文名。放在这里而不是证据模块里：对象自己的一行摘要要用它，
而证据模块反过来依赖对象。谁用谁依赖，别绕回去。"""

STATUSES: tuple[str, ...] = ("active", "retired")
"""对象只有两种状态：还在用、已经不用了。

不设 `draft` / `confirmed`：那是**任务**的阶段，不是对象的属性。一个结构被选成
当前受体之后仍然是同一个结构对象，变的是会话上的当前受体，不是它自己。
"""


class ObjectError(ValueError):
    """对象的身份键或类别不合法。"""


def normalize_key(kind: str, key: str) -> str:
    """把身份键规整成可比较的形式。

    结构编号与登录号按大写存：`4hjo` 与 `4HJO` 是同一个结构，存成两种写法之后
    "同一个东西"这件事就要靠调用方记得统一大小写，迟早漏一次。
    """
    text = str(key or "").strip()
    if not text:
        raise ObjectError(f"{kind} 的身份键不能为空")
    if kind in ("structure", "target"):
        return text.upper()
    return text


def make_id(kind: str, key: str) -> str:
    """对象的稳定 ID：`类别:身份键`。"""
    if kind not in KINDS:
        raise ObjectError(f"不认识的类别：{kind}。可用：{'、'.join(KINDS)}")
    return f"{kind}:{normalize_key(kind, key)}"


def split_id(obj_id: str) -> tuple[str, str]:
    """把 ID 拆回（类别, 身份键）。只按第一个冒号拆：身份键里可以有冒号。"""
    kind, _, key = str(obj_id or "").partition(":")
    if kind not in KINDS or not key:
        raise ObjectError(f"不是合法的对象 ID：{obj_id}")
    return kind, key


@dataclass(frozen=True)
class ResearchObject:
    """一个科研对象。不可变：更新走 `with_refs` / `renamed`，返回新对象。"""

    id: str
    kind: str
    key: str
    label: str = ""
    # 谁产出的：工具名、数据库、文献出处。不是"哪一步"，那是 task_id。
    source: str = ""
    # 只有 evidence 类填；取值见 `evidence_registry.CLASSES`。
    evidence_class: str = ""
    # 只有 evidence 类填：这条证据支持 / 反驳哪些假设（`hypothesis:<id>`）。
    # 方向记在证据上而不是假设上，是因为同一条证据可能同时支持一条、反驳另一条，
    # 记在假设那一侧就要在两处各写一遍，迟早写漏一处。
    supports: tuple[str, ...] = ()
    refutes: tuple[str, ...] = ()
    # 指向登记表 handle 或 runs/ 下的文件。**不放大文件内容。**
    refs: tuple[str, ...] = ()
    task_id: str = ""
    agent_role: str = ""
    status: str = "active"
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ObjectError(f"不认识的类别：{self.kind}")
        if self.status not in STATUSES:
            raise ObjectError(f"不认识的状态：{self.status}")
        if self.id != make_id(self.kind, self.key):
            raise ObjectError(
                f"ID 与身份键对不上：{self.id} 应当是 {make_id(self.kind, self.key)}"
            )
        if self.kind == "evidence" and not self.evidence_class:
            raise ObjectError("证据对象必须写明证据类别（experimental_activity 之类）")
        if self.kind != "evidence" and self.evidence_class:
            raise ObjectError(f"{self.kind} 不是证据，不该有证据类别")
        if self.kind != "evidence" and (self.supports or self.refutes):
            raise ObjectError(f"{self.kind} 不是证据，不该挂支持或反驳关系")
        for target in (*self.supports, *self.refutes):
            # 两种写法都认：工作区里的假设对象用 `hypothesis:<键>`，会话任务状态里的
            # 用 `hyp#1`。它们是同一条假设的两种登记处，强行统一会让一边写不进去。
            if not (
                str(target).startswith("hypothesis:")
                or re.fullmatch(r"hyp#\d+", str(target))
            ):
                raise ObjectError(f"支持/反驳只能指向假设，收到的是：{target}")

    @property
    def label_or_id(self) -> str:
        return self.label or self.id

    def with_refs(self, refs: tuple[str, ...], *, source: str = "") -> "ResearchObject":
        """追加引用并升一版。引用去重、保持顺序。"""
        merged = list(self.refs)
        for ref in refs:
            text = str(ref or "").strip()
            if text and text not in merged:
                merged.append(text)
        changed = tuple(merged) != self.refs or (source and source != self.source)
        if not changed:
            return self
        return replace(
            self,
            refs=tuple(merged),
            source=source or self.source,
            version=self.version + 1,
            updated_at=time.time(),
        )

    def retired(self) -> "ResearchObject":
        if self.status == "retired":
            return self
        return replace(self, status="retired", version=self.version + 1, updated_at=time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "source": self.source,
            "evidence_class": self.evidence_class,
            "supports": list(self.supports),
            "refutes": list(self.refutes),
            "refs": list(self.refs),
            "task_id": self.task_id,
            "agent_role": self.agent_role,
            "status": self.status,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchObject":
        kind = str(data.get("kind") or "")
        key = str(data.get("key") or "")
        return cls(
            id=str(data.get("id") or make_id(kind, key)),
            kind=kind,
            key=key,
            label=str(data.get("label") or ""),
            source=str(data.get("source") or ""),
            evidence_class=str(data.get("evidence_class") or ""),
            supports=tuple(str(item) for item in data.get("supports") or ()),
            refutes=tuple(str(item) for item in data.get("refutes") or ()),
            refs=tuple(str(item) for item in data.get("refs") or ()),
            task_id=str(data.get("task_id") or ""),
            agent_role=str(data.get("agent_role") or ""),
            status=str(data.get("status") or "active"),
            version=int(data.get("version") or 1),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def line(self) -> str:
        """一行摘要。给工作区索引与工具输出用。"""
        head = f"{self.id}（{KIND_LABELS.get(self.kind, self.kind)}"
        if self.evidence_class:
            head += f"·{EVIDENCE_LABELS.get(self.evidence_class, self.evidence_class)}"
        head += "）"
        bits = [bit for bit in (self.label, self.source) if bit]
        if self.refs:
            bits.append("、".join(self.refs[:3]))
        return head + ("：" + "，".join(bits) if bits else "")
