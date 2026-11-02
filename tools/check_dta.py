from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.domain.affinity import (  # noqa: E402
    dta_python,
    dta_readiness,
    dta_root,
)

ok, detail = dta_readiness()
print(f"DTA：{'可用' if ok else '不可用'}")
print(f"  {detail}")
if not ok:
    raise SystemExit(1)

root = dta_root()
python = dta_python()
checks = {
    "virtual_screen.py": root / "virtual_screen.py",
    "fit_train.py": ROOT / "vsagent" / "core" / "agent" / "domain" / "fit_train.py",
}
for name, path in checks.items():
    print(f"  {name}：{path}（{'在' if path.is_file() else '不在'}）")
    if not path.is_file():
        raise SystemExit(1)

code = (
    "import sys, torch, dgl; "
    "print('python', sys.version.split()[0], 'torch', torch.__version__, 'dgl', dgl.__version__)"
)
run = subprocess.run(
    [python, "-c", code],
    capture_output=True,
    text=True,
    timeout=180,
)
print((run.stdout or "").strip())
if run.returncode != 0:
    print((run.stderr or "").strip()[-2000:])
    raise SystemExit(1)
print("DTA 环境检查通过")
