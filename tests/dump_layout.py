from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from textual.widgets import Label, Static  # noqa: E402

from opencode_tui.app import OpenCodeApp  # noqa: E402
from opencode_tui.widgets.editor_pane import EditorPane  # noqa: E402

OUTPUT_DIR = ROOT / "tests" / "output"
WIDTH = 100
HEIGHT = 30


def describe(widget: object) -> str:
    """把组件的位置尺寸与内容整理成一行文本。"""
    region = getattr(widget, "region", None)
    name = type(widget).__name__
    widget_id = getattr(widget, "id", "") or "-"
    classes = " ".join(sorted(getattr(widget, "classes", ())))
    content = ""
    if isinstance(widget, (Static, Label)):
        content = str(widget.content).replace("\n", "\\n")[:60]
    region_text = f"{region.x},{region.y} {region.width}x{region.height}" if region else "-"
    return f"{name:<20} id={widget_id:<18} {region_text:<14} [{classes}] {content}"


def dump(app: OpenCodeApp, title: str) -> str:
    """导出当前屏幕的组件布局文本。"""
    lines = [f"=== {title} ({WIDTH}x{HEIGHT}) ==="]
    for widget in app.screen.walk_children(with_self=True):
        lines.append(describe(widget))
    return "\n".join(lines)


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    state = ROOT / ".probe" / "layout_state.toml"
    state.unlink(missing_ok=True)
    app = OpenCodeApp(state_path=state)
    async with app.run_test(size=(WIDTH, HEIGHT)) as pilot:
        await pilot.pause()
        (OUTPUT_DIR / "home.txt").write_text(dump(app, "HomeScreen"), encoding="utf-8")

        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.press(*list("explain this repo"))
        await pilot.press("enter")
        await pilot.pause(3.0)
        (OUTPUT_DIR / "chat.txt").write_text(dump(app, "ChatScreen"), encoding="utf-8")

        editor = app.screen.query_one(EditorPane)
        editor.text = "/help"
        await pilot.pause()
        (OUTPUT_DIR / "command_mode.txt").write_text(dump(app, "命令模式"), encoding="utf-8")

        app.execute_command("help")
        await pilot.pause()
        (OUTPUT_DIR / "help.txt").write_text(dump(app, "HelpScreen"), encoding="utf-8")

    print("已导出布局快照：")
    for path in sorted(OUTPUT_DIR.glob("*.txt")):
        print(f"  - {path.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
