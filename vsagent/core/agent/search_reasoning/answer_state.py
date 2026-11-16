"""答案状态的建立与存放。

完整对象按「会话 + 轮次」写成一份 JSON：`<状态目录>/answers/<会话编号>/<答案编号>.json`。
不写进 `state.toml`：那一份是整场会话的状态，一次回答的证据与评估动辄几十条，
混进去会让状态文件与启动时的解析都变慢。

事件只带 `answer_id` 与计数，正文从这里读。旧会话没有这个目录时按
「没有答案状态」处理，不补造一份。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from vsagent.core.paths import config_home

from . import prompts
from .models import AnswerState, Claim, SearchReasoningError
from .jsonio import Ask, parse_json

# 初始说法条数的上下限。少于三条说明这份回答还没有可核对的结构；
# 多于七条会把上下文与后续评估都撑开。
MIN_CLAIMS = 3
MAX_CLAIMS = 7


def new_answer_id() -> str:
    """生成一个答案编号。

    带时间前缀，目录里按文件名排序就是轮次顺序；后缀用随机段，
    同一秒内建立两份时不会撞号。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"ans_{stamp}_{uuid.uuid4().hex[:6]}"


def answers_directory(state_dir: Path | None = None) -> Path:
    """答案状态的根目录：`<状态目录>/answers`。"""
    return (state_dir or config_home()) / "answers"


def answer_path(state_dir: Path | None, session_id: str, answer_id: str) -> Path:
    """一份答案状态的文件位置。"""
    return answers_directory(state_dir) / session_id / f"{answer_id}.json"


def create_state(
    question: str,
    mode: str = "rag",
    *,
    initial_answer: str = "",
    claims: list[Claim] | None = None,
    gaps: list[str] | None = None,
    answer_id: str = "",
    baseline_source: str = "prior",
    trigger_tool: str = "",
    trigger_step: int = 0,
) -> AnswerState:
    """按一次模型输出建立答案状态。

    说法状态一律写成 `initial`：这一步还没有任何证据，先把框架立起来，
    后面的检索结果只负责改它，不负责替它。
    """
    state = AnswerState(
        answer_id=answer_id or new_answer_id(),
        question=question,
        mode=mode,
        baseline_source=baseline_source,
        trigger_tool=trigger_tool,
        trigger_step=trigger_step,
        initial_answer=initial_answer,
        initial_claims=[item.copy(status="initial") for item in (claims or [])],
        knowledge_gaps=[str(item).strip() for item in (gaps or []) if str(item).strip()],
        status="initial",
    )
    return state


def save_answer(state: AnswerState, state_dir: Path | None, session_id: str) -> Path:
    """把一份答案状态写成文件，返回路径。

    先写临时文件再换名：写到一半被中断时，目录里不会留下半份 JSON。
    """
    target = answer_path(state_dir, session_id, state.answer_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def load_answer(path: Path) -> AnswerState:
    """从文件读回一份答案状态。"""
    if not path.is_file():
        raise SearchReasoningError(f"答案状态文件不存在：{path}")
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SearchReasoningError(f"答案状态文件读不出来：{path}：{error}") from error
    if not isinstance(data, dict):
        raise SearchReasoningError(f"答案状态文件的内容应当是一份 JSON 对象：{path}")
    return AnswerState.from_dict(data)


def latest_answer(state_dir: Path | None, session_id: str) -> AnswerState | None:
    """读这个会话最近写的一份答案状态；没有时返回 None。"""
    folder = answers_directory(state_dir) / session_id
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("ans_*.json"), key=lambda item: item.stat().st_mtime)
    if not files:
        return None
    return load_answer(files[-1])


async def capture_prior(
    question: str,
    ask: Ask,
    *,
    task_text: str = "",
    narration: str = "",
    recent: str = "",
    memory: str = "",
    trigger_tool: str = "",
    trigger_step: int = 0,
) -> AnswerState:
    """第一次检索结果返回后，在结果进入上下文之前写下先验答案。

    输入只给问题、任务状态、工具调用之前已经写下的叙述、上一轮答复与记忆，
    不塞当前工具输出。这样先验答案反映的是模型自己的理解，后面的证据只负责
    补充与校正。
    """
    messages = [
        {"role": "system", "content": prompts.PRIOR_SYSTEM},
        {
            "role": "user",
            "content": prompts.PRIOR_USER.format(
                question=question.strip(),
                task=task_text.strip() or "（没有任务状态）",
                narration=narration.strip() or "（没有写）",
                recent=recent.strip() or "（没有最近对话）",
                memory=memory.strip() or "（没有相关记忆）",
            ),
        },
    ]
    payload = parse_json(await ask(messages), "先验答案")
    answer = str(payload.get("initial_answer") or "").strip()
    if not answer:
        raise SearchReasoningError("先验答案为空")

    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise SearchReasoningError("先验答案没有给出说法清单")
    claims: list[Claim] = []
    for index, item in enumerate(raw_claims[:MAX_CLAIMS], start=1):
        if isinstance(item, str):
            item = {"content": item}
        if not isinstance(item, dict):
            raise SearchReasoningError(f"第 {index} 条说法的形状无法识别：{item!r}")
        claims.append(
            Claim(
                id=str(item.get("id") or f"c{index}"),
                content=str(item.get("content") or ""),
                category=item.get("category", "fact"),
                confidence=item.get("confidence", 0.5),
                status="initial",
            )
        )
    if len(claims) < MIN_CLAIMS:
        raise SearchReasoningError(
            f"先验答案只给了 {len(claims)} 条说法，至少要 {MIN_CLAIMS} 条"
        )

    gaps = [
        str(item).strip()
        for item in (payload.get("knowledge_gaps") or [])
        if str(item).strip()
    ]
    if not gaps:
        raise SearchReasoningError("先验答案没有给出知识缺口")
    queries = [
        str(item).strip()
        for item in (payload.get("queries") or [])
        if str(item).strip()
    ]

    state = create_state(
        question,
        "rag",
        initial_answer=answer,
        claims=claims,
        gaps=gaps[:5],
        baseline_source="prior",
        trigger_tool=trigger_tool,
        trigger_step=trigger_step,
    )
    state.queries = queries[:5]
    return state
