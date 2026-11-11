"""把一次实验的登记表整理成可核对的产物索引。

输入是实验目录（`result/<exp>`）与一份 claim 映射；输出是
`result/<exp>/artifact-index.json`。索引里每个 handle 都带路径、类型、字节数与
SHA256，目录按「相对路径 + 文件摘要」的清单再摘要一次，所以目录里任何一个文件
变了都会变。claim 映射引用不存在的 handle 时当场退出，不做「找不到就跳过」。

用法：
    python tools/artifact_index.py result/E1-egfr-r1 --claims .probe/claims_e1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root: Path) -> tuple[str, int, int]:
    """目录摘要：先按相对路径排序，逐文件记「路径 摘要」，再对整份清单摘要一次。"""
    lines: list[str] = []
    total = 0
    count = 0
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(root).as_posix()
        digest = file_digest(item)
        size = item.stat().st_size
        lines.append(f"{relative} {size} {digest}")
        total += size
        count += 1
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), total, count


def describe(path: Path) -> dict[str, object]:
    if path.is_dir():
        digest, size, count = tree_digest(path)
        return {"form": "directory", "sha256": digest, "bytes": size, "files": count}
    if path.is_file():
        return {
            "form": "file",
            "sha256": file_digest(path),
            "bytes": path.stat().st_size,
            "files": 1,
        }
    return {"form": "missing", "sha256": "", "bytes": 0, "files": 0}


def handle_text(handle: dict) -> str:
    return f"{handle['kind']}#{handle['seq']}"


def build(exp: Path, claims_path: Path | None) -> dict[str, object]:
    ledgers = sorted((exp / "home" / ".vsagent" / "ledgers").glob("*.json"))
    if not ledgers:
        raise SystemExit(f"{exp} 下没有登记表，先确认这是一次跑完的实验")

    entries: list[dict[str, object]] = []
    known: set[str] = set()
    for ledger_path in ledgers:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        for entry in data.get("entries", []):
            handle = handle_text(entry["handle"])
            path = Path(str(entry.get("path") or ""))
            record = {
                "handle": handle,
                "path": str(path),
                "summary": entry.get("summary", ""),
                "fingerprint": entry.get("fingerprint", ""),
                "file_state": entry.get("file_state", ""),
                "parents": [handle_text(item) for item in entry.get("parents", [])],
                "meta": entry.get("meta", {}),
                "session": data.get("session_id", ""),
            }
            record.update(describe(path))
            entries.append(record)
            known.add(handle)

    claims: dict[str, object] = {}
    if claims_path is not None:
        raw = json.loads(claims_path.read_text(encoding="utf-8"))
        for claim_id, claim in raw.items():
            wanted = list(claim.get("artifacts") or [])
            missing = [item for item in wanted if item not in known]
            if missing:
                raise SystemExit(
                    f"{claim_id} 引用了登记表里没有的 handle：{', '.join(missing)}"
                )
            files = []
            for relative in claim.get("files") or []:
                path = (exp / str(relative)).resolve()
                if not path.exists():
                    raise SystemExit(f"{claim_id} 引用的文件不存在：{relative}")
                record = {"path": str(relative)}
                record.update(describe(path))
                files.append(record)
            claims[claim_id] = {
                "statement": claim.get("statement", ""),
                "artifacts": wanted,
                "files": files,
            }

    return {
        "experiment": exp.name,
        "session_ledgers": [item.name for item in ledgers],
        "claims": claims,
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成实验的产物索引与 claim 映射")
    parser.add_argument("experiment", help="实验目录，例如 result/E1-egfr-r1")
    parser.add_argument("--claims", help="claim 映射 JSON；不给就只出产物清单")
    args = parser.parse_args()

    exp = Path(args.experiment).resolve()
    if not exp.is_dir():
        raise SystemExit(f"{exp} 不存在")
    index = build(exp, Path(args.claims) if args.claims else None)
    target = exp / "artifact-index.json"
    target.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    print(f"写出 {target}，{len(index['entries'])} 个 handle，{len(index['claims'])} 条 claim")
    return 0


if __name__ == "__main__":
    sys.exit(main())
