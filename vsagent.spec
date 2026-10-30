from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH)

# 静态资产要跟着包走。`web/` 是网页端（index.html / pages / assets），
# 加进 `datas` 之后冻结产物里才有它——`service.web_root()` 按 `sys._MEIPASS` 找。
# 漏了它的话服务照常起来，只是页面 404，而 `open_viewer` 仍给得出地址，
# 现象看着像「前端没写」，实际是没打进去。所以打包后要查
# `build/opencode_tui/Analysis-00.toc`（不是 `strings`，PKG 段是压缩的）。
datas = [
    (str(root / "opencode_tui" / "styles" / "app.tcss"), "opencode_tui/styles"),
    (str(root / "web"), "web"),
]

# 第三方包**自己带的**数据文件不会自动进包：`_pyinstaller_hooks_contrib` 里没有 meeko 钩子，
# 而 meeko 是按 `Path(__file__).parents[0] / "data" / "params"` 与 `files("meeko") / "data"`
# 找那些 json / toml 的。冻结后这两个路径都指到 `_MEIPASS/meeko/...` 底下，不把文件带进去
# 就是个空目录。现象是 `prepare_receptor` 抛
# `ValueError: names passed to 'load_atom_params' need to suffixed with .json ... name was ad4_types`
# —— `preparation.py` 在类体里拿 `params_dir.glob("*.json")` 建 `packaged_params`，
# 扫不到文件就成空字典，于是默认值 `load_atom_params="ad4_types"` 谁都认不出来。
# 这还不是最后一处：`polymer.py` 之后还要读 `template_charges.json` 等三个，所以整个
# `data/` 都得带。`subdir="data"` 收 12 个文件（4 个顶层 json + params 下 8 个 + metal_vdw.toml）。
datas += collect_data_files("meeko", subdir="data")

a = Analysis(
    [str(root / "opencode_tui" / "__main__.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="opencode-tui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
