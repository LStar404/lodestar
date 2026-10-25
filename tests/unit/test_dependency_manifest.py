"""依赖清单回归（Review #4）：`python-multipart` 缺失时 FastAPI 的 UploadFile 路由
在注册阶段就抛 RuntimeError，整个服务起不来。它必须出现在正式依赖清单与两个启动脚本里。

启动脚本是 GBK 编码的 .bat：按字节读、优先 UTF-8、失败回退 GBK 解码，
断言只依赖 ASCII 关键字（python-multipart / multipart / fastapi），不受编码影响。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]            # note-gen/
WORKSPACE = ROOT.parent                               # 上级目录（用户桌面布局的启动脚本所在）
LAUNCHER_INSIDE = ROOT / "scripts" / "启动自动笔记软件.bat"
# 外层启动脚本不在仓库里（桌面工作区特有）；克隆/CI 环境不存在时相关断言跳过，
# 但仓库内的 scripts 启动脚本与两个依赖清单必须始终正确。
LAUNCHER_OUTSIDE = WORKSPACE / "启动自动笔记软件.bat"


def existing_launchers() -> list[Path]:
    out = [LAUNCHER_INSIDE] if LAUNCHER_INSIDE.exists() else []
    if LAUNCHER_OUTSIDE.exists():
        out.append(LAUNCHER_OUTSIDE)
    return out


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _dep_name(spec: str) -> str:
    """'python-multipart>=0.0.9' → 'python-multipart'（只取包名，忽略版本约束）。"""
    for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
        spec = spec.split(sep)[0]
    return spec.strip().lower()


def test_requirements_declares_python_multipart():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = [_dep_name(line) for line in text.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert "python-multipart" in names, f"requirements.txt 缺少 python-multipart: {names}"


def test_pyproject_declares_python_multipart():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = [_dep_name(dep) for dep in data["project"]["dependencies"]]
    assert "python-multipart" in names, f"pyproject.toml 缺少 python-multipart: {names}"


def test_launchers_probe_and_install_python_multipart():
    launchers = existing_launchers()
    assert LAUNCHER_INSIDE in launchers, f"仓库内启动脚本不存在: {LAUNCHER_INSIDE}"
    for bat in launchers:
        lines = _read_text(bat).splitlines()
        probe = [line for line in lines if '-c "import' in line]
        assert probe, f"{bat} 找不到依赖探测行（python -c \"import ...\"）"
        assert any("multipart" in line for line in probe), (
            f"{bat} 依赖探测行未包含 multipart（探测缺失才会触发安装）: {probe}")
        backup = [line for line in lines if "pip install" in line and "fastapi" in line]
        assert backup, f"{bat} 找不到备用 pip 安装行"
        assert any("python-multipart" in line for line in backup), (
            f"{bat} 备用安装列表未包含 python-multipart: {backup}")


def test_launchers_stay_in_sync():
    """两个启动脚本（仓库内 + 桌面外层）当前完全一致，必须保持同步（避免只改一处）。

    外层脚本不在 git 里：克隆/CI 环境没有它时跳过本条（生产依赖声明由上面两条保证）。
    """
    if not LAUNCHER_OUTSIDE.exists():
        pytest.skip("外层启动脚本不在仓库内（克隆/CI 环境），跳过双份同步检查")
    assert _read_text(LAUNCHER_INSIDE) == _read_text(LAUNCHER_OUTSIDE), \
        "note-gen/scripts 与上级目录的两个启动脚本必须保持内容一致"
