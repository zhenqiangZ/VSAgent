from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.domain.interactions import compute  # noqa: E402

receptor = ROOT / "runs" / "A-312" / "receptor.pdb"
pose = ROOT / "runs" / "A-312" / "ligands" / "pose-01.pdb"
if not receptor.is_file() or not pose.is_file():
    print(f"缺少检查用的真实坐标：{receptor} / {pose}")
    raise SystemExit(2)

result = compute(
    receptor.read_text(encoding="utf-8"),
    pose.read_text(encoding="utf-8"),
    want_plip=True,
)
if result.plip_ok:
    print(f"PLIP 正常：{len(result.interactions)} 条作用，{len(result.contacts)} 个接触残基")
    raise SystemExit(0)
print(f"PLIP 没算成：{result.plip_reason}")
raise SystemExit(1)
