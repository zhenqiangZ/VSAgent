from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

from vsagent.core.file_log import record as log_read
from vsagent.core.models import Message, Part, PartType, Session, ToolStatus
from vsagent.core.paths import project_root
from vsagent.core.agent.research.workspace import Workspace

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
# 摘要那一条插在系统提示之后
SUMMARY_NOTICE = "（以下是本次对话早前部分的摘要）"
# 常驻记忆插在系统提示之后、摘要之前
MEMORY_NOTICE = "（以下是本项目已知的事实，来自跨会话记忆）"
# 用户习惯单独一段，紧跟在系统提示之后；它与项目事实分开，因为它只在用户改口时变
PROFILE_NOTICE = "（以下是用户长期要求，跨会话保留）"
# 上一个主题刚写完的答复。主题在轮中间封存时，原文不再随请求发出，
# 模型会忘记自己已经答过，于是把同一件事重写一遍。这一段把最后一条正文带回来，
# 只用于衔接，不要求模型复述。
REPLY_NOTICE = "（以下是本段之前已经给用户的答复原文，供衔接用，不要重复）"
# 事件块（已经封存的主题）插在摘要之后、最近交互之前
EVENT_NOTICE = (
    "（以下是已经封存的主题：每个主题是一段做完的工作，下面是它的流水、结论与交接，"
    "原文不再逐条列出；要某一步的原文用 recall_event <编号> step=<步号>）"
)
# 摘要指令。八段固定。前四段回答「这件事是什么」，后四段回答「为什么走到这里」——
# 后者是长会话里最容易丢、又最影响下一步判断的东西：
# 被否掉的方案会让人走回头路，报错与修复会让人重复踩坑。
SUMMARY_INSTRUCTION = (
    "把下面这段对话收成一份摘要，只写八段，每段一个小标题：\n"
    "一、用户的目标与意图\n"
    "二、已经完成的改动与涉及的文件\n"
    "三、待办的下一步\n"
    "四、用户明确给出的约束与要求\n"
    "五、做过的决定与被否掉的方案（为什么否掉）\n"
    "六、错误与修复（哪一步失败过、怎么绕过去的）\n"
    "七、当前正在做的那一步\n"
    "八、关键数字与标识（handle、编号、分数、路径）\n"
    "第四段一条都不能漏：那些要求对之后的每一步都生效，漏掉之后会被当成没说过。\n"
    "如果上面还给了一份「已有摘要」，把它与新内容**合并**成一份：旧事实不要丢，"
    "被新内容推翻的就直接改写，不要两份并列。\n"
    "只写摘要本身，不要寒暄，不要复述这段指令。"
)
# 摘要那一次请求最多喂进去这么多字符。按字符卡住是个上界：汉字大约一字一个 token，
# 其余字符更少，所以字符数不超，token 数就不会超。
SUMMARY_INPUT_LIMIT = 60_000

# 事件（主题）的说明。旧的「摘要指令」已经退休：主题记录由内核每步自动攒，
# 标题与结论是模型顺手给的（`close_topic`），不再是一次独立请求。
# 用法写在 `event_log.CloseTopicTool.spec` 里，离调用点更近。

# bash 工具的执行方式说明。
#
# 这条是从现场来的：模型把上一条命令里的 `cd` 当成了后续命令的基准，于是下一条
# 命令在错的目录里找不到文件。两次调用之间没有任何东西被保持，这一句必须写在
# 系统提示里，否则模型只能从失败里反复试。
#
# **平台不写在这一段里**：它由 `platform_note()` 按 `sys.platform` 现取，紧跟在这段
# 后面。原先这里是写死的「这套环境是 Windows，用 dir 而不是 ls」——在 Linux 上跑
# 的时候，那句话是在教模型写错命令（findings F2）。
SHELL_DISCIPLINE = (
    "bash 每调用一次都是新的解释器：cd、set、export 不留到下一次，换目录写 `cd 目录 && 命令`。\n"
    "命令按下面那句说的平台语法写。"
)


def platform_note(platform_name: str | None = None) -> str:
    """平台那一句。**必须按实际平台给**，不能一律说 Windows。

    这句话原先写死成「这套环境是 Windows，用 dir 而不是 ls」——它在 Linux 上跑
    的时候是**在教模型写错命令**：实测模型照着它写 `dir /s /b`、`netstat -ano |
    findstr`，失败之后才改回 `ls`。平台是本机的常量，放进稳定前缀不伤缓存
    （同一次运行里每个字节都一样），但它必须是**真的**。

    放在前缀而不是易变的头里，是为了让它在 system 角色下、且与 `AGENTS.md` 一样
    只随环境变：日期那种每天一变的东西才需要躲到易变区。
    """
    name = (platform_name or sys.platform).lower()
    if name.startswith("win"):
        return (
            "这套环境是 Windows：命令按它的语法写，用 dir 而不是 ls、用 copy 而不是 cp、"
            "用 type 而不是 cat。不要用 pwd，需要知道当前目录时用 cd 不带参数。"
        )
    # 其余按 POSIX 处理：`linux` / `linux2` / `darwin` / `freebsd` 都是 bash + coreutils
    # 那一套，差别只在几个命令的旗标上，不值得让模型去分辨。
    label = {"darwin": "macOS", "linux": "Linux", "linux2": "Linux"}.get(name, name)
    return (
        f"这套环境是 {label}（bash）：用 ls、cp、grep；"
        "Windows 的 dir、copy、findstr、type 在这里不存在。"
    )


def environment_block(
    workspace: Path | None,
    state_dir: Path | None,
    *,
    now: float | None = None,
) -> str:
    """易变区那段「运行环境」：日期、工作目录、状态目录。

    为什么不并进 `PREFIX`：日期每天变一次，进了前缀就每天击穿一次 prompt cache。
    **只写日期不写时分**——带上时间的话这一头每一步都和上一步不同，缓存从这
    一条起全部作废，等于每步都重发一遍上下文。

    为什么要写状态目录：`state.toml`、`connection.toml` 都在那儿，
    而模型默认它在工作目录下的 `./.vsagent`。实测它就是这么 `cd` 进去、
    失败，再 `ls ~/.vsagent` 才找着的（findings F4）。

    **不写「ledgers/ 也在这里」**：登记表快照固定落在 `~/.vsagent/ledgers`，
    与状态文件的位置无关——`state_path` 被改到别处时（测试与脚本常这么干），
    那句话就是假的。只说这一句能保证为真的部分。
    """
    stamp = time.strftime("%Y-%m-%d", time.localtime(now))
    lines = [f"（运行环境：今天 {stamp}）"]
    if workspace is not None:
        lines.append(f"- 工作目录：{workspace}（runs/、corpus/、logs/ 都在它下面）")
    if state_dir is not None:
        lines.append(
            f"- 状态目录：{state_dir}"
            "（state.toml、connection.toml 在这里，不在工作目录下）"
        )
    return "\n".join(lines)


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
# 中间段每 N 行再留一行。
#
# 只留关键行有个盲区：**结构化输出**（口袋清单、逐条比对表、逐文件统计）里
# 一行都匹配不上「报错 / 结论」，整段被压成一句「省略 N 行」。实测一次 11.4K
# 字符的口袋清单被压到 161 字符，20 个口袋的中心全丢，模型下一句就是
# 「The output got compressed」，然后重跑一遍命令——多花一整步（约 14K token）
# 换回它本来已经拿到手的数据。
#
# 按窗口抽样保留骨架：同样那 158 行中间段留下 59 行（约 2K 字符），
# 20 个口袋的 id / center / radius 都在，仍远小于重跑一步。过程日志那一类会被
# 压得更狠（超过 4000 字符还会进深档），所以抽样不会让「成片的进度行」回到上下文里。
COMPRESS_SAMPLE = 8
# 每个抽样窗口留几行。留 1 行会和「每 8 行一条记录」的格式**正好对齐**：
# 那次口袋清单每个口袋 8 行，单行抽样把 20 个口袋的 id 全留下了、中心一个没留。
# 留 3 行能带出记录的头几个字段（id / center / radius），代价是压缩率从 12.5% 降到 37.5%
# ——超过 4000 字符的日志会在下一档（深档）被压到只剩关键行，所以这一档放宽不吃亏。
COMPRESS_KEEP = 3
# 省略的行数到这个量级才值得在省略说明里带上「怎么取回」：说明本身也有长度，
# 只省下三四行时加上它反而让正文变长。
COMPRESS_HINT_MIN = 8
# 压过的正文里带这一行，见它就知道这一档已经压过了，不必再试。
OMISSION_MARK = "… 压缩掉"
# 更狠那一档留下的标记，与上一档分开，免得两档互相把对方当成「已压过」。
DEEP_MARK = "… 只留要点"
# 输出被压短后，省略说明里那句「怎么取回」的提示上限。
#
# 提示放在省略说明那一行里、不追加到正文末尾：正文有 4000 字符的硬上限，
# 追加会把刚压好的内容重新顶破。这句自己也要有上限，否则说明倒把额度吃回去。
COMPRESSION_HINT_LIMIT = 200

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


def compression_hint(
    tool_name: str = "",
    tool_input: dict[str, Any] | None = None,
    step_no: int = 0,
) -> str:
    """工具输出被压短后，写清三件事：压过、完整内容还在、用哪条更窄的命令取回。

    这句进的是省略说明那一行，不是正文末尾：正文有 4000 字符的硬上限，
    追加到末尾会把刚压好的内容重新顶破。整句还有字数上限，免得说明把额度吃回去。

    只说「省略了多少」而不说怎么取回，模型会以为数据没了，换着法子重跑同一条
    命令（实测同一份 meta.json 用 bash+python3 读了四次）。前两句是通用写法，
    `check_output_limit` 认那几处命令字样，照实留着；拿得到这次的工具与参数时，
    再点出读的是哪个文件、原来那一步怎么取回，让模型不必猜要重跑哪条命令。
    """
    parts = [
        "这段输出的中间被压短了，完整内容还留在会话记录里，没有删除。",
        "要全文：read <path> offset=0 limit=2000 分段读，或 grep 定位关键行。"
        f"单次最多给 {MODEL_OUTPUT_LIMIT} 字符——重读同一个文件也一样。",
    ]
    target = ""
    if isinstance(tool_input, dict):
        for key in ("path", "file", "command"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                target = value.strip()
                break
    if target:
        source = f"（来自 {tool_name}）" if tool_name else ""
        parts.append(f"这次读的是 {target[:60]}{source}。")
    if step_no:
        parts.append(f"要看原来那一步，用 read_turn step={step_no} 取回。")
    return "".join(parts)[:COMPRESSION_HINT_LIMIT]


def compress_tool_output(text: str, deep: bool = False, hint: str = "") -> str | None:
    """把一段工具输出按内容挑着留。改短了返回新正文，没什么可改返回 None。

    先保住关键行（报错原文、结论行），中间段再均匀抽样留下骨架，首尾几行补回上下文，
    剩下的换成一行省略说明。`deep=True` 是更狠的一档：连首尾与抽样都不要，只留关键行。

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
    keep = {index for index, line in enumerate(middle) if is_key_line(line)}
    if not deep:
        # 按**原始位置**开窗，不按关键行之后的序号：关键行一多，抽样点会被挤掉，
        # 剩下的就全是关键行，等于没有骨架。深档不抽样——它是最后一档，
        # 只在非深档仍然超限时才用。
        for start in range(0, len(middle), COMPRESS_SAMPLE):
            keep.update(range(start, min(start + COMPRESS_KEEP, len(middle))))
    kept = [middle[index] for index in sorted(keep)]
    omitted = len(middle) - len(kept)
    blocks = [*head, *kept]
    if omitted > 0:
        mark = DEEP_MARK if deep else OMISSION_MARK
        note = f"{mark} {omitted} 行"
        # 只写「省略 N 行」时，模型会以为数据没了，转头重跑一遍命令（实测过一次：
        # 它下一句就是「The output got compressed」）。省得少的时候不加这句——
        # 说明本身也有长度，加了反而不划算。`hint` 由翻译层传进来：把「压了什么、
        # 怎么取回」写全，两条压缩路径用同一句话；装配器那一路不传，保持原样。
        if hint:
            note += f"；{hint}"
        elif omitted >= COMPRESS_HINT_MIN:
            note += "（原文可用 read_turn 取回）"
        blocks.append(note)
    blocks.extend(tail)
    result = "\n".join(blocks)
    # 按字符比而不是按 token 比：省略说明本身也有长度，用 token 比时
    # 「只省下几行 + 一句说明」会被判成没省，白白退化成不压。
    if result == text or len(result) >= len(text):
        return None
    return result


def truncate_for_model(
    text: str,
    *,
    tool_name: str = "",
    tool_input: dict[str, Any] | None = None,
    step_no: int = 0,
) -> str:
    """把工具输出裁成发给模型的那一份。

    给模型看的那一份与给人看的那一份要求不同：人要看结论，模型要能接上下一句。
    先按内容挑着留（保报错原文与结论行）；挑完仍超长才按位置兜底，
    兜底那一档只保证不超上限，正常走不到。会话里那条 Part 存的是完整原文，
    不受这里影响。

    被压短的那一份，无论走「按内容挑着留」还是「按位置留首尾」，省略说明里
    都带同一句 `compression_hint`：写清压过、完整内容还在、用哪条更窄的命令取回。
    只报「省略多少」不说怎么取回，模型会以为数据没了、换着法子重跑同一条命令
    （实测同一份 meta.json 用 bash+python3 读了四次）。三个参数都带默认值，
    装配器与旧调用点直接调用时行为不变。
    """
    original = len(text)
    if original <= MODEL_OUTPUT_LIMIT:
        return text
    hint = compression_hint(tool_name, tool_input, step_no)
    for deep in (False, True):
        shrunk = compress_tool_output(text, deep=deep, hint=hint)
        if shrunk is None:
            continue
        text = shrunk
        if len(text) <= MODEL_OUTPUT_LIMIT:
            return text
    return _cut_middle(text, MODEL_OUTPUT_HALF, total=original, hint=hint)


def rules_directory(root: Path | None = None) -> Path:
    """规则文件所在的目录。

    默认取**这份代码所属的项目**——源码运行时是仓库根的 `dist/`（与打包产物同一个
    目录，规则只有这一份），打包之后优先取可执行文件旁边，旁边没有规则再取解包目录
    （`sys._MEIPASS`）。两支都**不看启动目录**，所以不管从哪个目录启动，
    读到的都是同一份规则文件。

    打包那一支的顺序是「旁边优先、包里兜底」：

    - **旁边**：`vsagent.spec` 在打包结束时把 `AGENTS.md` 与 `rules/` 复制到
      可执行文件旁边，想改规则不必重新打包；
    - **包里**：`datas` 也收了一份，只把 exe 单独拷走时照样读得到。

    早先只读旁边、靠人工复制，实测那份落后了八天、`rules/` 整个没复制——读不到时
    `read_rule` 返回空串，程序照常运行，只是模型少了三档规则（P-68）。现在复制是
    打包步骤自己做，判据 `verify_exe.check_rules_bundle()` 两头都查字节。

    这与 `env.env_file()` / `permission.project_config_directory` 是同一套判断，
    只有源码运行那一支不同：那两个跟着启动目录走（用户在哪就认哪），
    这一份跟着代码走——规则是写给这个项目本身的，换目录不该把它甩掉。

    `VSA_RULES_DIR` 指到哪儿就取哪儿，调试与断言用它换一份规则文件。
    """
    override = os.environ.get(RULES_DIR_ENV)
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).parent
        if (beside / RULES_FILE).is_file():
            return beside
        return Path(sys._MEIPASS)
    # 源码运行时规则在 `dist/`：那是打包产物的目录，也是规则唯一的一份
    # （仓根不再保留副本，两份必然有一份先过期）。
    # 仓根用 `paths.project_root()` 算，不在这里数 `parents[]`：
    # 这个文件搬过一次目录，数层数的写法会静默指到别处去。
    return project_root() / "dist"


# 项目规则不再进任何会话的前缀：主控行为以 `principal.md` 为准，
# `AGENTS.md` 只留给 `/init` 与人工查阅。`project_rules` 仍然读文件，
# 给判据核对体量与编码；`stable_prefix` 不再调用它。


def project_rules(root: Path) -> str:
    """读 `root` 下的规则文件，每次调用都重新读磁盘。

    每次发问之前都重新读一遍，所以在会话中途改动这个文件之后，
    下一个 step 就按新内容发出去，不用重开会话。

    这里收的是**目录**，不是工作目录：给它的通常是 `rules_directory()`
    算出来的那个，与模型当前在哪个目录里干活无关。

    两种「读不出规则」的情形都不抛异常，也都不给半份：

    - 文件不存在 → 空字符串，那一段不出现；
    - 读不出来（编码错、权限、被独占）→ 空字符串，只记一行失败日志。

    **不设大小上限**：规则是许可与判据的来源，少一条就可能让模型做一个本来被禁止的
    动作。原先超过 32KB 就整份不注入，只留一句「用 read 分段读」——那句话默认模型会
    照做，实际它常常直接动手，确认点等于不存在。文件大了由 `check_prefix` 报数，
    不再拦。

    异常也不往外抛：`stable_prefix` 在 `AgentLoop` 里是装配之前调的，
    那里没有 try，一个 UnicodeDecodeError 会直接掀掉一整轮。
    """
    path = root / RULES_FILE
    if not path.is_file():
        log_read(path, purpose="rules.load", ok=False, detail="文件不存在")
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (UnicodeDecodeError, OSError) as exc:
        log_read(path, purpose="rules.load", ok=False, detail=f"读取失败：{exc}")
        return ""
    # 每个 step 都重读一次，所以这里也每个 step 记一行：看到它连续出现，
    # 说明规则确实是「每次发问都重新读」，而不是被缓存的旧内容。
    log_read(path, purpose="rules.load", size=len(text.encode("utf-8")))
    return text


# ----------------------------------------------------------------------
# 分级规则：全局不变式、角色规则、阶段规则、参考材料
# ----------------------------------------------------------------------
#
# 规则分四级加载，各自回答一个问题：
#
# - **全局**（`rules/global/`）：所有 Agent 都遵守的不变式，回答「什么绝对不能做」；
# - **角色**（`rules/agents/`）：按 `agent_role` 加载，回答「这个角色交什么、不交什么」；
# - **阶段**（`rules/stages/`）：按当前任务子目标加载，回答「这一步做到什么算完成」；
# - **参考**（`rules/references/`）：**不默认注入**，模型需要时自己读。
#
# 为什么角色规则进稳定前缀、阶段规则进动态上下文：角色在一次会话里不变，
# 进前缀可以命中缓存；阶段会随子目标推进而变，进前缀每变一次就把整段缓存打掉。
#
# 全局规则与 `AGENTS.md` 不重复：那一份留原则（科学可追溯、用户决策优先），
# 这一份留做法（数值从哪来、七级证据、什么算「没有记录」）。两边重复的段落
# 已经从 `AGENTS.md` 里删掉，改成一句指向这里的索引——改一边不会只改一半。

RULES_SUBDIR = "rules"

GLOBAL_RULE_FILES: tuple[str, ...] = (
    "scientific_integrity.md",
    "evidence_policy.md",
    "permission_policy.md",
)

ROLE_RULE_DIR = "agents"
STAGE_RULE_DIR = "stages"
REFERENCE_RULE_DIR = "references"


def rules_home(root: Path) -> Path:
    """分级规则所在的目录。与 `AGENTS.md` 同一层，跟着代码走。"""
    return rules_directory(root) / RULES_SUBDIR


def read_rule(path: Path) -> str:
    """读一份规则文件。读不出来返回空串，不抛。

    与 `project_rules` 同一条理由：规则读失败不该掀掉一整轮，
    但也**不给半份**——半个角色规则比没有更容易让模型以为剩下的不重要。
    """
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (UnicodeDecodeError, OSError) as exc:
        log_read(path, purpose="rules.load", ok=False, detail=f"读取失败：{exc}")
        return ""


def global_rules(root: Path) -> str:
    """三份全局不变式，按固定顺序拼接。"""
    home = rules_home(root) / "global"
    blocks = [read_rule(home / name) for name in GLOBAL_RULE_FILES]
    return "\n\n".join(block for block in blocks if block)


def agent_rules(root: Path, role: str) -> str:
    """一个角色的规则。角色为空时取 `principal`——那是根会话的身份。"""
    name = (role or "principal").strip()
    return read_rule(rules_home(root) / ROLE_RULE_DIR / f"{name}.md")


def stage_rules(stage: str, root: Path | None = None) -> str:
    """一个阶段的规则。阶段为空时返回空串：没有阶段就不加这一段。

    `root` 不给时按 `rules_directory()` 的默认规则找：源码运行是项目根，
    打包之后是可执行文件所在目录。装配器在上下文层，手上没有工作目录，
    靠这一条默认值取到同一份规则。
    """
    if not stage:
        return ""
    return read_rule(rules_home(rules_directory(root)) / STAGE_RULE_DIR / f"{stage}.md")


def reference_files(root: Path) -> list[str]:
    """可选的参考材料。**只列名字，不读正文**：它们按需取，不默认注入。"""
    home = rules_home(root) / REFERENCE_RULE_DIR
    if not home.is_dir():
        return []
    return sorted(path.name for path in home.glob("*.md"))


def rule_inventory(root: Path) -> dict[str, list[str]]:
    """这一套规则里有哪些文件。判据用它核对角色与阶段是否齐全。"""
    home = rules_home(root)
    return {
        "global": [name for name in GLOBAL_RULE_FILES if (home / "global" / name).is_file()],
        "agents": sorted(path.stem for path in (home / ROLE_RULE_DIR).glob("*.md")),
        "stages": sorted(path.stem for path in (home / STAGE_RULE_DIR).glob("*.md")),
        "references": reference_files(root),
    }


# 假设纪律：事实、证据、分析与假设要分开；只有机制解释、研究假设或方案比较
# 这类任务才要求形成可检验假设，执行型任务不强制产出假设。
HYPOTHESIS_CONTRACT = ()

# 主控职责与派发纪律。只给根会话：子 agent 的任务书由父会话给，它不决定走哪条路。
PRINCIPAL_CAMPAIGN = ()

# 评分职责按角色分配：对接、亲和力、候选评价各有归属，不在全局预设模型与阈值。
SCORING_DISCIPLINE = ()

# 呈现纪律：视图由 visualization 角色负责，页面地址必须来自工具返回。
PRESENT_DISCIPLINE = ()

# 工具清单在会话建立时定稿，由角色配置决定，运行当中不变。
WORK_DISCIPLINE = (
    "工具清单由当前角色配置决定，会话建立后保持固定；不得假定自己拥有未提供的工具。\n"
    "需要其他角色的工作时，将需求交回 Principal，不得通过 bash、脚本或其他旁路绕过角色权限。\n"
    "优先使用专用工具完成其声明的工作；工具已返回并登记结果时，不要无目的重复执行。"
    "核对结果应针对具体疑点，避免重复读取、重复计算或无意义的文件检查。"
)

# 共享工作区：引用已有对象，写入不越权。
LEDGER_DISCIPLINE = (
    "共享工作区中的科研对象、证据和计算产物应通过系统提供的引用或 handle 追踪；"
    "引用已有对象时优先使用其稳定标识，不要仅凭文件名猜测对象身份。\n"
    "共享读取不代表拥有写入权限。只允许在当前角色授权范围内创建或更新产物；"
    "不得覆盖其他角色或用户已有的结果，除非任务和权限明确允许。"
)

# 跨会话记忆用来恢复已确认的事实与要求，不替代当前任务证据。
MEMORY_DISCIPLINE = (
    "跨会话记忆用于恢复已确认的项目事实与用户明确要求，不替代当前任务证据。"
    "当用户指向先前讨论、决定或产物时，先检索相关记忆或历史记录；"
    "若记忆与当前文件、工具结果或用户最新指令冲突，以当前有效信息为准。"
)

# 主题封存：一个独立任务完成或换到不相关主题时，记录结论与后续依赖。
EVENT_DISCIPLINE = (
    "当一个独立任务已经完成，或当前工作切换到不相关主题时，按事件工具的说明封存主题；"
    "记录实际完成的工作、关键结论、产物引用和后续依赖。不要为了形式化记录而频繁封存，"
    "也不要把未完成事项写成已完成。需要恢复细节时使用事件检索。"
)


# 子 agent 的身份补充。排在身份段之后。
SUBAGENT_DISCIPLINE = (
"""
## Minimum Sufficient Execution

你是由 Principal 派发的专业 Worker Agent。你的目标是以最小充分执行完成当前 AgentTask，而不是最大化分析深度、工具调用次数或交付内容。

### 1. Task Scope
- 当前 AgentTask 是唯一执行目标。
- 角色职责和工具能力定义的是你可以做什么，不代表当前任务都需要执行。
- 不得将角色模板、完整科研流程或可选分析自动扩展为当前任务。
- 不得重复完成父任务或其他 Agent 已经完成的工作。

### 2. Minimal Execution
- 优先选择能够直接满足当前目标的专用工具。
- 如果一次工具调用已经能够获得所需结果，不得为了增加可信度而机械追加验证。
- 优先复用已有输入、结构、证据和计算产物，不重复获取或计算。
- 不执行与当前验收条件无直接关系的检索、分析、计算、登记和可视化。
- 不得仅因为某项分析属于角色职责，就主动执行该分析。

### 3. Tool Call Necessity
每次调用工具前，判断：
1. 当前任务是否仍存在未满足的要求？
2. 这次调用是否直接帮助满足某项要求？
3. 是否已有可复用的结果？
4. 是否存在更直接、更低成本的实现方式？

如果任务已经满足验收条件，或者调用不能实质性改善交付结果，则不得继续调用工具。

### 4. Completion and Stop
满足以下条件时立即结束：
- 当前任务目标已经实现。
- 必需交付物已经生成或已明确无需生成。
- 必要的证据和引用已经具备。
- 没有尚未解决的必要依赖。

完成后直接生成 AgentReport，不得为了形式化完整性而追加工具调用。

### 5. Optional Work
可选分析不得自动升级为必做任务。

如果发现可能有价值但不影响当前任务完成的后续工作，将其记录在 suggested_followups 中，交由 Principal 判断，不得自行执行。

### 6. Blocked Execution
如果关键输入缺失、工具失败或现有证据不足：
- 只执行能够解除当前阻塞的必要操作。
- 不得通过不断增加其他分析来掩盖阻塞。
- 无法继续时如实返回 partial 或 failed，并说明原因。

### 7. Final Principle
任务完成优先于流程完整，结果充分优先于分析数量。

不以工具调用次数、执行步骤或输出篇幅衡量任务质量。能够用一次调用解决的问题，不得人为扩展成多步工作。
"""
)


def stable_prefix(workspace: Workspace, session: Session) -> str:
    """稳定前缀：身份、角色说明、通用纪律与分层规则。

    稳定前缀只放会话期间不变的内容。任务规格、当前阶段、依赖状态和进度由
    `ContextAssembler` 放入动态上下文，避免每一步改变前缀并破坏缓存。

    子 agent 与主控都不读 `AGENTS.md`：主控的边界由 `principal.md`、全局规则与
    代码共同决定；子 agent 的边界由 `AgentTask`、本角色规则、全局规则与阶段规则决定。
    `AGENTS.md` 仍保留在磁盘上，供 `/init` 与人工查阅。
    """
    role = _session_role(session)
    blocks = [_identity(session.agent_name, role)]

    if session.depth > 0:
        blocks.append(SUBAGENT_DISCIPLINE)
    else:
        blocks.append(PRINCIPAL_CAMPAIGN)

    blocks.extend(
        [
            HYPOTHESIS_CONTRACT,
            WORK_DISCIPLINE,
            f"{SHELL_DISCIPLINE}\n{platform_note()}",
            LEDGER_DISCIPLINE,
            MEMORY_DISCIPLINE,
        ]
    )
    # 子会话没有事件工具，写一段封存纪律只会让它去调不存在的工具。
    if session.depth <= 0:
        blocks.append(EVENT_DISCIPLINE)

    # 评分纪律属于专业角色，不向所有 Agent 注入。
    if role in {"docking", "affinity", "analysis"}:
        blocks.append(SCORING_DISCIPLINE)
    # 视图生成由 visualization 负责；其他角色不承担页面交付义务。
    if role == "visualization" or session.depth <= 0:
        blocks.append(PRESENT_DISCIPLINE)

    base = rules_directory(workspace.root)
    invariants = global_rules(base)
    if invariants:
        blocks.append(f"以下全局规则适用于所有角色，必须遵守：\n\n{invariants}")

    mine = agent_rules(base, role) if role else ""
    if mine:
        blocks.append(f"以下是当前角色 {role} 的专属规则，必须遵守：\n\n{mine}")

    # 项目规则不再进前缀：主控行为以 `principal.md` 为准，`AGENTS.md` 只留给
    # `/init` 与人工查阅。`project_rules` 仍然保留，给判据核对文件本身。
    return "\n\n".join(block for block in blocks if block)


def _session_role(session: Session) -> str:
    """会话的角色名。根会话默认 `principal`，子会话没有角色时留空。"""
    role = str(getattr(session, "agent_role", "") or "").strip()
    if role:
        return role
    return "principal" if session.depth <= 0 else ""


def _identity(agent_name: str, role: str = "principal") -> str:
    """身份段：运行模式与 Agent 角色分开描述。

    `agent_name` 表示 Plan / Build 运行模式；`agent_role` 表示多 Agent 职责。
    两者不能混为一谈：Plan/Build 不代表角色，角色也不自动授予工具权限。
    """
    mode = str(agent_name or "Build").strip().lower()
    if mode == "plan":
        # 这一段的职责是让模型**不去试**，所以必须覆盖 `PLAN_DENY` 的全部类别，
        # 而不是只提一两个例子：只写「准备受体不在此列」时，模型会以为
        # dock / 打分 / 排序这些照常可用，试一次被拒一次，白烧几步。
        mode_text = (
            "当前运行模式为 Plan：**只读检索与读取可用**"
            "（查库、取结构、查文献、读语料、看登记表）。\n"
            "**任何会写盘或跑引擎的动作都会被拒绝**：准备受体与配体、检测与定义口袋、"
            "对接、重对接验证与参照集、打分与微调、写运行目录、批量筛选与排序、"
            "药效团构建与筛选、结构叠合、性质预测、骨架与配对分析、枚举类似物、"
            "记录实验数据，以及 bash、写文件、改文件。\n"
            "需要这些动作时给出方案与参数，交给用户在 Build 方式下执行。"
        )
    else:
        mode_text = (
            "当前运行模式为 Build：可以在运行时权限和当前角色授权范围内执行任务；"
            "Build 不代表拥有全部工具权限，也不能绕过用户确认。"
        )

    from vsagent.core.agent.roles.catalog import ROLES

    if not role:
        role_text = "你的 Agent 角色尚未记录；只按当前 AgentTask 执行，不得自行猜测职责或工具。"
        return f"{mode_text}\n{role_text}"

    if role not in ROLES:
        role_text = f"当前角色标识为 {role}，但它不在当前角色注册表中；不得自行猜测职责或工具。"
        return f"{mode_text}\n{role_text}"

    if role == "principal":
        role_text = ()
    else:
        from vsagent.core.agent.roles.profiles import profile

        spec = profile(role)
        duty_text = "、".join(spec.duties) or "无"
        permission_text = "、".join(spec.permissions) or "无"
        role_text = (
            f"你的 Agent 角色是 {role}（{spec.title}）。\n"
            f"角色职责：{spec.note}\n"
            f"负责的工作：{duty_text}。\n"
            f"允许的动作类别：{permission_text}。这些类别是角色授权边界，不代表每个任务都需要执行；"
            "具体任务范围由当前 AgentTask 决定。"
        )
    return f"{mode_text}\n{role_text}"


def summary_messages(session: Session, upto: int) -> list[dict[str, Any]]:
    """造一次摘要请求的消息：只喂**新露出来的那一段**，并把旧摘要带上。

    先前每次从第 0 条重喂整段历史：一是每次压缩都要重读一遍早已收过的内容，
    二是模型看到的是「原始对话」，它会把旧结论重新组织一遍，`summary_upto`
    之前的事实于是每次都可能变形。现在只喂 `[summary_upto, upto)`，
    旧摘要作为「已有摘要」交给它合并。

    原文按翻译层翻出来再铺成文字，所以摘要看到的是模型当时看到的那一份，
    包括工具调用与工具结果，不包括界面上那些标记。
    """
    start = session.summary_upto if session.summary else 0
    transcript = _transcript(session, upto, start)
    if len(transcript) > SUMMARY_INPUT_LIMIT:
        # 正常不会走到这里：一次压缩只推进几步。只有单条消息本身超长时才会，
        # 那时保留开头与结尾仍然好过整份发不出去。
        transcript = _cut_middle(transcript, SUMMARY_INPUT_LIMIT // 2)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SUMMARY_INSTRUCTION}]
    if session.summary:
        messages.append(
            {
                "role": "user",
                "content": f"已有摘要（把它与新内容合并成一份，不要丢已有的事实）：\n{session.summary}",
            }
        )
    messages.append({"role": "user", "content": transcript})
    return messages


def _transcript(session: Session, upto: int, start: int = 0) -> str:
    """把 `[start, upto)` 这几条消息铺成一段纯文本。

    `start` 是给事件用的：事件只收口**新露出来的那一段**，把它前面
    已经收过的内容再喂一遍，既费额度又会让模型把旧事重新写进新事件。
    """
    blocks: list[str] = []
    for message in session.messages[start:upto]:
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


def _cut_middle(
    text: str, half: int, *, total: int | None = None, hint: str = ""
) -> str:
    """留开头与结尾，中间写一行省略说明。

    `total` 是**压缩之前**的长度：模型看到的是压缩后的那一份，只报它的话
    「原文到底多长」就没有答案。`hint` 是给模型的下一步动作——摘要那条路
    不需要它（那里没有人会去 `read` 一个文件），所以默认空。
    """
    omitted = len(text) - 2 * half
    note = f"… 省略 {omitted} 字符（原文 {total or len(text)} 字符）"
    if hint:
        note = f"{note}\n   {hint}"
    return f"{text[:half]}\n{note}\n{text[-half:]}"


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
    return translate_parts(message.role, message.parts)


def model_tool_output(part: Part) -> str:
    """工具结果进入模型上下文的那一份正文。

    检索结果被 SAR 单通道接走之后，原文留在 Part 上给界面与 `read_turn`，
    模型只看到一句「已整理为证据、原文按步号取回」的提示。数据库与计算
    结果没有这个标记，照原样进入上下文。
    """
    meta = part.tool_meta if isinstance(part.tool_meta, dict) else {}
    if meta.get("sar_suppressed"):
        return str(meta.get("sar_model_output") or "（检索结果已整理为证据）")
    return part.tool_output


def translate_parts(role: str, parts: list[Part]) -> list[dict[str, Any]]:
    """把一段 Part 翻成模型消息。

    按段调用是装配器的需要：一条 assistant 消息装着一整轮的所有步，
    `split_turns` 要按 `STEP_START` 一段一段切，切完再各自翻译。
    整条消息翻译与逐段翻译的结果完全一致，逐段那份只是多知道每段从哪个
    Part 开始——`Turn.part_start` 就取这个位置。
    """
    if role == "user":
        return _translate_user(parts)
    return _translate_assistant(parts)


def _translate_user(parts: list[Part]) -> list[dict[str, Any]]:
    """用户消息：正文与引用文件拼成同一条 content。"""
    chunks: list[str] = []
    for part in parts:
        if part.type is PartType.TEXT and part.text:
            chunks.append(part.text)
        elif part.type is PartType.FILE and part.file_path:
            chunks.append(f'<file path="{part.file_path}">\n{part.text}\n</file>')
    if not chunks:
        return []
    return [{"role": "user", "content": "\n".join(chunks)}]


def _translate_assistant(parts: list[Part]) -> list[dict[str, Any]]:
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
                        "content": truncate_for_model(
                            model_tool_output(part),
                            tool_name=part.tool_name,
                            tool_input=part.tool_input,
                        ),
                    }
                )
        elif content:
            item = {"role": "assistant", "content": content}
            item["reasoning_content"] = marker
            translated.append(item)
        reasoning.clear()
        text.clear()
        tools.clear()

    for part in parts:
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
