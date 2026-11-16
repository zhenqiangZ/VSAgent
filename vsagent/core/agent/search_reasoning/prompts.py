"""搜索增强推理各阶段的提示文字。

提示集中在一处：五个阶段共用同一套 JSON 约定与措辞，改一处就能一起生效。
每个提示都要求「只输出 JSON」，方便调用方直接解析；解析失败的兜底不在提示里，
由调用方抛 `SearchReasoningError`。
"""

from __future__ import annotations

import json

PRIOR_SYSTEM = """外部资料正在取回。你先按自己已有的理解回答用户的问题，再列出这份回答依赖哪些说法。

只输出一个 JSON 对象，不要写解释文字，不要用代码块围栏：
{
  "initial_answer": "一段可以独立回答用户问题的完整文字",
  "claims": [
    {"id": "c1", "content": "一条可以被单独核对的说法", "category": "method|fact|mechanism|prediction|task", "confidence": 0.0,
     "external": true}
  ],
  "knowledge_gaps": ["现在还缺什么", "..."],
  "queries": ["后续要查的问题", "..."]
}

要求：
- 这一份是检索结果返回之前的先验答案，要能独立回答用户问题，不要写成「等查完再回答」。
- 不要猜测检索结果，也不要在正文里提到「正在检索」。
- 说法给 3 到 7 条，每条一句话，能被一条证据支持或反驳。
- 不要因为存在查询能力就把所有说法都标成待查；只有真正依赖外部信息的才写 external=true。
- 知识缺口至少一条。
- 查询问题按缺口给，最多 5 条。
"""

PRIOR_USER = """用户问题：{question}

任务状态：
{task}

工具调用之前已经写下的内容：
{narration}

上一轮助手答复：
{recent}

已有记忆摘要：
{memory}

只输出 JSON。"""

ASSESS_SYSTEM = """你在判断几条查询结果与几条说法的关系。只输出 JSON：
{
  "assessments": [
    {"evidence_id": "e1", "related_claim_ids": ["c1"], "relation": "supporting|supplementary|contradictory|irrelevant",
     "support_level": "direct|partial|contextual|none", "integration_action": "retain|append|strengthen|revise|reject",
     "rationale": "一句话说明依据"}
  ]
}

判据：
- supporting：证据直接给出与说法同方向的结论；
- supplementary：证据只提供背景或一部分信息；
- contradictory：证据给出相反结论；
- irrelevant：与任何说法都没有对应关系。
- 相关但不支持具体说法的结果，不能判成 supporting。
"""

ASSESS_USER = """问题：{question}

说法：
{claims}

证据：
{evidence}

只输出 JSON。"""

INTEGRATOR_SYSTEM = """你在一份已经写好的回答上做选择性整合。查询结果只补充或校正说法，不接管回答。

只输出一个 JSON 对象，不要写解释文字，不要用代码块围栏：
{
  "final_answer": "按原结构组织的完整回答",
  "claims": [
    {"id": "c1", "content": "整合后的说法", "category": "method|fact|mechanism|prediction|task",
     "status": "supported|partially_supported|unverified|conflicted|revised|rejected",
     "evidence_ids": ["e1"], "action": "retain|append|strengthen|revise|reject",
     "rationale": "为什么这样处理"}
  ],
  "conflicts": [
    {"topic": "冲突的是什么", "sides": ["来源一", "来源二"], "condition": "在什么条件下各自成立"}
  ]
}

规则：
- 没有挂到具体说法的证据，不允许改动说法。
- 只改有动作的说法，其余原样保留；最终答案按原结构组织，不要把证据摘要直接拼在末尾。
- 相反证据要保留双方来源，结论写成有条件的表述。
- 预测与推断不能写成实验事实。
- 每条改动写明依据的证据编号。
"""

INTEGRATOR_USER = """用户问题：{question}

先验答案（第一次检索结果返回前写下）：
{answer}

说法清单：
{claims}

证据与评估：
{evidence}

只输出 JSON。"""

VERIFIER_SYSTEM = """你在交付之前核对一份回答。只检查四件事，只输出 JSON：
{
  "passed": true,
  "issues": [
    {"kind": "completeness|grounding|change|limit", "claim_id": "c1", "text": "问题说明"}
  ],
  "semantic": [
    {"claim_id": "c1", "evidence_id": "e1", "consistent": true, "note": "方向是否一致"}
  ]
}

四件事：
- 完整性：初始说法每一条都要有去向（保留、修订、拒绝），拒绝要写理由。
- 来源：外部事实类说法必须有证据编号；没有来源的标为未核实。
- 改变：没有证据支持的改动要指出来。
- 任务限制：用户明确禁止的事情，答案里不能出现对应结果。
"""

VERIFIER_USER = """用户问题：{question}

任务限制：
{limits}

初始说法：
{initial}

当前说法：
{current}

最终答案：
{answer}

只输出 JSON。"""


def dumps(value: object) -> str:
    """把提示里要嵌入的对象写成紧凑 JSON。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
