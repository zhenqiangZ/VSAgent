from __future__ import annotations

import ctypes
from pathlib import Path

# 按依赖顺序加载：cudnn 依赖 cublas，cublas 依赖 cudart。
_LIBRARIES = (
    "cuda_runtime/lib/libcudart.so.12",
    "cublas/lib/libcublasLt.so.12",
    "cublas/lib/libcublas.so.12",
    "cudnn/lib/libcudnn.so.9",
    "cufft/lib/libcufft.so.11",
    "curand/lib/libcurand.so.10",
)

_state: bool | None = None


def _library_root() -> Path | None:
    """onnxruntime 旁边的 `nvidia/`（pip 安装的那份 CUDA 库）。"""
    import onnxruntime

    root = Path(onnxruntime.__file__).resolve().parent.parent / "nvidia"
    return root if root.is_dir() else None


def preload() -> bool:
    """把 pip 安装的 CUDA 库挂进进程，返回 CUDA 是否可用。

    onnxruntime 用 `dlopen` 按名字找 libcudnn.so.9 一类文件，而 pip 把库放在
    `site-packages/nvidia/*/lib`，安装目录不在动态链接器的搜索路径里。先以
    `RTLD_GLOBAL` 加载一遍，后面 onnxruntime 就能按名字找到同一份库。
    六个库缺一个就按 CPU 处理：半套运行库会在推理时抛出，而那时已经进了检索。
    """
    global _state
    if _state is not None:
        return _state
    import onnxruntime

    if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
        _state = False
        return _state
    root = _library_root()
    if root is None or any(not (root / name).is_file() for name in _LIBRARIES):
        _state = False
        return _state
    for relative in _LIBRARIES:
        ctypes.CDLL(str(root / relative), mode=ctypes.RTLD_GLOBAL)
    _state = True
    return _state


def providers() -> list[str]:
    """给 fastembed 的执行器列表。CUDA 不可用时只给 CPU。"""
    if preload():
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def device() -> str:
    """当前会用哪个执行器，只给显示用。"""
    return "GPU" if preload() else "CPU"
