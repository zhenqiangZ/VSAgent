"""生成 S1 用的两份 PDF 夹具（有书签 / 无书签）。

跑一次就够，产物直接进 `tests/fixtures/`：

    .venv/bin/python tests/fixtures/make_pdfs.py

**为什么要自己拼 PDF**：仓里没有 reportlab / fpdf，装一个只为生成两份夹具不值当。
这里只用到 PDF 最基础的那几个对象，几十行就够，而且生成物是纯文本、可读、可复查。
真实世界的版式（两栏、扫描件、Springer 模板）不靠这两份覆盖——那是 `meta.structure`
与 `meta.warning` 的活。
"""

from __future__ import annotations

from pathlib import Path

PAGES = [
    [
        "1 Introduction",
        "Epidermal growth factor receptor (EGFR) is a receptor tyrosine kinase.",
        "Mutations such as T790M confer resistance to first generation inhibitors.",
    ],
    [
        "2 Methods",
        "2.1 In vitro assay",
        "Cells were cultured and treated with gefitinib at increasing doses.",
        "IC50 values were fitted with a four parameter logistic model.",
    ],
    [
        "3 Results",
        "Osimertinib retained activity against the T790M mutant.",
        "The measured IC50 was 12 nM in the resistant cell line.",
    ],
]

OUTLINE = [
    (0, "1 Introduction", 0),
    (0, "2 Methods", 1),
    (1, "2.1 In vitro assay", 1),
    (0, "3 Results", 2),
]


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build(*, outline: bool) -> bytes:
    """拼一份 N 页 PDF。`outline=True` 时带上 /Outlines 书签树。"""
    objects: list[bytes] = []

    def add(body: str) -> int:
        objects.append(body.encode("latin-1"))
        return len(objects)

    # 先占位：1 = Catalog，2 = Pages，3 = Font，然后才是页与内容流。
    catalog_id, pages_id, font_id = 1, 2, 3
    objects = [b"", b"", b""]

    page_ids: list[int] = []
    for lines in PAGES:
        stream = ["BT /F1 11 Tf 72 732 Td 14 TL"]
        for line in lines:
            stream.append(f"({_escape(line)}) Tj T*")
        stream.append("ET")
        content = "\n".join(stream) + "\n"
        body = f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}endstream"
        content_id = add(body)
        page_id = add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        page_ids.append(page_id)

    # 字体对象占的是 3 号位，但它是最后才 add 的——按编号回填。
    font_body = (
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    outlines_id = 0
    if outline:
        # 先算每个标题的编号：父节点 + 每个条目各一个对象。
        first = len(objects) + 1
        outlines_id = add("")  # 占位，等条目编号出来再回填
        item_ids: dict[int, int] = {}
        for index, (_level, _title, _page) in enumerate(OUTLINE):
            item_ids[index] = add("")
        # 同级的下一个、子级的第一个：按层级算。
        children: dict[int, list[int]] = {}
        stack: list[int] = []
        for index, (level, _title, _page) in enumerate(OUTLINE):
            while stack and OUTLINE[stack[-1]][0] >= level:
                stack.pop()
            parent = stack[-1] if stack else None
            children.setdefault(-1 if parent is None else parent, []).append(index)
            stack.append(index)
        for parent, kids in children.items():
            for position, index in enumerate(kids):
                level, title, page = OUTLINE[index]
                parts = [
                    f"/Title ({_escape(title)})",
                    f"/Parent {item_ids[parent] if parent >= 0 else outlines_id} 0 R",
                    f"/Dest [{page_ids[page]} 0 R /Fit]",
                ]
                if position + 1 < len(kids):
                    parts.append(f"/Next {item_ids[kids[position + 1]]} 0 R")
                if position > 0:
                    parts.append(f"/Prev {item_ids[kids[position - 1]]} 0 R")
                if index in children:
                    parts.append(f"/First {item_ids[children[index][0]]} 0 R")
                    parts.append(f"/Last {item_ids[children[index][-1]]} 0 R")
                    parts.append(f"/Count {len(children[index])}")
                objects[item_ids[index] - 1] = (
                    "<< " + " ".join(parts) + " >>"
                ).encode("latin-1")
        top = children[-1]
        objects[outlines_id - 1] = (
            f"<< /Type /Outlines /First {item_ids[top[0]]} 0 R "
            f"/Last {item_ids[top[-1]]} 0 R /Count {len(OUTLINE)} >>"
        ).encode("latin-1")
        assert outlines_id == first

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[catalog_id - 1] = (
        f"<< /Type /Catalog /Pages {pages_id} 0 R"
        + (f" /Outlines {outlines_id} 0 R" if outlines_id else "")
        + " >>"
    ).encode("latin-1")
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    ).encode("latin-1")
    objects[font_id - 1] = font_body.encode("latin-1")
    assert all(objects), "每个对象都要被回填"

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def main() -> None:
    target = Path(__file__).resolve().parent
    (target / "paper-outline.pdf").write_bytes(build(outline=True))
    (target / "paper-flat.pdf").write_bytes(build(outline=False))
    for name in ("paper-outline.pdf", "paper-flat.pdf"):
        print(name, (target / name).stat().st_size, "bytes")


if __name__ == "__main__":
    main()
