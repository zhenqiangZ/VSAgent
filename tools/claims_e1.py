"""把一次实验的登记表解析成 Claim 1–3 的产物映射。

用法：
    python tools/claims_e1.py result/E1-egfr-r1

写出 `result/E1-egfr-r1/claims.json`，交给 `tools/artifact_index.py`。
角色按登记表里的 `meta.kind` / `meta.stage` 认，不按编号认——编号每次运行都会变。
缺任何一个角色就当场退出，不生成一份「少了东西但看起来正常」的索引。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def handle_text(handle: dict) -> str:
    return f"{handle['kind']}#{handle['seq']}"


def load_ledger(exp: Path) -> list[dict]:
    paths = sorted((exp / "home" / ".vsagent" / "ledgers").glob("*.json"))
    if not paths:
        raise SystemExit(f"{exp} 下没有登记表")
    entries: list[dict] = []
    for path in paths:
        entries.extend(json.loads(path.read_text(encoding="utf-8")).get("entries") or [])
    return entries


def pick(entries: list[dict], predicate, role: str) -> str:
    for entry in entries:
        if predicate(entry):
            return handle_text(entry["handle"])
    raise SystemExit(f"登记表里没有 {role}")


def survey_file(exp: Path) -> str:
    root = exp / "home" / ".vsagent" / "surveys"
    candidates = sorted(root.glob("*/survey.json"), key=lambda item: item.stat().st_mtime)
    if not candidates:
        raise SystemExit("没有结构全景文件 survey.json")
    return str(candidates[-1].relative_to(exp))


def run_dirs(exp: Path) -> list[str]:
    root = exp / "runs"
    found = [item for item in sorted(root.glob("*")) if (item / "meta.json").is_file()]
    if not found:
        raise SystemExit("没有写出运行目录")
    return [str(item.relative_to(exp)) for item in found]


def data_dirs(exp: Path) -> list[str]:
    """联网取数的落盘目录：把数据快照一并纳入索引。"""
    out = []
    for relative in ("home/.vsagent/chembl", "home/.vsagent/surveys", "home/.vsagent/structures"):
        if (exp / relative).is_dir():
            out.append(relative)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 E1 的 claim 映射")
    parser.add_argument("experiment", help="实验目录，例如 result/E1-egfr-r1")
    args = parser.parse_args()

    exp = Path(args.experiment).resolve()
    entries = load_ledger(exp)

    def meta(entry: dict) -> dict:
        return entry.get("meta") or {}

    roles = {
        "structure_raw": pick(
            entries,
            lambda e: e["handle"]["kind"] == "receptor" and meta(e).get("stage") == "raw",
            "原始结构 receptor#*（stage=raw）",
        ),
        "receptor_prepared": pick(
            entries,
            lambda e: e["handle"]["kind"] == "receptor"
            and meta(e).get("stage") == "prepared",
            "准备后的受体 receptor#*（stage=prepared）",
        ),
        "pocket_view": pick(
            entries,
            lambda e: e["handle"]["kind"] == "artifact" and meta(e).get("view") == "pockets",
            "口袋查看入口 artifact#*（view=pockets）",
        ),
        "ligands_actives": pick(
            entries,
            lambda e: e["handle"]["kind"] == "ligandset"
            and meta(e).get("stage") == "actives",
            "ChEMBL 活性配体集 ligandset#*（stage=actives）",
        ),
        "ligands_prepared": pick(
            entries,
            lambda e: e["handle"]["kind"] == "ligandset"
            and meta(e).get("stage") == "prepared",
            "准备后的配体集 ligandset#*（stage=prepared）",
        ),
        "docking": pick(
            entries,
            lambda e: meta(e).get("kind") == "docking",
            "对接产物 artifact#*（kind=docking）",
        ),
        "model": pick(
            entries,
            lambda e: meta(e).get("kind") == "model",
            "亲和力模型 artifact#*（kind=model）",
        ),
        "affinity": pick(
            entries,
            lambda e: meta(e).get("kind") == "affinity",
            "亲和力打分 artifact#*（kind=affinity）",
        ),
        "candidates": pick(
            entries,
            lambda e: meta(e).get("kind") == "candidates",
            "候选表 artifact#*（kind=candidates）",
        ),
    }

    survey = survey_file(exp)
    runs = run_dirs(exp)
    claims = {
        "C1": {
            "statement": "从用户交办走到候选分子、策略假设、机制解释与交付产物。",
            "artifacts": [
                roles["docking"],
                roles["model"],
                roles["affinity"],
                roles["candidates"],
            ],
            "files": runs,
        },
        "C2": {
            "statement": "结构与口袋的选择由实际检索到的结构证据决定。",
            "artifacts": [
                roles["structure_raw"],
                roles["receptor_prepared"],
                roles["pocket_view"],
            ],
            "files": [survey],
        },
        "C3": {
            "statement": "每一项关键判断都能追溯到工具调用与实际文件。",
            "artifacts": [
                roles["structure_raw"],
                roles["receptor_prepared"],
                roles["pocket_view"],
                roles["ligands_actives"],
                roles["ligands_prepared"],
                roles["docking"],
                roles["model"],
                roles["affinity"],
                roles["candidates"],
            ],
            "files": [survey, *runs, *data_dirs(exp)],
        },
    }

    target = exp / "claims.json"
    target.write_text(json.dumps(claims, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {target}")
    for name, handle in roles.items():
        print(f"  {name:18s} {handle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
