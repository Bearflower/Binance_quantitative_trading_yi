"""K 线表名校验与解析（R01：防止外部输入改变 SQL 结构）

本模块是「以 symbol/interval 构造 K 线表名」的唯一入口（单词点）。
所有对外查询、注册、采集入口必须调用 build_kline_table_name，禁止在别处
内联拼接 f"kline_{symbol.lower()}_{interval}"，避免 SQL 结构注入。

校验采用 fail-closed 策略：任一层校验不通过或发生异常，均拒绝并抛出
TableNameValidationError，由调用方（路由层）转换为 4xx 响应，绝不继续拼 SQL。
"""

import re
from functools import lru_cache
from typing import Awaitable, Callable, Optional, Set, Tuple

from shared.utils.logger import get_logger

logger = get_logger(__name__)


class TableNameValidationError(ValueError):
    """表名/参数校验失败（路由层据此返回 4xx，禁止继续拼 SQL）"""


def _resolve_settings(settings):
    """获取配置对象：显式传入优先，否则延迟导入全局 settings（避免循环依赖）"""
    if settings is not None:
        return settings
    from shared.core.config import settings as global_settings
    return global_settings


def _split_csv(raw) -> Set[str]:
    """将配置项（逗号分隔字符串或列表）统一拆分为去空白的字符串集合"""
    if raw is None:
        return set()
    items = list(raw) if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    return {str(item).strip() for item in items if str(item).strip()}


@lru_cache(maxsize=64)
def _compile(pattern: str):
    """编译正则（按 pattern 缓存，避免每请求重复编译；纯函数无共享可变状态）"""
    return re.compile(pattern)


def _pattern(cfg, attr: str) -> Optional[str]:
    """从配置读取正则字符串；缺失或非字符串时返回 None（由调用方 fail-closed）"""
    value = getattr(cfg, attr, None)
    return value if isinstance(value, str) and value.strip() else None


def _flag_enabled(value, default: bool = True) -> bool:
    """稳健解析布尔开关：仅接受真实布尔/可识别的字符串，其余（含 MagicMock）回退默认值"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def read_non_negative_int(value, default: int = 0) -> int:
    """稳健解析非负整数配置：非 int（如 MagicMock）或负值一律回退默认值"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value >= 0:
        return value
    return default


def _registry_entries(registry) -> list:
    """实时读取注册表当前激活标的（非启动快照）。

    注册表不可用（未初始化/数据库异常）时降级为空列表，交由
    「固定标的 ∪ settings.SYMBOLS」静态白名单兜底；异常外泄即失败，
    但仍保持 fail-closed（静态白名单之外一律拒绝）。
    """
    if registry is None:
        return []
    try:
        configs = registry.get_active_symbols()
    except Exception as e:  # noqa: BLE001 - 降级为静态白名单，仍保持 fail-closed
        logger.warning("读取标的注册表失败，降级为静态白名单校验：%s", e)
        return []
    entries = []
    for config in configs or []:
        symbol = str(getattr(config, "symbol", "")).strip().upper()
        intervals = {str(i).strip() for i in (getattr(config, "intervals", None) or [])}
        intervals.discard("")
        if symbol:
            entries.append((symbol, intervals))
    return entries


def _collect_whitelist(cfg, registry) -> Tuple[Set[str], Set[str]]:
    """汇总内存层白名单：(允许的 symbol 集合, 允许的 interval 集合)"""
    registry_symbols: Set[str] = set()
    registry_intervals: Set[str] = set()
    for symbol, intervals in _registry_entries(registry):
        registry_symbols.add(symbol)
        registry_intervals |= intervals
    allowed_symbols = {s.upper() for s in _split_csv(getattr(cfg, "FIXED_SYMBOLS", None))}
    allowed_symbols |= {s.upper() for s in _split_csv(getattr(cfg, "SYMBOLS", None))}
    allowed_symbols |= registry_symbols
    allowed_intervals = _split_csv(getattr(cfg, "COLLECT_INTERVALS", None))
    allowed_intervals |= registry_intervals
    return allowed_symbols, allowed_intervals


def validate_symbol_interval_format(symbol: str, interval: str, *, settings=None) -> Tuple[str, str]:
    """仅做格式层校验：返回 (大写 symbol, 去空白 interval)，不查询白名单。

    用于「新标的注册」这类尚未进入白名单、但必须先阻止注入的场景，
    以及 build_kline_table_name 的内部前置校验。
    """
    cfg = _resolve_settings(settings)
    if not isinstance(symbol, str) or not isinstance(interval, str):
        raise TableNameValidationError("symbol/interval 必须为字符串")
    norm_symbol = symbol.strip().upper()
    symbol_pattern = _pattern(cfg, "SYMBOL_FORMAT_PATTERN")
    if symbol_pattern is None or not _compile(symbol_pattern).fullmatch(norm_symbol):
        raise TableNameValidationError(f"symbol 格式非法：{symbol!r}")
    norm_interval = interval.strip()
    if not norm_interval:
        raise TableNameValidationError("interval 不能为空")
    return norm_symbol, norm_interval


def is_valid_table_name(table_name: str, *, settings=None) -> bool:
    """判断表名是否整体匹配配置的严格正则（R01-AC6 单测入口，fail-closed）"""
    if not isinstance(table_name, str):
        return False
    cfg = _resolve_settings(settings)
    pattern = _pattern(cfg, "TABLE_NAME_PATTERN")
    if pattern is None:
        logger.error("未配置 TABLE_NAME_PATTERN，表名校验 fail-closed 拒绝")
        return False
    return bool(_compile(pattern).fullmatch(table_name))


def _assemble_table_name(cfg, norm_symbol: str, norm_interval: str) -> str:
    """由已规范化的 symbol/interval 组装表名，并整体匹配 TABLE_NAME_PATTERN（fail-closed）"""
    table_name = f"kline_{norm_symbol.lower()}_{norm_interval}"
    if not is_valid_table_name(table_name, settings=cfg):
        raise TableNameValidationError(f"表名不匹配 TABLE_NAME_PATTERN：{table_name}")
    return table_name


def _validate_format_and_assemble(cfg, symbol: str, interval: str) -> Tuple[str, str, str]:
    """格式层 + 表名层校验并组装，返回 (大写 symbol, 去空白 interval, 表名)。

    不查询白名单，供 build_kline_table_name 与读路径入口共用（单一实现，杜绝重复校验）。
    """
    norm_symbol, norm_interval = validate_symbol_interval_format(symbol, interval, settings=cfg)
    return norm_symbol, norm_interval, _assemble_table_name(cfg, norm_symbol, norm_interval)


def build_table_name_by_format(symbol: str, interval: str, *, settings=None) -> str:
    """仅做「格式层 + 表名层」校验（无白名单）并返回表名。

    供读路径存在性放行前的**前置过滤**：非法 symbol/interval 在触达任何 DB 之前即被
    fail-closed 拒绝（不查询白名单、不执行存在性查询）。
    """
    cfg = _resolve_settings(settings)
    return _validate_format_and_assemble(cfg, symbol, interval)[2]


def build_kline_table_name(symbol: str, interval: str, *, registry=None, settings=None) -> str:
    """【写路径/保留入口】输入 symbol/interval，返回合法表名；任一校验不通过抛 TableNameValidationError。

    校验三层（fail-closed，任一层异常即视为失败）：
      1) 格式层：symbol 匹配 SYMBOL_FORMAT_PATTERN、interval 非空
      2) 表名层：生成表名整体匹配 settings.TABLE_NAME_PATTERN
      3) 白名单层：symbol ∈ (FIXED_SYMBOLS ∪ registry 激活标的 ∪ settings.SYMBOLS)；
                   interval ∈ (settings.COLLECT_INTERVALS ∪ registry 激活标的 intervals)
    """
    cfg = _resolve_settings(settings)
    norm_symbol, norm_interval, table_name = _validate_format_and_assemble(cfg, symbol, interval)
    allowed_symbols, allowed_intervals = _collect_whitelist(cfg, registry)
    if norm_symbol not in allowed_symbols:
        raise TableNameValidationError(f"symbol 不在白名单：{norm_symbol}")
    if norm_interval not in allowed_intervals:
        raise TableNameValidationError(f"interval 不在白名单：{norm_interval}")
    return table_name


async def build_readable_table_name(
    symbol: str,
    interval: str,
    *,
    table_exists: Optional[Callable[[str], Awaitable[bool]]] = None,
    registry=None,
    settings=None,
) -> str:
    """【读路径唯一入口，P0-2】白名单 OR「表已存在且格式合法」。

    校验顺序（任一不通过即抛 TableNameValidationError，绝不继续拼 SQL）：
      1) 格式层 + 表名层：复用 _validate_format_and_assemble（不新增第二套校验）
      2) 白名单命中（FIXED ∪ SYMBOLS ∪ registry active）→ 直接放行
      3) 未命中且开关开启且注入了 table_exists 回调：
           表存在 → 记 INFO（source=existing_table）并放行
           不存在 → 拒绝
           回调抛异常 → 拒绝（fail-closed）
      4) 其余（开关关闭 / 未注入回调）→ 拒绝

    「表是否存在」的事实由具备 DB 连接的路由层以参数化回调注入，本模块不直连 DB。
    """
    cfg = _resolve_settings(settings)
    norm_symbol, norm_interval, table_name = _validate_format_and_assemble(cfg, symbol, interval)
    allowed_symbols, allowed_intervals = _collect_whitelist(cfg, registry)
    if norm_symbol in allowed_symbols and norm_interval in allowed_intervals:
        return table_name
    allow_existing = _flag_enabled(getattr(cfg, "ALLOW_EXISTING_TABLE_SYMBOLS", None), True)
    if not allow_existing or table_exists is None:
        raise TableNameValidationError(f"symbol/interval 不在白名单：{norm_symbol}/{norm_interval}")
    try:
        exists = await table_exists(table_name)
    except Exception as e:  # noqa: BLE001 - 存在性查询失败即 fail-closed
        raise TableNameValidationError(f"表存在性检查失败（fail-closed）：{table_name} - {e}") from e
    if not exists:
        raise TableNameValidationError(
            f"symbol/interval 不在白名单且表不存在：{norm_symbol}/{norm_interval}"
        )
    logger.info("读路径按「表已存在」放行：%s（source=existing_table）", table_name)
    return table_name
