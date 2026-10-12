"""任务状态：本会话的目标、用户约束、进度与阻塞。

与记忆（`memory.py`）的分工要分清，两者容易被混为一谈：

- **记忆是跨会话的项目事实**（受体在哪个文件、上次用什么参数），会话换了它还在；
- **任务状态是本会话的目标与进度**，会话换了它跟着换。

`TaskState` 本身是数据，放在 `core/models.py`（`Session` 持有它）；这里只放
模型能调用的那个工具。放一起会成环：`models.py` 是数据层，不该反过来依赖工具层。

任务状态进上下文时整块进 `TASK` 层、**不参与淘汰**——用户提的要求不能因为聊得长
就被挤出去，这是这一层存在的全部理由。
"""

from __future__ import annotations

from typing import Any

from ..models import TaskState
from .tools import Tool, ToolError, ToolResult, ToolSpec


class TaskTool(Tool):
    """更新本会话的任务状态。

    会话对象由内核注入（`task_argument`），模型看不到、也不许传——
    模型只能改这五个字段，改不了别的会话，也改不了会话上的其它东西。
    """

    spec = ToolSpec(
        name="task",
        description=(
            "记录或更新本会话的任务状态：目标、用户提出的约束、已完成、待办、阻塞。"
            "这段状态每一步都会完整发给模型，不会因为对话变长被丢掉，"
            "所以**用户提的要求一律记进 `constraints` 参数**，别指望聊天记录还记得住。"
            "只填这次真正要改的字段，没填的字段不动。"
            "`constraints` 是累加的：这次没写出来的旧约束不会被删掉。"
            "done / todo / blockers 是整段替换：要改就把这一类完整重写一遍，"
            "传空数组表示这一类清空。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "本会话要做成什么，一句话；传了就覆盖原来的目标",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户新提出的要求，逐条追加；已有的约束不会被覆盖",
                },
                # 这个参数原来叫 `add_constraints`。名字与状态里那个字段
                # （`constraints`）、与说明文字、与渲染出来的标题都对不上，
                # 模型照着说明去写 `constraints`，被 `additionalProperties`
                # 挡下来白烧一步（现场：4 个会话里各撞过一次）。
                # 现在以 `constraints` 为准，旧名字继续收，免得老会话的
                # 历史里那些调用被模型学了去。
                "add_constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "与 constraints 相同，保留的旧写法",
                },
                "done": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "已完成的步骤，整段替换",
                },
                "todo": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "待办步骤，整段替换",
                },
                "blockers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "当前卡住的地方，整段替换",
                },
            },
            "additionalProperties": False,
        },
    )

    # 收任务状态的参数名。内核按它把 `session.task_state` 塞进来。
    task_argument = "state"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """按字段更新任务状态。没写任何字段就直接说清楚，不白改一次版本号。"""
        state: TaskState = arguments["state"]
        # 两个名字收的是同一件事：新名字 `constraints` 是给模型看的，
        # 旧名字 `add_constraints` 继续认。都给就都追加。
        added = [
            *(arguments.get("constraints") or ()),
            *(arguments.get("add_constraints") or ()),
        ]
        fields = ("goal", "constraints", "add_constraints", "done", "todo", "blockers")
        if not any(arguments.get(name) is not None for name in fields):
            raise ToolError(
                "这次调用没有给出任何要改的字段。"
                "至少要填 goal / constraints / done / todo / blockers 里的一个。"
            )
        for name in ("constraints", "add_constraints", "done", "todo", "blockers"):
            value = arguments.get(name)
            if value is not None and not isinstance(value, list):
                raise ToolError(f"参数 {name} 应当是字符串数组")

        changed = state.apply(
            goal=arguments.get("goal"),
            add_constraints=added or None,
            done=arguments.get("done"),
            todo=arguments.get("todo"),
            blockers=arguments.get("blockers"),
        )
        if not changed:
            return ToolResult(output="任务状态与这次给的一样，没有变化。")
        return ToolResult(
            output=f"任务状态已更新（v{state.version}）：\n{state.render()}",
            meta={"version": state.version, "constraints": len(state.constraints)},
        )
