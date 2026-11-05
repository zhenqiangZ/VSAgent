from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    get_package_paths,
)

root = Path(SPECPATH)

# 静态资产要跟着包走。`web/` 是网页端（index.html / pages / assets），
# 加进 `datas` 之后冻结产物里才有它——`service.web_root()` 按 `sys._MEIPASS` 找。
# 漏了它的话服务照常起来，只是页面 404，而 `open_viewer` 仍给得出地址，
# 现象看着像「前端没写」，实际是没打进去。所以打包后要查
# `build/vsagent/Analysis-00.toc`（不是 `strings`，PKG 段是压缩的）。
datas = [
    (str(root / "vsagent" / "styles" / "app.tcss"), "vsagent/styles"),
    (str(root / "web"), "web"),
]

# `fit_affinity` 把 `fit_train.py` 当脚本交给 DTA 环境的解释器跑，路径是
# `Path(__file__).with_name("fit_train.py")`。冻结后 `__file__` 指向解包目录，
# 而脚本本身不会被 import、因此不在 PYZ 里，于是子进程报
# `can't open file '/tmp/_MEI.../vsagent/core/agent/domain/fit_train.py'`。
# 它一行都不 import torch/rdkit，整份放进 datas 即可。
datas.append(
    (
        str(root / "vsagent" / "core" / "agent" / "domain" / "fit_train.py"),
        "vsagent/core/agent/domain",
    )
)

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

# PLIP 与 openbabel 的数据、动态库。冻结版里 `from plip.structure.preparation import
# PDBComplex` 会抛 `ValueError: not enough values to unpack (expected 2, got 1)`，
# 源码环境同一次导入正常。那一行来自 `pybel._formatstodict`：它把每个格式拆成
# 「名字 -- 说明」，拆不出两个值时当场抛出。格式表由插件提供，而插件
# （`openbabel/lib/openbabel/<版本>/*.so`）既不在 `collect_data_files` 的结果里
# （它跳过 .so），也不在 `collect_dynamic_libs` 的结果里（只扫 `lib/` 第一层）。
# 少了它们，openbabel 加载不到任何格式，导入期就崩在这一行。
datas += collect_data_files("plip")
datas += collect_data_files("openbabel")
binaries = collect_dynamic_libs("openbabel")

_, _openbabel_dir = get_package_paths("openbabel")
_plugin_root = Path(_openbabel_dir) / "lib" / "openbabel"
datas += [
    (str(item), str(Path("openbabel/lib/openbabel") / item.parent.name))
    for item in sorted(_plugin_root.glob("*/*.so"))
]

# RDKit 的 `Data/` 目录。PyInstaller 自带与 `_pyinstaller_hooks_contrib` 里都没有 rdkit 钩子，
# 冻结后 `_MEIPASS/rdkit/` 底下只有 `Chem` / `DataStructs` / `ForceField` / `Geometry` /
# `ML` 与 `rdBase.so`，没有 `Data`。`RDConfig.RDDataDir` 由 `rdkit.__file__` 推出来，
# 指到 `_MEIPASS/rdkit/Data`，于是 `pharmacophore._factory()` 抛
# `OSError: File: /tmp/_MEI.../rdkit/Data/BaseFeatures.fdef could not be opened.`
# 药效团构建与筛选都走这一处（`pharmacophore.py` 的 `feature_points`），
# `build_pharmacophore` 要先生成完全部构象、再逐个叠合，到提特征这一步才抛出——
# 前面几分钟的计算全部白做，界面上还停在「执行中」。整个 `Data` 只有 38 个文件、
# 2 MB，一次收齐，省得以后哪个 RDKit 功能又要读里面的 `Crippen.txt` 或 `Pains`。
datas += collect_data_files("rdkit", subdir="Data")

a = Analysis(
    [str(root / "vsagent" / "__main__.py")],
    pathex=[str(root)],
    binaries=binaries,
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
    name="VSAgent",
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
