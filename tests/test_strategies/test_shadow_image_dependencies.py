"""静态守护测试：复用 btc-eth 镜像的 shadow 入口依赖必须被 Dockerfile COPY 覆盖。

背景：mtcps-xau-shadow 容器复用 trading-btc-eth 镜像，但 shadow 模块会 import
btc_eth_aggressive 等兄弟包；若 Dockerfile 漏 COPY 对应目录，容器会 crash-loop。
本测试在本地静态扫描，不启容器、不联网。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "strategies/btc_eth/Dockerfile"
SHADOW_DIR = REPO_ROOT / "strategies/btc_eth/shadow"

# 项目内顶层包前缀，只关心这两类 import
_PROJECT_PREFIXES = ("strategies.", "shared.")

# 匹配 `COPY <src> <dst>` 或 `COPY ["<src>", ..., "<dst>"]` 两种语法
# 我们只取 src（仓库侧路径），因为要判断哪些仓库顶层目录被带进镜像
_COPY_RE = re.compile(
    r'^\s*COPY\s+(?:"([^"]+)"|(\S+))'
)

# 匹配 `from <pkg> import ...` 或 `import <pkg>`，只抓项目内前缀
_FROM_IMPORT_RE = re.compile(
    r'^\s*from\s+((?:' + '|'.join(_PROJECT_PREFIXES) + r')[A-Za-z0-9_\.]*)\s+import'
)
_IMPORT_RE = re.compile(
    r'^\s*import\s+((?:' + '|'.join(_PROJECT_PREFIXES) + r')[A-Za-z0-9_\.]*)'
)


def _extract_copied_top_dirs(dockerfile: Path) -> set:
    """解析 Dockerfile 所有 COPY 行，返回被复制进镜像的仓库顶层路径集合。

    例如 COPY strategies/btc_eth_aggressive/ /app/strategies/btc_eth_aggressive/
    会产出 "strategies/btc_eth_aggressive"（末尾斜杠已剔除）。
    """
    copied = set()
    for line in dockerfile.read_text().splitlines():
        m = _COPY_RE.match(line)
        if not m:
            continue
        src = m.group(1) or m.group(2)
        if not src:
            continue
        # 去除末尾斜杠（目录写法）
        src = src.rstrip("/")
        copied.add(src)
    return copied


def _extract_project_import_top_packages(shadow_dir: Path) -> set:
    """扫描 shadow 目录所有 .py 文件，提取项目内 import 的顶层包。

    顶层包定义：strategies.<first_segment> 或 shared（shared 自身即顶层）。
    例如 `from strategies.btc_eth_aggressive.strategy import ...` → "strategies/btc_eth_aggressive"
    """
    imported_top = set()
    for py_file in sorted(shadow_dir.rglob("*.py")):
        text = py_file.read_text()
        for line in text.splitlines():
            # 跳过注释行
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            pkg = None
            m = _FROM_IMPORT_RE.match(line)
            if m:
                pkg = m.group(1)
            else:
                m = _IMPORT_RE.match(line)
                if m:
                    pkg = m.group(1)
            if pkg is None:
                continue
            # 截取顶层段
            if pkg.startswith("shared"):
                imported_top.add("shared")
            elif pkg.startswith("strategies"):
                parts = pkg.split(".")
                if len(parts) >= 2:
                    imported_top.add(f"strategies/{parts[1]}")
    return imported_top


def test_shadow_dependencies_covered_by_dockerfile_copy():
    """shadow 入口依赖的每个项目内顶层包，都必须被 Dockerfile COPY 覆盖。"""
    copied = _extract_copied_top_dirs(DOCKERFILE)
    imported = _extract_project_import_top_packages(SHADOW_DIR)

    missing = imported - copied
    # "strategies/btc_eth" 一定在 copied 里（Dockerfile 第 20 行），
    # 但为保险也一起校验
    assert imported, f"shadow 目录下未扫描到任何项目内 import，请检查扫描逻辑"

    assert not missing, (
        f"btc-eth Dockerfile 缺少以下 COPY，"
        f"否则 mtcps-xau-shadow 容器会 ModuleNotFoundError:\n"
        f"  缺失 COPY 目标 : {sorted(missing)}\n"
        f"  当前已 COPY     : {sorted(copied)}\n"
        f"  shadow 需要     : {sorted(imported)}"
    )
