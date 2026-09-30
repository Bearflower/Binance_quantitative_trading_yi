"""R01/R08 修复批次测试的公共夹具。

本目录测试需要同时覆盖两套彼此隔离的 `shared` 依赖：
- R01（kline_service）：`shared.utils.logger` / `shared.core.config` / `shared.core.database`
- R08（ai_tuner）：`shared.condition_orders` / `shared.binance_api` / `shared.notification`

冲突来源（必须在同一 pytest 进程内共存）：
- 其他测试目录（如 `tests/test_shared`）需要**真实**的 `shared.*` 模块（仓库根 `shared/`
  是扁平布局，`shared.utils` / `shared.binance_api` / `shared.notification` 都是真实模块）；
- 本目录需要这些同名的 `shared` **子模块桩**；
- 顶层包名 `core` / `api` / `models` 在本目录指向 `services/kline_service/` 下的包，
  而 `tests/test_shared/test_unrealized_pnl.py` 又需要顶层 `core` 指向
  `dashboard/backend/core`（该测试自带 sys.path 注入），同名撞车。

因此这里**不**在导入期永久改写全局，而是做「目录级、可恢复」的隔离：

1. `.trae` 全局禁止项 → 绝不创建假顶层 `shared`、绝不设置 `shared.__path__`；只按
   `tests/test_kline_service/conftest.py` 的做法注册 `shared` **子模块**条目；
2. 用 `pytest_collectstart` 依据 `collector.path` 判定「进入/离开本目录」：进入时装桩
   并前置 `sys.path`，离开时把受管前缀（shared/core/api/models/services/ai_tuner）
   的 `sys.modules` 条目与 `sys.path` **整体还原**为进入前的快照；
3. 被测模块（`core/table_name_guard.py`）会在函数体内**惰性**
   `from shared.core.config import settings`，因此另用本目录专属的 autouse fixture
   在**用例执行期**装桩，保证运行期惰性导入可用，用完即还原。

这样桩与路径的存活范围被严格限制在本目录内，其他目录依旧导入真实模块，互不击穿。
kline_service 的真实 `Settings` 对象（供 R01-AC6 校验真实默认正则）通过
`importlib.util.spec_from_file_location` 隔离加载，不经过 `shared` 包解析。
"""

import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KLINE_SERVICE_PATH = PROJECT_ROOT / "services" / "kline_service"
REAL_CONFIG_PATH = KLINE_SERVICE_PATH / "shared" / "core" / "config.py"
HERE = Path(__file__).resolve().parent

# 本目录导入过程中可能被改写、需要整体快照/还原的顶层前缀。
# 说明：刻意 **不** 纳入 `ai_tuner` —— R08 用字符串 patch
# `ai_tuner.cleanup.orphan_cleanup.get_open_orders`，其目标模块对象必须保持稳定；
# 若还原时把它移出 sys.modules，`mock.patch` 会重新导入该模块，产生新的模块对象，
# 与被测类所在模块不一致，导致 patch 打空。
_MANAGED_PREFIXES = ("shared", "core", "api", "models")


# ============================================================
# 基础工具
# ============================================================

def _module(name: str) -> types.ModuleType:
    """注册并返回一个空的桩模块。"""
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _load_real_settings():
    """加载 kline_service 的真实 Settings 实例（不经过 shared 包解析）。"""
    spec = importlib.util.spec_from_file_location("_r01_r08_real_config", REAL_CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.settings


_real_settings = _load_real_settings()


class _StubBinanceAPIError(Exception):
    """BinanceAPIError 桩，携带 code/message 便于被测代码判定错误分支。"""

    def __init__(self, code: int = 0, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(message)


# ============================================================
# 桩安装
# ============================================================

def _install_stub_modules():
    """只注册 R01/R08 需要的 `shared` **子模块**桩。

    刻意 **不** 创建顶层假 `shared`、**不** 设置 `shared.__path__`，避免对其他
    需要真实 `shared.*` 的测试目录造成不可恢复的破坏。
    """
    # ---------- R01：kline_service 侧 shared 桩 ----------
    _utils = _module("shared.utils")
    _logger_mod = _module("shared.utils.logger")
    _logger_mod.get_logger = lambda name, *a, **k: logging.getLogger(name)
    _utils.logger = _logger_mod

    _core = _module("shared.core")
    _core_config = _module("shared.core.config")
    _core_config.settings = _real_settings
    _core_config.Settings = type(_real_settings)
    _core.config = _core_config
    _core_db = _module("shared.core.database")
    _core_db.Database = object
    _core_db.DatabaseManager = object
    _core_db.db_manager = None
    _core.database = _core_db

    # ---------- R08：ai_tuner 侧 shared 桩 ----------
    _cond = _module("shared.condition_orders")
    _cond.get_open_orders = None
    _cond.mark_order_canceled = None
    _cond.mark_order_executed = None
    _cond.ensure_table = None

    _binance = _module("shared.binance_api")
    _binance.BinanceClient = object
    _binance.BinanceAPIError = _StubBinanceAPIError

    _notif = _module("shared.notification")
    _notif.NotificationClient = object


# ============================================================
# 可恢复的全局环境（sys.modules 受管前缀 + sys.path）
# ============================================================

_saved_modules = None   # 进入前的受管前缀 sys.modules 快照
_saved_path = None      # 进入前的 sys.path 快照
_active = False         # 当前是否处于「本目录隔离环境」
_dir_nodeid = None      # 本目录 Dir collector 的 nodeid（其 report 代表本目录收集结束）


def _is_managed(name: str) -> bool:
    """判断模块名是否属于受管前缀。"""
    return any(name == p or name.startswith(p + ".") for p in _MANAGED_PREFIXES)


def _snapshot_managed_modules():
    """快照受管前缀的 sys.modules 条目。"""
    return {k: v for k, v in sys.modules.items() if _is_managed(k)}


def _restore_managed_modules(saved):
    """把受管前缀的 sys.modules 条目整体还原为快照内容。"""
    for name in [k for k in sys.modules if _is_managed(k)]:
        del sys.modules[name]
    sys.modules.update(saved)


def _activate():
    """进入本目录隔离环境：装桩 + 前置 sys.path（幂等）。"""
    global _saved_modules, _saved_path, _active
    if _active:
        return
    _saved_modules = _snapshot_managed_modules()
    _saved_path = list(sys.path)
    for p in (str(PROJECT_ROOT), str(KLINE_SERVICE_PATH)):
        if p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(KLINE_SERVICE_PATH))
    _install_stub_modules()
    _active = True


def _deactivate():
    """离开本目录隔离环境：还原 sys.modules 与 sys.path（幂等）。"""
    global _active
    if not _active:
        return
    _restore_managed_modules(_saved_modules)
    sys.path[:] = _saved_path
    _active = False


def _under_here(path) -> bool:
    """判断 collector 路径是否位于本目录之下。"""
    try:
        real = os.path.realpath(str(path))
    except (TypeError, ValueError):
        return False
    here = str(HERE)
    return real == here or real.startswith(here + os.sep)


# ============================================================
# pytest 钩子：目录级通/断隔离环境
# ============================================================

def pytest_collectstart(collector):
    """收集开始：若 collector 属于本目录，则装桩（并记录本目录 Dir 的 nodeid）。

    注意：conftest 定义的收集钩子是**按 collector 的 conftest 链分发**的——只会在
    本目录子树内的 collector 上触发，其他目录（如 tests/test_shared）的 collector
    根本不会调用本钩子，因此天然不会误伤它们。
    """
    global _dir_nodeid
    path = getattr(collector, "path", None)
    if path is None or not _under_here(path):
        return
    _activate()
    nodeid = getattr(collector, "nodeid", None)
    if nodeid and os.path.realpath(str(path)) == str(HERE):
        _dir_nodeid = nodeid  # 本目录自身的 Dir collector（其 report 代表本目录收集结束）


def pytest_collectreport(report):
    """收集报告：本目录 Dir 的 report 在其子节点之后触发，据此还原环境。"""
    nodeid = getattr(report, "nodeid", None)
    if _active and _dir_nodeid is not None and nodeid == _dir_nodeid:
        _deactivate()


def pytest_collection_finish(session):
    """全部收集结束：兜底还原（覆盖单文件参数等无 Dir report 的场景）。"""
    _deactivate()


def pytest_unconfigure(config):
    """会话结束兜底还原，避免任何残留。"""
    _deactivate()


# ============================================================
# 夹具
# ============================================================

@pytest.fixture(autouse=True)
def _r01_r08_isolated_env():
    """在本目录**每个用例执行期**装桩，用完立即还原。

    被测模块会在函数体内**惰性** `from shared.core.config import settings`，桩必须
    活跃到用例执行结束；而其他测试目录在收集期需要真实 `shared.*`，故以「按用例
    装/卸」把桩的存活范围限制在本目录用例内，绝不外泄。
    """
    _activate()
    try:
        yield
    finally:
        _deactivate()


@pytest.fixture(scope="session")
def real_settings():
    """真实 kline_service 默认配置（用于 R01-AC6 校验真实默认正则）。"""
    return _real_settings