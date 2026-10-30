#!/usr/bin/env python3
"""`vsagent.core.agent.domain.fit_train` 的命令行壳。

实现在包里（打包后 `tools/` 不在 exe 解包目录中），这里只保留一个可以直接敲的入口：

```bash
models/DTA/.venv/bin/python tools/fit_target.py <spec.json>
```
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vsagent.core.agent.domain.fit_train import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
