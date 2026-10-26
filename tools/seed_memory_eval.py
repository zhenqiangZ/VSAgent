"""造一份**可复现**的记忆台账与召回集，用来量 recall@k 与 stale 比例。

为什么要有这一份：`eval_memory.py` 的召回集是「用户自己项目里的几件事」，
只有本机才有一份，改了打分或失效逻辑就没法比较前后。这里造的是同一类东西里
的**合成版**：事实本身是虚拟筛选领域的常规内容（靶点编号、盒子参数、配体库
规模、对接参数……），重点是刻意混进来的四类难例——

- **事实更新**：同一份引用上写了新值（带「改为 / 现在」这类更新词），
  旧条目必须被标成 `superseded_by` 而不是一起留在召回里；
- **互相矛盾**：两条结论对立、引用不同，都不该被静默丢掉，排序上新的要在前；
- **引用被删**：写完把文件删掉，条目要变成 `missing` 并且**不参与召回**；
- **过期**：`expires_at` 过了的条目同样不参与召回。

用法（产物全在 `.probe/` 下，已 gitignore，所以「评测集不进版本控制」这条不变：
进版本控制的是**怎么造**，不是造出来的那一份）：

```
python tools/seed_memory_eval.py
python tools/eval_memory.py --root .probe/memory-eval --cases .probe/memory-eval/cases.jsonl
```
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opencode_tui.core.agent.memory import MemoryStore  # noqa: E402

# 台账正文。`(正文, 引用)`；引用是相对台账根的真实文件，agent 写入时它必须存在。
FACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("靶点人 EGFR 对应 UniProt P00533，全长 1210 个氨基酸。", ()),
    (
        "EGFR 的 4HJO 是 erlotinib 共晶结构，分辨率 2.75 Å，只用了链 A。",
        ("fixtures/4hjo/receptor.pdbqt",),
    ),
    ("4HJO 的结合口袋以 Leu718、Val726、Ala743 三个残基为界。", ()),
    (
        "KRAS G12C 用 6OIM 作为受体模板，它带 sotorasib 共价配体。",
        ("fixtures/kras/6oim.pdb",),
    ),
    ("Cav2.2 靶点是 CACNA1B，UniProt Q00975，2339 个氨基酸。", ()),
    ("α7 nAChR 是 CHRNA7 / P36544，502 个氨基酸，已有 51 个实验结构。", ()),
    ("受体准备走 meeko 加 gemmi 这条链，质子化按 pH 7.4 处理。", ()),
    ("盒子中心取共晶配体的质心，边长取配体半径再加 8 Å。", ()),
    (
        "4HJO 的受体准备产物在 receptors/4hjo/，box.txt 记着盒子参数。",
        ("fixtures/4hjo/receptor.pdbqt",),
    ),
    (
        "配体库原始 5000 个分子，取自 ChEMBL 上 EGFR 的活性子集。",
        ("fixtures/library/ligands.sdf",),
    ),
    ("粗筛先过 Lipinski 五规则，再过 PAINS 子结构过滤。", ()),
    ("去重按 InChIKey 前 14 位，重复的只留分子量最大的那个。", ()),
    ("α7 的配体库只保留季铵盐骨架，其余在导入阶段就丢掉。", ()),
    ("对接用 AutoDock Vina，exhaustiveness 16，随机种子固定 42。", ()),
    ("Cav2.2 的单次对接超时设成 30 分钟，因为它口袋大、可旋转键多。", ()),
    ("KRAS 的 G12C 口袋太浅，对接参数改成共价对接，warhead 不参与采样。", ()),
    ("亲和力打分用 VCrossFormer，输出 pK，数值越高表示结合越强。", ()),
    ("pK 与 Vina 的负分不可比：一个越大越好，一个越小越好。", ()),
    (
        "DTA 环境在 models/DTA/.venv，Python 3.10 加 torch 2.2.1，主仓一行都不 import torch。",
        (),
    ),
    (
        "运行目录写在 runs/ 下，目录名按「靶点-构象」拼，例如 4hjo-demo。",
        (),
    ),
    ("语料索引在 library.db，检索走两路词法加一路语义，再用 RRF 合并。", ()),
    ("文献入库后当次就能查，因为写盘时会顺手把新页写进索引。", ()),
    ("EGFR 的共晶配体 erlotinib 分子量是 393.4。", ()),
    # 互相矛盾的两条：引用不同，所以都不会被取代，靠排序分先后（新的在前）。
    (
        "4HJO 的 A 链是激酶域，可以直接拿来做受体。",
        ("fixtures/4hjo/receptor.pdbqt",),
    ),
    (
        "4HJO 的 B 链带有额外的配体结合位点，本项目的受体用 B 链。",
        ("fixtures/4hjo/chain-b.pdbqt",),
    ),
)

# 事实更新的两组：先写旧值，再写带更新词的同一引用。
UPDATE_OLD: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("4HJO 的盒子中心是 (12.3, 4.5, 8.1)。", ("fixtures/4hjo/box.txt",)),
    (
        "EGFR 的粗筛阈值是 IC50 小于 10 µM。",
        ("fixtures/chembl/egfr_ic50.csv",),
    ),
)
UPDATE_NEW: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "4HJO 的盒子中心已经改为 (14.0, 6.2, 9.4)，边长 22 Å。",
        ("fixtures/4hjo/box.txt",),
    ),
    (
        "EGFR 的粗筛阈值现在改成 IC50 小于 1 µM。",
        ("fixtures/chembl/egfr_ic50.csv",),
    ),
)

# 引用被删的那一条：写完就把文件删掉，它必须落成 missing 且不再被召回。
MISSING_TEXT = "4HJO 的原始 PDB 是 2026-09-30 下载的，放在 fixtures/4hjo/raw.pdb。"
MISSING_REF = "fixtures/4hjo/raw.pdb"

# 引用内容变了的那一条：留在可用状态，只在注入时带一行提醒（stale）。
# 这一条刻意写得**窄**：正文里不放 EGFR / 对接 这类全库都在用的词。台账里一条
# 内容变了的记录如果同时是「热门主题」，它会让每一道题都多带回一条 stale ——
# 那个数字量到的是「检索不精」，不是「过期记忆被当成了答案」。
STALE_TEXT = "第一轮筛选的日志在 runs/batch-01/meta.json，那一次记了 120 个姿态。"
STALE_REF = "fixtures/runs/batch-01/meta.json"

# 已经过期的那一条。
EXPIRED_TEXT = "α7 的结构预测任务在 2026-08 跑过一次，结果已经过期。"

FIXTURE_FILES: dict[str, str] = {
    "fixtures/4hjo/receptor.pdbqt": "REMARK 4HJO 链 A，2.75 A\n",
    "fixtures/4hjo/chain-b.pdbqt": "REMARK 4HJO 链 B\n",
    "fixtures/4hjo/box.txt": "center 12.3 4.5 8.1\nsize 22 22 22\n",
    "fixtures/4hjo/raw.pdb": "HEADER 4HJO 原始文件\n",
    "fixtures/kras/6oim.pdb": "HEADER 6OIM\n",
    "fixtures/chembl/egfr_ic50.csv": "molecule,ic50_uM\nA,0.4\nB,7.2\n",
    "fixtures/library/ligands.sdf": "5000 molecules\n",
    "fixtures/runs/4hjo-run/meta.json": '{"run_id": "4hjo-run", "poses": 120}\n',
    "fixtures/runs/batch-01/meta.json": '{"run_id": "batch-01", "poses": 120}\n',
}

# 人工标注的召回集：一条问题对应「答案里该出现的可辨片段」。
# 片段不放 id —— id 带时间戳，台账重建一次就全变了。
CASES: tuple[dict[str, list[str] | str], ...] = (
    {"q": "人 EGFR 的 UniProt 编号是什么", "expect": ["P00533"]},
    {"q": "4HJO 的分辨率是多少", "expect": ["2.75"]},
    {"q": "4HJO 的口袋有哪些残基", "expect": ["Leu718"]},
    {"q": "KRAS G12C 用哪个结构当模板", "expect": ["6OIM"]},
    {"q": "Cav2.2 对应哪个 UniProt", "expect": ["Q00975"]},
    {"q": "α7 nAChR 有多少个氨基酸", "expect": ["502"]},
    {"q": "受体准备用什么工具链", "expect": ["meeko"]},
    {"q": "盒子边长怎么定", "expect": ["配体半径再加 8"]},
    {"q": "配体库一共有多少分子", "expect": ["5000"]},
    {"q": "粗筛用什么过滤规则", "expect": ["Lipinski"]},
    {"q": "去重按什么键", "expect": ["InChIKey"]},
    {"q": "对接的 exhaustiveness 设成多少", "expect": ["16"]},
    {"q": "Cav2.2 对接超时多久", "expect": ["30 分钟"]},
    {"q": "亲和力打分输出什么指标", "expect": ["pK"]},
    {"q": "pK 和 Vina 分数能直接比吗", "expect": ["不可比"]},
    {"q": "DTA 环境在哪个目录", "expect": ["models/DTA/.venv"]},
    {"q": "运行目录怎么命名", "expect": ["靶点-构象"]},
    {"q": "语料检索走几路", "expect": ["两路词法"]},
    {"q": "4HJO 的盒子中心现在是多少", "expect": ["(14.0, 6.2, 9.4)"]},
    {"q": "EGFR 的粗筛阈值现在是多少", "expect": ["小于 1 µM"]},
    {"q": "4HJO 的受体该用哪条链", "expect": ["本项目的受体用 B 链"]},
    {"q": "第一轮筛选记了多少个姿态", "expect": ["120 个姿态"]},
)


def write_fixtures(root: Path) -> None:
    """把被引用的文件先建出来：agent 写入要求引用真实存在。"""
    for name, text in FIXTURE_FILES.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def seed(root: Path) -> MemoryStore:
    """按「正常 → 更新 → 矛盾 → 删除 → 过期」的顺序建台账。"""
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    write_fixtures(root)

    store = MemoryStore(root)
    for text, refs in FACTS:
        store.remember(text, list(refs), source="agent", session_id="seed", turn=1)
    for text, refs in UPDATE_OLD:
        store.remember(text, list(refs), source="agent", session_id="seed", turn=2)
    for text, refs in UPDATE_NEW:
        store.remember(text, list(refs), source="agent", session_id="seed", turn=3)

    store.remember(STALE_TEXT, [STALE_REF], source="agent", session_id="seed", turn=4)
    missing = store.remember(
        MISSING_TEXT, [MISSING_REF], source="agent", session_id="seed", turn=5
    )
    expired = store.remember(
        EXPIRED_TEXT,
        [],
        source="agent",
        session_id="seed",
        turn=6,
        expires_at=time.time() - 86400,
    )

    # 让两条难例真的变成难例：删掉被引用的文件、改掉被引用的内容。
    (root / MISSING_REF).unlink(missing_ok=True)
    meta = root / STALE_REF
    meta.write_text('{"run_id": "batch-01", "poses": 137}\n', encoding="utf-8")
    store.revalidate()

    assert missing.state == "missing" and not missing.is_live(), "被删引用没有落成 missing"
    assert not expired.is_live(), "过期条目仍然算可用"
    superseded = [entry for entry in store.entries.values() if entry.superseded_by]
    assert len(superseded) == len(UPDATE_OLD), (
        f"更新那两条没有把旧条目标失效：只有 {len(superseded)} 条被取代"
    )
    return store


def write_cases(root: Path) -> Path:
    """评测集写到台账旁边；它是代码生成的，所以不进版本控制也丢不了。"""
    target = root / "cases.jsonl"
    lines = [
        json.dumps({"q": case["q"], "expect": case["expect"]}, ensure_ascii=False)
        for case in CASES
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="造记忆评测台账（离线、可复现）")
    parser.add_argument("--root", default=str(PROJECT_ROOT / ".probe" / "memory-eval"))
    args = parser.parse_args()
    root = Path(args.root)

    store = seed(root)
    cases = write_cases(root)

    entries = list(store.entries.values())
    live = [entry for entry in entries if entry.is_live()]
    superseded = [entry for entry in entries if entry.superseded_by]
    missing = [entry for entry in entries if entry.state == "missing"]
    expired = [entry for entry in entries if entry.expires_at and not entry.is_live()]
    print(f"台账目录　{store.root}")
    print(f"条目　　　{len(entries)} 条，其中生效 {len(live)} 条")
    print(
        f"难例　　　被取代 {len(superseded)}　引用丢失 {len(missing)}　已过期 {len(expired)}"
        "　内容变化（stale，仍可用）"
        f" {sum(1 for entry in entries if entry.state == 'stale')}"
    )
    print(f"评测集　　{cases}（{len(CASES)} 条）")
    print()
    print("跑评测：")
    print(f"  python tools/eval_memory.py --root {root} --cases {cases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
