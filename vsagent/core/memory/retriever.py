from __future__ import annotations

import math
import time

from ..agent.prompt import estimate_tokens
from ..rrf import RRF_K
from ..text import terms
from .models import (
    DECAY_DAYS,
    EXPERIENCE_DECAY_DAYS,
    FILE_STATE_NOTES,
    KIND_NAMES,
    MAX_PREFERENCES,
    PREFERENCE_LIMIT,
    RESIDENT_LIMIT,
    MemoryEntry,
    MemoryKind,
)

# 与当前任务相关的条目加分。引用路径相同或互为开头，说明「这条讲的就是手上这份文件」；
# 对不上不罚——一条关于别处经验的记忆仍可能有用，只是该排在更贴切的后面。
APPLICABILITY_BOOST = 1.2
# 只靠语义命中的条目，余弦分不能比这一问的最高分低太多。
#
# 余弦没有「不相关」这个值：台账只有两条时，随便一句都会有一个不低的相似度
# （「受体」对「配体库在 ligands.sdf」是 0.411，最高分是 0.567）。按最高分的
# 八成卡一道，把「勉强沾边」挡在结果之外；同时出现在字面那一路的条目不受这条限制——
# 它已经有字面证据，不需要语义单独担保。
SEMANTIC_MARGIN = 0.8


class MemoryRetriever:
    """召回、打分、常驻注入与渲染。

    都是 `MemoryStore` 的方法，单独成模块只为让检索与写入各占一个文件；
    调用侧仍是 `store.recall(...)`。
    """

    def recall(
        self,
        query: str = "",
        *,
        limit: int = 20,
        touch: bool = False,
        kind: MemoryKind | None = None,
        model: str = "",
        context_paths: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """按关键词与语义两路检索；空查询按更新时间排。

        两路各自排名，再按名次相加（RRF）：`Σ 1/(60 + 名次)`。名次与分数不同，
        余弦分与字面命中数没有共同的量纲，直接相加会让其中一路凭量级压过另一路。
        两路都靠前的条目分最高；只在其中一路出现的条目仍有机会，
        同义改写（「残基」与「氨基酸」）就靠这一条被召回。

        先前是「字面一条都没命中才走语义」：那是二选一，字面碰上一个字就
        不再看意思，改写问法的召回只有 15%。

        `context_paths` 是当前受体与当前产物的路径。条目引用与之相同或互为
        上级目录时乘 1.2，让「讲的就是手上这份文件」的条目排在前面。

        `touch` 只有**显式 recall** 才打开：常驻注入内部也会调它，
        那条路径如果也算「用过」，所有条目都会永远算「最近用过」。
        """
        scored = self.recall_scored(
            query,
            limit=limit,
            kind=kind,
            model=model,
            context_paths=context_paths,
        )
        hits = [entry for entry, _ in scored]
        if touch:
            now = time.time()
            for entry in hits:
                entry.last_used_at = now
        return hits

    def recall_scored(
        self,
        query: str = "",
        *,
        limit: int = 20,
        kind: MemoryKind | None = None,
        model: str = "",
        context_paths: list[str] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        """与 `recall` 同一套排序，多带一个分数。

        分数就是排序用的那个数：两路 RRF 之和乘上类型、可信度、时间与贴题程度。
        它没有绝对量纲，只在这一次查询内部可比；网页上按名次展示，不折算成百分比。
        空查询不参与相关度，分数一律给 0。
        """
        now = time.time()
        words = terms(query.strip().lower())
        allowed = [
            entry
            for entry in self.entries.values()
            if entry.is_live(now)
            and (kind is None or entry.kind is kind)
            # 习惯不进相关度：它的价值与这次问的是哪个靶点无关。
            and (not words or entry.kind is not MemoryKind.PREFERENCE)
        ]
        if not words:
            hits = sorted(allowed, key=lambda item: -item.updated_at)[:limit]
            return [(entry, 0.0) for entry in hits]

        lexical = {
            entry.id: self._score(entry, words, now, context_paths)
            for entry in allowed
        }
        lexical = {key: value for key, value in lexical.items() if value > 0}
        lexical_ranked = sorted(
            lexical,
            key=lambda item: (-lexical[item], -self.entries[item].updated_at),
        )
        semantic = self.semantic_scores(query, model) if model else {}
        semantic = {key: value for key, value in semantic.items() if key in {item.id for item in allowed}}
        if semantic:
            margin = max(semantic.values()) * SEMANTIC_MARGIN
            semantic = {
                key: value
                for key, value in semantic.items()
                if key in lexical or value >= margin
            }
        semantic_ranked = sorted(
            semantic,
            key=lambda item: (-semantic[item], -self.entries[item].updated_at),
        )

        fused: dict[str, float] = {}
        for rank, entry_id in enumerate(lexical_ranked, start=1):
            fused[entry_id] = fused.get(entry_id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, entry_id in enumerate(semantic_ranked, start=1):
            fused[entry_id] = fused.get(entry_id, 0.0) + 1.0 / (RRF_K + rank)

        scored = [
            (
                self.entries[entry_id],
                value * self._multiplier(self.entries[entry_id], now, context_paths),
            )
            for entry_id, value in fused.items()
        ]
        scored.sort(key=lambda pair: (-pair[1], -pair[0].updated_at))
        return scored[:limit]

    def semantic_scores(self, query: str, model: str) -> dict[str, float]:
        """这一问在每个条目上的余弦分。模型不可用时返回空表。

        向量是派生物，存在 `.vsmemory/vectors.db`，按内容指纹对账：
        改了正文的条目重算，没改的一次也不编码。
        """
        from .. import embed
        from . import vectors

        records = [
            (entry.id, entry.text)
            for entry in self.entries.values()
            if entry.is_live()
        ]
        if not records:
            return {}
        conn = vectors.connect(self.root)
        try:
            vectors.sync(
                conn,
                records,
                model,
                lambda texts: embed.encode_passages(texts, model),
            )
            hits = vectors.search(
                conn,
                embed.encode_query(query, model),
                model=model,
                limit=len(records),
                min_score=embed.min_score(),
            )
        finally:
            conn.close()
        return dict(hits)

    def _score(
        self,
        entry: MemoryEntry,
        words: list[str],
        now: float,
        context_paths: list[str] | None = None,
    ) -> float:
        """一条记忆对这次查询的字面得分。空查询不打分，一律 1。

        用户习惯不进相关度：它的价值与这次问的是哪个靶点无关，进相关度会被
        任何一个偶然命中的关键词顶上去，而它本来就应该一直在。
        """
        if not words:
            return 1.0
        if entry.kind is MemoryKind.PREFERENCE:
            return 0.0
        text = entry.text.lower()
        refs = [ref.lower() for ref in entry.refs]
        score = 0.0
        for word in words:
            if word in text:
                score += 2.0
            if any(word in ref for ref in refs):
                score += 1.0
        return score * self._multiplier(entry, now, context_paths)

    def _multiplier(
        self,
        entry: MemoryEntry,
        now: float,
        context_paths: list[str] | None = None,
    ) -> float:
        """与查询无关的折扣与加成：类型、可信度、时间、是否贴着当前任务。

        两路合并之后乘在这里：名次决定相关性，这些因素决定同一条记忆
        在这个项目里有多可信、多新、多贴题。
        """
        factor = 1.0
        if entry.kind is MemoryKind.EXPERIENCE:
            # 失败换来的判断值得看，但它说的是「某种条件下会发生什么」，
            # 直接用它会让人把一次失败当成普遍规律。
            factor *= 0.8
        # 引用不存在的条目已经由 confidence=0.5 表达，不再另乘一次：
        # 同一条事实判两遍，口述的内容会掉到几乎看不见。
        factor *= 0.5 + 0.5 * entry.confidence
        if context_paths and self._applies(entry, context_paths):
            factor *= APPLICABILITY_BOOST
        half_life = (
            EXPERIENCE_DECAY_DAYS
            if entry.kind is MemoryKind.EXPERIENCE
            else DECAY_DAYS
        )
        age_days = max(0.0, (now - entry.updated_at) / 86400)
        return factor * math.exp(-age_days / half_life)

    def _applies(self, entry: MemoryEntry, context_paths: list[str]) -> bool:
        """这条记忆引用的文件是不是当前任务正在用的那一个。

        `refs` 允许写相对路径，当前路径来自登记表，两边都按项目根解释之后
        再比。相同或互为上级目录都算——「runs/1iep/receptor.pdb」与
        「runs/1iep」讲的是同一份材料。
        """
        if not entry.refs:
            return False
        wanted = [self._resolve(path) for path in context_paths if str(path).strip()]
        for ref in entry.refs:
            path = self._resolve(ref)
            for item in wanted:
                if path == item or path in item.parents or item in path.parents:
                    return True
        return False

    def _ordered(self) -> list[MemoryEntry]:
        """按更新时间倒序，最近的排前面。"""
        return sorted(
            self.entries.values(), key=lambda item: item.updated_at, reverse=True
        )

    def resident_text(self, limit: int = RESIDENT_LIMIT, query: str = "") -> str:
        """常驻注入的正文：用户习惯一段 + 项目情况一段。

        两段分开是有原因的：习惯只在用户改口时变化，项目情况随问题变化。
        合成一段的话，问题一变整段就变；装不下时还会整块对半砍，
        用户的固定要求可能先被砍掉。
        """
        profile = self.profile_text()
        room = max(0, limit - estimate_tokens(profile))
        project = self.project_text(room, query)
        return "\n".join(item for item in (profile, project) if item)

    def profile_text(self, limit: int = PREFERENCE_LIMIT) -> str:
        """用户习惯那一段：不参与相关度，按更新时间取最近的几条。

        上限 `PREFERENCE_LIMIT` 与 `MAX_PREFERENCES` 同时管：习惯每一条都要
        常驻，攒到几十条时这一段会挤掉项目事实，而那时一半内容与当前任务无关。
        """
        now = time.time()
        prefs = sorted(
            (
                entry
                for entry in self.entries.values()
                if entry.is_live(now) and entry.kind is MemoryKind.PREFERENCE
            ),
            key=lambda item: -item.updated_at,
        )
        lines: list[str] = []
        used = 0
        dropped = 0
        for entry in prefs[:MAX_PREFERENCES]:
            line = self.render_line(entry)
            cost = estimate_tokens(line)
            if used + cost > limit:
                dropped += 1
                continue
            lines.append(line)
            used += cost
        if dropped:
            lines.append(
                f"（另有 {dropped} 条习惯未注入，用 recall(kind=preference) 取）"
            )
        return "\n".join(lines)

    def project_text(
        self,
        limit: int = RESIDENT_LIMIT - PREFERENCE_LIMIT,
        query: str = "",
        context_paths: list[str] | None = None,
    ) -> str:
        """项目情况那一段：核心事实 + 与当前问题相关的 + 最近更新的。

        三段按优先级拼，按 id 去重，填满 `limit` 为止：

        1. core：有引用、且近 30 天被显式 `recall` 命中过，最多 5 条；
        2. relevant：用当前问题检索出来的，最多 5 条；
        3. recent：最近更新的，最多 3 条；
        4. 还有余量就按「有引用优先、新的先」继续填。

        先前是「全部活条目按更新时间排，装不下就跳过」：内容一多，注入的永远是
        最近改过的那几条，与当前问题无关。相关度优先之后，同样 1200 token
        能命中的东西多得多；没被选中的仍可用 `recall` 取到。
        """
        if limit <= 0:
            return ""
        now = time.time()
        facts = [
            entry
            for entry in self.entries.values()
            if entry.is_live(now) and entry.kind is not MemoryKind.PREFERENCE
        ]
        if not facts:
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
            for entry in sorted(facts, key=lambda item: -item.last_used_at)
            if entry.refs and entry.last_used_at and now - entry.last_used_at <= recent_window
        ]
        take(core, 5)
        if query.strip():
            take(self.recall(query, limit=10, context_paths=context_paths), 5)
        # 最近更新的那几条，有引用的优先：引用指向项目里真实存在的产物，
        # 比一句没有凭据的陈述更值得占常驻额度。
        take(sorted(facts, key=lambda item: (not item.refs, -item.updated_at)), 3)
        take(sorted(facts, key=lambda item: (not item.refs, -item.updated_at)), len(facts))

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

    def render_line(self, entry: MemoryEntry) -> str:
        """一行记忆：类型标记、正文、引用、冲突与状态提醒。

        三类都带标记：注入时常驻正文里习惯、经验与事实混在一堆，
        模型分不清哪一条是在对它提要求、哪一条只是带条件的教训。
        记忆同时出现在 `/memory`、常驻注入与 `recall` 三处，
        标记只在渲染这一处加，三处看到的是同一份。
        """
        marker = {
            MemoryKind.FACT: "[事实] ",
            MemoryKind.PREFERENCE: "[习惯] ",
            MemoryKind.EXPERIENCE: "[经验] ",
        }[entry.kind]
        line = f"- {marker}{entry.text}"
        if entry.refs:
            line += f"（{entry.refs[0]}）"
        unresolved = [
            item
            for item in entry.conflicts
            if item in self.entries and self.entries[item].is_live()
        ]
        if unresolved:
            line += f"（与 {'、'.join(unresolved)} 冲突，未解决）"
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
        """`/memory` 用的两行：正文那一行 + 类型、来源、时间与状态。"""
        stamp = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(entry.updated_at or entry.created_at)
        )
        bits = [
            f"类型 {KIND_NAMES[entry.kind]}",
            f"来源 {entry.source or '未知'}",
            f"更新 {stamp}",
        ]
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
