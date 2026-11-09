from __future__ import annotations

from pathlib import Path

from ..agent.ledger import LedgerError, fingerprint_path, parse_handle
from .models import (
    SUPERSEDE_MARKERS,
    SUPERSEDE_OVERLAP,
    MemoryEntry,
    MemoryError,
    MemoryKind,
)


def _bigrams(text: str) -> set[str]:
    """小写、去掉空白与标点之后，按相邻两个字切出的二元组集合。"""
    plain = "".join(char.lower() for char in text if char.isalnum())
    if len(plain) < 2:
        return {plain} if plain else set()
    return {plain[index : index + 2] for index in range(len(plain) - 1)}


def overlap(left: str, right: str) -> float:
    """两条记忆正文的字符二元组重合度，0～1。

    取代判定用它选「最相近的那一条」。同一份受体下可以同时存在多条互不相干
    的事实，「共用引用 + 带更新词」只能说明它们在说同一份文件。
    二元组不引入词典，代价是同义改写（「换成」与「改为」）算不出重合。
    """
    left_grams = _bigrams(left)
    right_grams = _bigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


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


class MemoryLifecycle:
    """写入之后的失效判定、引用对账与删除。

    这些方法都要 `MemoryStore` 的条目表、路径与写盘，单独成模块只是为了让
    写入、检索、失效三件事各有自己的文件；调用侧仍是 `store.foo(...)`。
    """

    def _supersede_targets(
        self,
        text: str,
        refs: list[str],
        supersedes: list[str] | None,
        now: float,
        kind: MemoryKind = MemoryKind.FACT,
    ) -> tuple[list[MemoryEntry], str]:
        """这次写入要取代哪些旧条目，以及按什么判据判的。

        - 显式给了编号：就按编号来，模型自己判断过哪条旧了。
        - 否则看「同主题」：同类型，新文本带更新词，再算正文重合度取最高的一条。
          只共用引用不够——同一份受体上可以同时有多条互不相干的事实；
          只带更新词也不够——「盒子改为 24A」与「受体改为 v2」共用一份引用，
          却是在说两件事。
        返回的判据写在**被取代的那一条**上，事后查账要回答的是它为什么失效。
        """
        if supersedes:
            chosen = [
                self.entries[item]
                for item in supersedes
                if item in self.entries
                and self.entries[item].is_live(now)
                and self.entries[item].kind is kind
            ]
            return chosen, "explicit"
        if kind is not MemoryKind.FACT:
            # 经验说的是「某种条件下会发生什么」，习惯说的是用户的长期要求。
            # 两者「文字相近 + 共用引用 + 带更新词」常常只是同一个条件下的另一种说法，
            # 自动作废会把一条仍然成立的教训抹掉。要换掉它们，写入的人明确点出编号。
            return [], ""
        if not any(marker in text for marker in SUPERSEDE_MARKERS):
            return [], ""
        # 隐式取代要求共用引用：没有引用的事实之间无从判断是不是同一件事。
        if not refs:
            return [], ""
        shared = set(refs)
        best: MemoryEntry | None = None
        best_score = 0.0
        for item in self.entries.values():
            if not item.is_live(now) or item.kind is not kind:
                continue
            if not (shared & set(item.refs)):
                continue
            score = overlap(text, item.text)
            if score > best_score:
                best, best_score = item, score
        if best is None or best_score < SUPERSEDE_OVERLAP:
            return [], ""
        return [best], f"overlap:{best_score:.2f}"

    def forget(self, entry_id: str) -> bool:
        """删掉一条记忆并写盘，返回是否删掉了。

        删掉的那条要从别人的冲突标记里一起去掉：留着的话，渲染时那行
        「与 mem_xxx 冲突」会指向一条已经不存在的记忆，读的人从界面和记忆文件里
        都查不到它。
        """
        if entry_id not in self.entries:
            return False
        del self.entries[entry_id]
        for entry in self.entries.values():
            if entry_id in entry.conflicts:
                entry.conflicts.remove(entry_id)
        self.save()
        return True

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
