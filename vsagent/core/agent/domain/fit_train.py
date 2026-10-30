"""在单个靶点上微调 VCrossFormer。**必须用 `models/DTA/.venv/bin/python` 跑。**

```bash
models/DTA/.venv/bin/python -m vsagent.core.agent.domain.fit_train <spec.json>
```

两个调用方：`fit_affinity` 工具（产品路径）与 `tools/retro_affinity.py fit`（实验路径）。
spec 的字段见 `finetune.build_spec`。

**这个文件不许被 `domain_tools()` 登记**：它不是工具，是一个要交给另一个解释器跑的
脚本。放在包内是为了打包后仍然找得到——放在 `tools/` 里，exe 解包目录中不会有它。
它自己一行都不 import torch/rdkit（全在函数体里），所以 `check_chemistry_gate` 不受影响。

为什么单独一个脚本、而不是让主仓直接调 `train.py`：
`train.py` 读的是 `config/configall.yaml`，那份配置写死了 PDBbind 的路径与划分。
这里做的是「把某个靶点的 N 个化合物当成一个独立数据集」，需要另一套 `data.*`
与另一份 `split.json`。用配置文件切换比改代码干净，也不用动 `models/DTA` 的任何一行。

两件事值得单独说：

- **口袋嵌入对所有化合物是同一个值**（一个靶点只有一份口袋）。所以训练里的蛋白分支
  输入是常量，模型学到的是「在这个口袋上，配体长什么样对应多强的活性」——
  这正是「靶点内迁移学习」的定义，也是它只对这一个靶点有效的原因。
- **head-only 只训 `affinity_mlp`，但它只修尺度、不修排序。** 回顾性实验（PTP1B，
  726 个训练点）里：head-only 把 RMSE 从 1.66 压到 1.16，ρ 却从 0.077 掉到 0.045；
  全量微调把 ρ 拉到 0.385（3 个种子 0.384 ± 0.017），RMSE 0.87。**要排序就训全部参数**，
  只训最后一层学到的是「这个靶点的活性大概在什么档位」。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import sys
from pathlib import Path

DTA_ROOT = Path(__file__).resolve().parents[4] / "models" / "DTA"
sys.path.insert(0, str(DTA_ROOT))

# **把自己所在的目录从 sys.path 上摘掉。** 用脚本方式启动时 Python 会把脚本目录放在
# 最前面，而这里就是 `vsagent/core/agent/domain/`——`import torch` 内部会
# `import http.client`，那一下会命中同目录的 `domain/http.py`，于是报
# 「No module named 'httpx'」。这个坑只在「用脚本方式跑包内文件」时出现。
_HERE = Path(__file__).resolve().parent
sys.path[:] = [
    entry for entry in sys.path
    if entry and Path(entry).resolve() != _HERE
]


def _load_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def build_embeddings(spec: dict) -> tuple[Path, Path]:
    """写 `pocket_embeddings.pkl` 与 `smi_embeddings.pkl`。

    口袋只算一次再复制给每个化合物：同一靶点只有一份口袋，重复算是浪费。
    SMILES 嵌入逐个算（ChemBERTa 77M，几十毫秒一个）。
    """
    import torch
    from utils.embeddings import compute_esm_embedding, compute_smi_embedding_plain

    from infer import extract_seqs_from_pdb

    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    device = spec.get("device", "cpu")
    smiles: dict[str, str] = spec["smiles"]

    sequences = extract_seqs_from_pdb(spec["pocket"])
    print(f"口袋序列：{len(sequences)} 条链，长度 {[len(s) for s in sequences]}")
    pocket_embedding = compute_esm_embedding(sequences, device)

    pocket_path = out_dir / "pocket_embeddings.pkl"
    with open(pocket_path, "wb") as handle:
        pickle.dump({key: pocket_embedding for key in smiles}, handle)
    print(f"口袋嵌入：{tuple(pocket_embedding.shape)} × {len(smiles)}")

    # ChemBERTa 只加载一次。仓库里的 `compute_smi_embedding_plain` 每次调用都会重新
    # 加载 77M 权重（44 个分子就加载 44 次），这里按它同样的「朴素均值」算，只是复用模型。
    from transformers import AutoTokenizer, RobertaModel
    from utils.constants import CHEMBERTA_CHECKPOINT

    tokenizer = AutoTokenizer.from_pretrained(CHEMBERTA_CHECKPOINT)
    chemberta = RobertaModel.from_pretrained(
        CHEMBERTA_CHECKPOINT, add_pooling_layer=False
    ).to(device)
    chemberta.eval()

    smi_path = out_dir / "smi_embeddings.pkl"
    table = {}
    with torch.no_grad():
        for index, (key, value) in enumerate(sorted(smiles.items()), start=1):
            inputs = tokenizer(
                value, return_tensors="pt", padding=True, truncation=True
            ).to(device)
            hidden = chemberta(**inputs).last_hidden_state
            table[key] = hidden.mean(dim=1).cpu().numpy().squeeze(0)
            if index % 200 == 0:
                print(f"  SMILES 嵌入 {index}/{len(smiles)}")
    with open(smi_path, "wb") as handle:
        pickle.dump(table, handle)
    print(f"SMILES 嵌入：{next(iter(table.values())).shape} × {len(table)}")
    return pocket_path, smi_path


def write_config(spec: dict, pocket_pkl: Path, smi_pkl: Path) -> Path:
    """由 `configall.yaml` 派生一份指向本靶点数据集的配置。"""
    from omegaconf import OmegaConf

    base = OmegaConf.load(DTA_ROOT / "config" / "configall.yaml")
    out_dir = Path(spec["out_dir"])
    cfg = OmegaConf.create(OmegaConf.to_container(base, resolve=False))
    # **结尾必须带斜杠**：`DB.write_db` 用的模式是 `data_path + "**/"`，而 glob 在
    # 没有 recursive=True 时把 `**` 当普通通配符——少了斜杠只会匹配到根目录自己。
    cfg.data.data_path = str(Path(spec["data_dir"]).resolve()) + os.sep
    cfg.data.affinity_path = spec["index"]
    cfg.data.db_path = str(out_dir / "db")
    cfg.data.data_split = spec["split"]
    cfg.data.out_path = str(out_dir / "runs")
    cfg.data.map_size = 8 * 1024 ** 3
    cfg.data.pocket_embedding_path = str(pocket_pkl)
    cfg.data.smi_embedding_path = str(smi_pkl)
    cfg.model.model_save_path = str(out_dir / "model")
    cfg.model.load_model = True
    cfg.model.load_model_path = spec["base_checkpoint"]
    cfg.train.debug = False
    cfg.train.epoch = int(spec.get("epochs", 30))
    cfg.train.batch_size = int(spec.get("batch", 16))
    cfg.train.lr = float(spec.get("lr", 1e-5))
    cfg.seed = int(spec.get("seed", 444))
    cfg.device = spec.get("device", "cpu")

    path = out_dir / "config.yaml"
    OmegaConf.save(cfg, path)
    return path


def build_db(config_path: Path) -> None:
    from dataset.db import DB
    from omegaconf import OmegaConf

    cfg = OmegaConf.load(config_path)
    db = DB(
        db_path=cfg.data.db_path,
        data_path=str(cfg.data.data_path),
        affinity_path=str(cfg.data.affinity_path),
        pocket_repr_path=str(cfg.data.pocket_embedding_path),
        smi_path=str(cfg.data.smi_embedding_path),
        map_size=int(cfg.data.map_size),
    )
    db.write_db()
    with db.env.begin(write=False) as txn:
        print(f"LMDB 条目：{txn.stat()['entries']}")
    db.close_db()


def train(config_path: Path, *, head_only: bool) -> tuple[Path | None, float]:
    import torch
    from omegaconf import OmegaConf
    from task.task import Task

    cfg = OmegaConf.load(config_path)

    class _HeadOnly(Task):
        """冻结除 `affinity_mlp` 以外的全部参数。

        `Task.train()` 内部会调 `initialize()`，所以只能在这里覆写、不能在外面先冻。
        """

        def initialize(self):
            super().initialize()
            for parameter in self.model.parameters():
                parameter.requires_grad = False
            for parameter in self.model.affinity_mlp.parameters():
                parameter.requires_grad = True
            trainable = [p for p in self.model.parameters() if p.requires_grad]
            self.opt = torch.optim.Adam(trainable, lr=self.cfg.train.lr)
            print(f"head-only：可训练参数 {sum(p.numel() for p in trainable):,}"
                  f" / {sum(p.numel() for p in self.model.parameters()):,}")

    task = (_HeadOnly if head_only else Task)(cfg)
    best_rmse, best_epoch = task.train()

    candidates = sorted(glob.glob(str(Path(cfg.model.model_save_path) / "best_model_*.pth")))
    checkpoint = Path(candidates[-1]) if candidates else None

    # **训练配置指向的是基座权重**。推理必须另写一份，否则 `load_model` 会把基座
    # 再加载一遍——表现是「微调前后完全一致」，而且不会报任何错。
    if checkpoint is not None:
        from omegaconf import OmegaConf as _OC

        infer_cfg = _OC.load(config_path)
        infer_cfg.model.load_model_path = str(checkpoint)
        _OC.save(infer_cfg, Path(cfg.model.model_save_path).parent / "config.infer.yaml")
    return checkpoint, float(best_rmse)


def main() -> int:
    parser = argparse.ArgumentParser(description="单靶点微调 VCrossFormer")
    parser.add_argument("spec", help="spec.json 的路径")
    args = parser.parse_args()

    spec = _load_spec(args.spec)
    out_dir = Path(spec["out_dir"])
    for key in ("data_dir", "index", "split", "pocket", "base_checkpoint"):
        if not Path(spec[key]).exists():
            print(f"缺少 {key}：{spec[key]}", file=sys.stderr)
            return 2

    pocket_pkl, smi_pkl = build_embeddings(spec)
    config_path = write_config(spec, pocket_pkl, smi_pkl)
    build_db(config_path)
    checkpoint, best_rmse = train(config_path, head_only=bool(spec.get("head_only", True)))

    result = {
        "checkpoint": None if checkpoint is None else str(checkpoint),
        "best_val_rmse": round(best_rmse, 4),
        "config": str(config_path),
        "head_only": bool(spec.get("head_only", True)),
        "epochs": int(spec.get("epochs", 30)),
        "lr": float(spec.get("lr", 1e-5)),
        "seed": int(spec.get("seed", 444)),
    }
    (out_dir / "fit.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("FIT_RESULT " + json.dumps(result, ensure_ascii=False))
    return 0 if checkpoint else 1


if __name__ == "__main__":
    raise SystemExit(main())
