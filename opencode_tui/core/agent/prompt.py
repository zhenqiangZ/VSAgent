from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from ..models import Message, Part, PartType, Session, ToolStatus
from .workspace import Workspace

# 单个工具输出发给模型的那一份总长度，超过就压。按字符卡是个上界：
# 汉字大约一字一个 token，其余字符更少，所以字符数不超，token 数就不会超。
MODEL_OUTPUT_LIMIT = 4000
MODEL_OUTPUT_HALF = 2000
# 手动压缩（`/compact`）时保留的最近消息条数。与装配器的 `RECENT` 软配额无关：
# 这一条是用户按出来的，管的是会话上那个压缩位置推到哪里。
KEEP_MESSAGES = 6
# 丢掉了更早的消息时插在模型消息最前面的那一句。装配器（`context.py`）用它
OMITTED_NOTICE = "（系统提示：本次对话开始之前的部分内容已经省略。）"
# 本 step 没有推理输出时，回传 reasoning_content 用的占位。
#
# 服务端要的是「这个字段存在」，内容它只用来自我一致，所以占位比省掉字段安全：
# 少了字段的那一条会被整条请求退回 400（「一步只有工具调用、没有推理」正是这个形状）。
EMPTY_REASONING = "（本步没有推理输出）"
# 中间推理被压掉之后回传的占位。与上一条同理：字段必须留着，不能省掉。
# 两个占位分开写，是为了在请求体里分得清「这一步本来就没推理」与「推理被压了」。
REASONING_PLACEHOLDER = "（本步的中间推理已压缩，结论见正文）"
# 项目规则文件，每次发问之前重新读
RULES_FILE = "AGENTS.md"
# 规则文件所在的目录，可以用这个环境变量顶掉（调试与断言换一份规则用）
RULES_DIR_ENV = "VSA_RULES_DIR"
# 规则文件注入系统提示的上限，按字节数算
RULES_LIMIT = 32 * 1024
# 摘要那一条插在系统提示之后
SUMMARY_NOTICE = "（以下是本次对话早前部分的摘要）"
# 常驻记忆插在系统提示之后、摘要之前
MEMORY_NOTICE = "（以下是本项目已知的事实，来自跨会话记忆）"
# 摘要指令。四段固定，最后一段是关键：用户早前给出的约束不能因为丢弃就没了
SUMMARY_INSTRUCTION = (
    "把下面这段对话收成一份摘要，只写四段，每段一个小标题：\n"
    "一、用户的目标与意图\n"
    "二、已经完成的改动与涉及的文件\n"
    "三、待办的下一步\n"
    "四、用户明确给出的约束与要求\n"
    "第四段一条都不能漏：那些要求对之后的每一步都生效，漏掉之后会被当成没说过。\n"
    "只写摘要本身，不要寒暄，不要复述这段指令。"
)
# 摘要那一次请求最多喂进去这么多字符。按字符卡住是个上界：汉字大约一字一个 token，
# 其余字符更少，所以字符数不超，token 数就不会超。
SUMMARY_INPUT_LIMIT = 60_000

# bash 工具的执行方式说明。
#
# 这条是从现场来的：模型按 Linux 的习惯写 `pwd` 与 `cp a b`，而这里每一步都交给
# Windows 的命令解释器上；更常见的一种是它把上一条命令里的 `cd` 当成了后续命令的基准，
# 于是下一条命令在错的目录里找不到文件。两次调用之间没有任何东西被保持，
# 这一句必须写在系统提示里，否则模型只能从失败里反复试。
SHELL_DISCIPLINE = (
    "bash 工具每调用一次都是一次全新的命令解释器：上一条命令里的 cd、set、export "
    "都不会留到下一次。要换目录就在同一条命令里写全，例如 "
    "`cd 项目目录 && 命令`，或者在命令里直接用完整路径。\n"
    "这套环境是 Windows，命令按它的语法写：用 dir 而不是 ls，用 copy 而不是 cp。"
    "不要用 pwd，需要知道当前目录时用 cd 不带参数。"
)


def estimate_tokens(text: str) -> int:
    """按字符类别估算 token 数。

    汉字与全角标点大约一个字一个 token，其余字符大约四字一个 token。
    统一按两个字一个 token 估的话，中文长对话会被低估到三分之一。
    """
    wide = 0
    narrow = 0
    for char in text:
        if _is_wide(char):
            wide += 1
        else:
            narrow += 1
    return wide + math.ceil(narrow / 4)


def _is_wide(char: str) -> bool:
    """判定一个字符是不是汉字或全角标点。"""
    code = ord(char)
    return (
        0x3000 <= code <= 0x303F
        or 0x4E00 <= code <= 0x9FFF
        or 0xFF00 <= code <= 0xFFEF
    )


# ---- 选择性压缩 ----
#
# 压缩按**内容类型**挑着丢，不按字符位置截。位置截断有两个毛病：报错原文可能正好
# 在被削掉的那一段里，结论所在的那一行也可能被留在省略号之外——削掉的正是要看的。
# 这里先按行分类，把关键行挑出来逐字留下，再用首尾几行补回上下文。
#
# 压缩工具输出时，正文短于这个 token 数就不再动它——再压也省不下多少。
COMPRESS_FLOOR = 64
# 非深度那一档留下的首尾行数，用来把关键行周围的上下文补回来。
COMPRESS_HEAD = 2
COMPRESS_TAIL = 2
# 压过的正文里带这一行，见它就知道这一档已经压过了，不必再试。
OMISSION_MARK = "… 压缩掉"
# 更狠那一档留下的标记，与上一档分开，免得两档互相把对方当成「已压过」。
DEEP_MARK = "… 只留要点"

# 工具输出里必须逐字留下的两类行。
#
# 报错按「行里含」判定：这些词在正常输出里几乎不出现，不会误伤。
ERROR_MARKERS: tuple[str, ...] = (
    "error", "traceback", "exception", "failed", "failure", "fatal",
    "denied", "refused", "not found", "no such file", "cannot", "unable",
    "错误", "失败", "异常", "报错", "拒绝", "找不到", "无法",
)
# 结论按「行首是」判定，不按「行里含」。差别是有原因的：「处理第 12 个配体完成」
# 这种过程行里也含「完成」，按含判定等于没压——而过程行正是要丢掉的那一大片。
RESULT_MARKERS: tuple[str, ...] = (
    "结论", "结果", "总计", "合计", "摘要", "命中", "最优", "最终",
    "score", "rmsd", "summary", "total", "result",
)
# 报错的调用链也是报错原文的一部分，不能只留最后一行。
TRACEBACK_PREFIXES: tuple[str, ...] = ('file "', "at ", "caused by")


def is_key_line(line: str) -> bool:
    """这一行是不是关键行：报错原文，或者结论行。关键行压缩时逐字留下。"""
    stripped = line.strip().lstrip("-*#·> \t")
    lowered = stripped.lower()
    if any(marker in lowered for marker in ERROR_MARKERS):
        return True
    return any(lowered.startswith(marker) for marker in (*RESULT_MARKERS, *TRACEBACK_PREFIXES))


def compress_tool_output(text: str, deep: bool = False) -> str | None:
    """把一段工具输出按内容挑着留。改短了返回新正文，没什么可改返回 None。

    先保住关键行（报错原文、结论行），再用首尾几行补回上下文，成片的过程输出
    换成一行省略说明。`deep=True` 是更狠的一档：连首尾也不要，只留关键行。

    返回 None 而不是原串，是为了让调用方分得清「压过了」与「已经没得压」——
    降级链靠这个判断停下来。压过的正文带标记，同一档不会重复压。
    """
    if estimate_tokens(text) <= COMPRESS_FLOOR:
        return None
    if DEEP_MARK in text or (not deep and OMISSION_MARK in text):
        return None
    lines = text.splitlines()
    if len(lines) <= COMPRESS_HEAD + COMPRESS_TAIL + 1:
        # 行数太少，按行分不出哪一段是过程输出。**不动它**，交给调用方按位置兜底——
        # 没有行结构就没有内容可挑，硬按字符切反而会把唯一那点信息切碎。
        return None
    head = [] if deep else lines[:COMPRESS_HEAD]
    tail = [] if deep else lines[-COMPRESS_TAIL:]
    middle = lines if deep else lines[COMPRESS_HEAD : len(lines) - COMPRESS_TAIL]
    kept = [line for line in middle if is_key_line(line)]
    omitted = len(middle) - len(kept)
    blocks = [*head, *kept]
    if omitted > 0:
        mark = DEEP_MARK if deep else OMISSION_MARK
        blocks.append(f"{mark} {omitted} 行过程输出")
    blocks.extend(tail)
    result = "\n".join(blocks)
    if result == text or estimate_tokens(result) >= estimate_tokens(text):
        return None
    return result


def truncate_for_model(text: str) -> str:
    """把工具输出裁成发给模型的那一份。

    给模型看的那一份与给人看的那一份要求不同：人要看结论，模型要能接上下一句。
    先按内容挑着留（保报错原文与结论行）；挑完仍超长才按位置兜底，
    兜底那一档只保证不超上限，正常走不到。会话里那条 Part 存的是完整原文，
    不受这里影响。
    """
    if len(text) <= MODEL_OUTPUT_LIMIT:
        return text
    for deep in (False, True):
        shrunk = compress_tool_output(text, deep=deep)
        if shrunk is None:
            continue
        text = shrunk
        if len(text) <= MODEL_OUTPUT_LIMIT:
            return text
    return _cut_middle(text, MODEL_OUTPUT_HALF)


def rules_directory(root: Path) -> Path:
    """规则文件所在的目录。

    默认取**这份代码所属的项目**——源码运行时是包的上级目录（开发项目根），
    打包之后是可执行文件所在目录。两支都**不看启动目录**，所以不管从哪个目录
    启动，读到的都是同一份规则文件。

    这与 `env.env_file()` / `permission.project_config_directory` 是同一套判断，
    只有源码运行那一支不同：那两个跟着启动目录走（用户在哪就认哪），
    这一份跟着代码走——规则是写给这个项目本身的，换目录不该把它甩掉。

    `VSA_RULES_DIR` 指到哪儿就取哪儿，调试与断言用它换一份规则文件。
    """
    override = os.environ.get(RULES_DIR_ENV)
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[3]


def project_rules(root: Path) -> str:
    """读 `root` 下的规则文件，每次调用都重新读磁盘。

    每次发问之前都重新读一遍，所以在会话中途改动这个文件之后，
    下一个 step 就按新内容发出去，不用重开会话。

    这里收的是**目录**，不是工作目录：给它的通常是 `rules_directory()`
    算出来的那个，与模型当前在哪个目录里干活无关。

    文件不存在时返回空字符串，系统提示里那一段就不出现。
    超过上限时在最后一个完整行处断开，末尾写明省略了多少字节。
    """
    path = root / RULES_FILE
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if len(text.encode("utf-8")) <= RULES_LIMIT:
        return text
    return _cut_rules(text)


def _cut_rules(text: str) -> str:
    """按上限截断规则文件，在最后一个完整行处断开。"""
    kept: list[str] = []
    size = 0
    for line in text.splitlines():
        width = len(line.encode("utf-8")) + 1
        if size + width > RULES_LIMIT:
            break
        kept.append(line)
        size += width
    omitted = len(text.encode("utf-8")) - size
    return "\n".join(kept) + f"\n（项目规则文件过长，省略了 {omitted} 字节）"


# 引用登记表里那些产物的纪律。身份段之后紧接着说，因为它影响每一次工具调用的写法。
LEDGER_DISCIPLINE = (
    "会话登记表里记着你已经拿到或算出来的东西（受体、配体集、产物）。"
    "**引用它们一律用 handle，不要写文件路径**：handle 形如 `receptor#1` / `ligandset#3`，"
    "从 list_workspace 里取，用 describe_entry 查详情。\n"
    "全库只有 import_structure 一个动作收路径（那一下真的需要人给出文件），"
    "其余工具收到路径一律报错。\n"
    "不要凭记忆写 handle——它编不出来但看着很合理，而登记表里没有的那个号"
    "会让后面每一步都建立在不存在的东西上。"
)

# 记忆是跨会话的，与登记表（会话内）分工不同，所以单列一段说清两者的区别。
MEMORY_DISCIPLINE = (
    "项目记忆里记着这个项目已知的事实（受体、结合位点、配体库），它**跨会话**保留。"
    "在追问用户「上次那个受体在哪」之前先用 recall 查一次——上一轮做过的事这里可能有记录。"
    "要把一条事实留给以后的会话时用 remember；**记忆里写文件路径，不要写 handle**，"
    "handle 只在本次会话有效，写进去下次就指向别的东西了。"
)


# 工作纪律。稳定前缀的第二段，紧跟身份。
WORK_DISCIPLINE = (
    "用户交办的事情需要动文件或跑程序时，调用工具完成，不要凭猜测写出结果。\n"
    "工具给出的失败原因是你下一轮的输入：参数被拒就改参数，命令报错就看报错内容。\n"
    "工具输出里的行号是文件中的绝对行号，定位参数按同一个基准。\n"
    "生成的中间文件与结果表写进工作目录，报告里说明每一项使用了什么参数。"
)


def stable_prefix(workspace: Workspace, session: Session) -> str:
    """稳定前缀（`PREFIX` 层）：身份、工作纪律、项目规则。

    这一段在同一会话内只随 `AGENTS.md` 变化，**不放时间、不放进度**——
    放了的话每一步都会击穿 prompt cache，前缀缓存永远命中不了。

    规则取 `rules_directory()` 那一份，与 `workspace.root`（模型干活的目录）
    无关：换工作目录不该换掉项目规则。
    """
    blocks = [
        _identity(session.agent_name),
        WORK_DISCIPLINE,
        SHELL_DISCIPLINE,
        LEDGER_DISCIPLINE,
        MEMORY_DISCIPLINE,
    ]
    rules = project_rules(rules_directory(workspace.root))
    if rules:
        blocks.append(f"这个项目的规则，每一条都必须遵守：\n\n{rules}")
    return "\n\n".join(blocks)


def build_system_prompt(workspace: Workspace, session: Session) -> str:
    """系统提示：稳定前缀 + 工作目录的当前状态。

    保留这个入口是为了兼容既有调用点。新装配走 `ContextAssembler`：
    那里把「当前状态」放进 `TASK_HEADER`（`TASK` 层），不再混进稳定前缀，
    前缀因此可以在会话内保持字节稳定。
    """
    return f"{stable_prefix(workspace, session)}\n\n{workspace.state_text(session)}"


def _identity(agent_name: str) -> str:
    """按 Agent 给出身份段。Build 可以改动文件，Plan 只看不改。"""
    if agent_name == "Plan":
        return (
            "你是虚拟筛选工作的助手，当前以 Plan 方式工作：只读文件与运行不会产生改动的命令，"
            "需要改动时给出方案与步骤，交给用户在 Build 方式下执行。"
            "浏览登记表、查 UniProt 与 ChEMBL、取结构这些只读或只取的动作照常可用；"
            "**准备受体不在此列**（它要往磁盘写一组产物），请在方案里写清参数。"
        )
    return "你是虚拟筛选工作的助手，当前以 Build 方式工作：可以直接读写文件与运行命令。"


def summary_messages(session: Session, upto: int) -> list[dict[str, Any]]:
    """造一次摘要请求的消息：把前 upto 条消息的原文交给模型。

    原文按翻译层翻出来再铺成文字，所以摘要看到的是模型当时看到的那一份，
    包括工具调用与工具结果，不包括界面上那些标记。
    """
    transcript = _transcript(session, upto)
    if len(transcript) > SUMMARY_INPUT_LIMIT:
        transcript = _cut_middle(transcript, SUMMARY_INPUT_LIMIT // 2)
    return [
        {"role": "system", "content": SUMMARY_INSTRUCTION},
        {"role": "user", "content": transcript},
    ]


def _transcript(session: Session, upto: int) -> str:
    """把前 upto 条消息铺成一段纯文本。"""
    blocks: list[str] = []
    for message in session.messages[:upto]:
        for item in translate_message(message):
            role = item["role"]
            if role == "tool":
                blocks.append(f"[工具结果]\n{item.get('content') or ''}")
                continue
            if item.get("tool_calls"):
                calls = "；".join(
                    f"{call['function']['name']}({call['function']['arguments']})"
                    for call in item["tool_calls"]
                )
                blocks.append(f"[{role} 要求调用工具] {calls}")
            content = item.get("content")
            if content:
                blocks.append(f"[{role}]\n{content}")
    return "\n\n".join(blocks)


def _cut_middle(text: str, half: int) -> str:
    """留开头与结尾，中间写一行省略说明。"""
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n… 省略 {omitted} 字符\n{text[-half:]}"


def item_tokens(item: dict[str, Any]) -> int:
    """估算一条模型消息占多少 token，只数要发出去的那些字段。"""
    total = 0
    content = item.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    reasoning = item.get("reasoning_content")
    if isinstance(reasoning, str):
        total += estimate_tokens(reasoning)
    for call in item.get("tool_calls") or []:
        function = call["function"]
        total += estimate_tokens(function["name"]) + estimate_tokens(function["arguments"])
    return total


def translate_message(message: Message) -> list[dict[str, Any]]:
    """按消息的角色选翻译方式。"""
    if message.role == "user":
        return _translate_user(message)
    return _translate_assistant(message)


def _translate_user(message: Message) -> list[dict[str, Any]]:
    """用户消息：正文与引用文件拼成同一条 content。"""
    chunks: list[str] = []
    for part in message.parts:
        if part.type is PartType.TEXT and part.text:
            chunks.append(part.text)
        elif part.type is PartType.FILE and part.file_path:
            chunks.append(f'<file path="{part.file_path}">\n{part.text}\n</file>')
    if not chunks:
        return []
    return [{"role": "user", "content": "\n".join(chunks)}]


def _translate_assistant(message: Message) -> list[dict[str, Any]]:
    """助手消息：一个 step 里的推理、正文与工具调用合成一条模型消息。

    DeepSeek 思考模式下，只要请求携带 tools 参数，历史里每一条 assistant 消息都要
    回传 reasoning_content，包括没有工具调用的最终回答；缺少任意一条都会被拒绝。

    所以这里挂的是**无条件**的：本 step 没有推理输出时补一句 `EMPTY_REASONING`，
    而不是把字段省掉。「每条 assistant 都带这个字段」是一条结构上的不变式，
    不靠「这一步刚好有推理」来成立——只认后者的写法漏掉了「一步只有工具调用、
    没有推理」这个形状，服务端收到缺字段的那一条就回 400。
    纯聊天的请求里多带的推理会被忽略，补的占位同样无害。
    """
    translated: list[dict[str, Any]] = []
    reasoning: list[str] = []
    text: list[str] = []
    tools: list[Part] = []

    def flush() -> None:
        content = "".join(text)
        # 两个分支都要挂：只在其中一个分支补过，另一个分支漏掉时症状一样是 400
        marker = "".join(reasoning) or EMPTY_REASONING
        if tools:
            item: dict[str, Any] = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": part.id,
                        "type": "function",
                        "function": {
                            "name": part.tool_name,
                            "arguments": json.dumps(part.tool_input, ensure_ascii=False),
                        },
                    }
                    for part in tools
                ],
            }
            item["reasoning_content"] = marker
            translated.append(item)
            for part in tools:
                translated.append(
                    {
                        "role": "tool",
                        "tool_call_id": part.id,
                        "content": truncate_for_model(part.tool_output),
                    }
                )
        elif content:
            item = {"role": "assistant", "content": content}
            item["reasoning_content"] = marker
            translated.append(item)
        reasoning.clear()
        text.clear()
        tools.clear()

    for part in message.parts:
        if part.type is PartType.STEP_START:
            flush()
        elif part.type is PartType.REASONING:
            reasoning.append(part.text)
        elif part.type is PartType.TEXT:
            text.append(part.text)
        elif part.type is PartType.TOOL and part.tool_status in (
            ToolStatus.COMPLETED,
            ToolStatus.FAILED,
        ):
            tools.append(part)
    flush()
    return translated
