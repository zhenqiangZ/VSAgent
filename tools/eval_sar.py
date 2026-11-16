"""搜索增强推理的离线评测：读答案状态文件，打印六项指标与失败项。

不调用模型、不重跑任务。它只回答「这一轮记录下来的结果是什么样」，
合格与否由人判断。

    python tools/eval_sar.py --session ses_0037
    python tools/eval_sar.py --session ses_0037 --state ~/.vsagent/state.toml
    python tools/eval_sar.py --all

六项指标：

- 检索相关度：证据 `metadata.relevance` 的平均值；
- 证据对应：有效评估里挂到具体说法的比例；
- 说法有来源比例：外部事实类说法里带证据编号的比例；
- 原说法保留比例：内容没有改动的初始说法比例；
- 任务覆盖：修订后的说法覆盖了多少初始说法；
- 没有理由的改变：改了内容却没有证据支撑的比例。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vsagent.core.agent.search_reasoning.answer_state import answers_directory
from vsagent.core.session_manager import default_state_path

GROUNDED_CATEGORIES = frozenset({"method", "fact", "mechanism"})
GROUNDED_STATUSES = frozenset({"supported", "partially_supported", "revised"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="读答案状态文件，打印搜索增强推理的六项指标")
    parser.add_argument("--session", default="", help="会话编号")
    parser.add_argument("--state", default="", help="状态文件，默认 ~/.vsagent/state.toml")
    parser.add_argument("--all", action="store_true", help="扫描全部会话")
    return parser.parse_args()


def _answers_root(state_path: Path) -> Path:
    """答案状态默认与状态文件同级：`<状态目录>/answers`。"""
    return answers_directory(state_path.parent)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"读不出 {path}：{error}") from error


def metrics(data: dict) -> dict[str, float]:
    """按一份答案状态算六项指标。"""
    claims = data.get("revised_claims") or data.get("initial_claims") or []
    initial = data.get("initial_claims") or []
    evidence = data.get("evidence_pool") or []
    assessments = data.get("assessments") or []

    scores = [
        float(item.get("metadata", {}).get("relevance") or 0.0)
        for item in evidence
        if isinstance(item.get("metadata"), dict)
    ]
    relevance = sum(scores) / len(scores) if scores else 0.0

    useful = [item for item in assessments if item.get("relation") != "irrelevant"]
    linked = [item for item in useful if item.get("related_claim_ids")]
    correspondence = len(linked) / len(useful) if useful else 0.0

    external = [
        item
        for item in claims
        if item.get("category") in GROUNDED_CATEGORIES
        and item.get("status") in GROUNDED_STATUSES
    ]
    sourced = [item for item in external if item.get("evidence_ids")]
    sourced_ratio = len(sourced) / len(external) if external else 1.0

    before = {str(item.get("id")): item for item in initial}
    after = {str(item.get("id")): item for item in claims}
    kept = 0
    changed_without_reason = 0
    for claim_id, old in before.items():
        new = after.get(claim_id)
        if new is None:
            continue
        if str(new.get("content") or "").strip() == str(old.get("content") or "").strip():
            kept += 1
        elif not new.get("evidence_ids"):
            changed_without_reason += 1
    retention = kept / len(before) if before else 1.0
    coverage = len([item for item in before if item in after]) / len(before) if before else 1.0
    unjustified = changed_without_reason / len(before) if before else 0.0

    return {
        "retrieval_relevance": round(relevance, 3),
        "evidence_correspondence": round(correspondence, 3),
        "sourced_external_ratio": round(sourced_ratio, 3),
        "claim_retention": round(retention, 3),
        "task_coverage": round(coverage, 3),
        "unjustified_change": round(unjustified, 3),
    }


def failures(data: dict) -> list[str]:
    """列出值得人看一眼的项：没有来源的事实、没挂说法的有效评估、未解决冲突。"""
    rows: list[str] = []
    claims = data.get("revised_claims") or data.get("initial_claims") or []
    for item in claims:
        if (
            item.get("category") in GROUNDED_CATEGORIES
            and item.get("status") in GROUNDED_STATUSES
            and not item.get("evidence_ids")
        ):
            rows.append(f"说法 {item.get('id')} 标成 {item.get('status')} 但没有证据")
        if item.get("status") == "unverified":
            rows.append(f"说法 {item.get('id')} 仍是未核实：{str(item.get('content') or '')[:40]}")
    for item in data.get("assessments") or []:
        if item.get("relation") != "irrelevant" and not item.get("related_claim_ids"):
            rows.append(f"证据 {item.get('evidence_id')} 判成 {item.get('relation')} 但没挂说法")
    for item in data.get("conflicts") or []:
        rows.append(f"未解决冲突：{item.get('topic') or item}")
    return rows


def report_one(path: Path) -> None:
    data = _load(path)
    print(f"\n{path}")
    print(f"  模式：{data.get('mode')}　状态：{data.get('status')}　修订：{data.get('revision')}")
    print(
        f"  基线：{data.get('baseline_source') or '—'}　触发："
        f"{data.get('trigger_tool') or '—'} / 第 {data.get('trigger_step') or 0} 步"
    )
    print(f"  说法 {len(data.get('initial_claims') or [])} 条 → "
          f"{len(data.get('revised_claims') or [])} 条；"
          f"证据 {len(data.get('evidence_pool') or [])} 条；"
          f"评估 {len(data.get('assessments') or [])} 条")
    for name, value in metrics(data).items():
        print(f"  {name:<26}{value}")
    items = failures(data)
    if items:
        print("  失败项：")
        for item in items[:12]:
            print(f"    - {item}")
    else:
        print("  失败项：没有")


def main() -> int:
    args = parse_args()
    state_path = Path(args.state).expanduser() if args.state else default_state_path()
    if args.all:
        files = sorted(_answers_root(state_path).rglob("ans_*.json"))
        if not files:
            print(f"没有答案状态文件：{_answers_root(state_path)}")
            return 1
        for path in files:
            report_one(path)
        return 0
    if not args.session:
        print("给 --session 或 --all", file=sys.stderr)
        return 2
    files = sorted((_answers_root(state_path) / args.session).glob("ans_*.json"))
    if not files:
        print(f"这个会话没有答案状态文件：{_answers_root(state_path) / args.session}")
        return 1
    report_one(files[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
