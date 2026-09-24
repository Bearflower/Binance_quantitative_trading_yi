"""
组合级单边行情熔断器（共享核心实现）

判定逻辑的唯一实现入口，三策略（MTPCS 原版/激进版、HRS）与指数计算器共同使用。
判定依据严格对齐需求文档 v1.1 第 4.6 节真值表（S1/S2/S3 + L1/L2/L3 + 冷却规则）：

- 冷却检查（不分方向）→ 组合级（池等权指数）→ 单币级（目标币自身 1h 涨幅）
- 组合级：short 方向 pool_index > +trigger_short 拦（S1）；long 方向 pool_index < -trigger_long 拦（L1）
- 单币级：short 方向 symbol_ret_1h > +single_coin_short 拦并进冷却（S2）；
  long 方向 < -single_coin_long 拦并进冷却（L2）
- 等于阈值一律放行（严格 `>` / `<`）
- 指数缺失（None）→ fail-open（放行），由调用方记录 warning

本模块依赖 shared.config_loader.load_shared_config 读取
shared/circuit_breaker_config.yaml（唯一事实源），严禁在代码中硬编码阈值。
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import structlog

from shared.config_loader import load_shared_config

logger = structlog.get_logger()

# 北京时区（与项目数据口径一致，指数整点按北京时间对齐）
BEIJING_TZ = timezone(timedelta(hours=8))

# 熔断指数快照表（与 database/postgres/init-scripts/07-market-circuit-breaker.sql 一致）
_INDEX_TABLE = "public.market_circuit_breaker_index"

# 池标识（分池平权口径下的两个指数池）
POOL_MTPCS = "mtpcs"
POOL_HRS = "hrs"


def load_circuit_breaker_config() -> Dict[str, Any]:
    """加载组合级熔断共享配置（market_circuit_breaker 节）

    读取 shared/circuit_breaker_config.yaml 的 market_circuit_breaker 节，
    作为三策略 + 指数计算器的唯一事实源。配置缺失时返回空字典（降级）。

    Returns:
        market_circuit_breaker 配置字典；缺失时返回空字典。
    """
    raw = load_shared_config("circuit_breaker_config.yaml")
    section = raw.get("market_circuit_breaker", {})
    if not section:
        logger.warning("组合级熔断配置节缺失，返回空配置")
    return section


def floor_index_hour(dt: Optional[datetime] = None) -> datetime:
    """计算指数对应的北京整点（floor to hour）

    指数计算器与各策略读取均以"北京整点"为 index_hour 口径：
    计算器在每小时 03 分计算"刚于整点收盘的那根 1h K 线"对应的等权涨幅，
    策略在开仓前读取该整点的指数。统一经本函数生成，避免时区口径漂移。

    Args:
        dt: 输入时刻（naive 视为北京时间，aware 先转北京时区）；None 表示当前时刻

    Returns:
        北京时区下对齐到整点的 datetime
    """
    now = dt or datetime.now(BEIJING_TZ)
    if now.tzinfo is None:
        # naive 视为北京时间（服务器容器常为 UTC，需显式指定北京时区）
        now = now.replace(tzinfo=BEIJING_TZ)
    else:
        now = now.astimezone(BEIJING_TZ)
    return now.replace(minute=0, second=0, microsecond=0)


def compute_1h_return(klines_1h) -> Optional[float]:
    """计算最近两根 1h K 线的涨跌幅

    Args:
        klines_1h: 1h K 线列表（每项含 'close' 字段），按时间正序

    Returns:
        涨跌幅（小数，如 0.02 = 2%）；K 线不足 2 根或前根收盘价非正时返回 None
    """
    if not klines_1h or len(klines_1h) < 2:
        return None
    prev_close = float(klines_1h[-2]["close"])
    cur_close = float(klines_1h[-1]["close"])
    if prev_close <= 0:
        return None
    return (cur_close - prev_close) / prev_close


def compute_cumulative_return(klines_1h, hours: int = 12) -> Optional[float]:
    """计算最近 N 根 1h K 线的累计涨跌幅（二期 12h 累计）

    累计窗口 [t - hours, t]：涨幅 = (close_t - close_{t-hours}) / close_{t-hours}。
    需要 hours+1 根 K 线才能跨满该窗口。

    Args:
        klines_1h: 1h K 线列表（每项含 'close' 字段），按时间正序
        hours: 累计窗口小时数（默认 12）

    Returns:
        累计涨跌幅（小数，带符号）；K 线不足 hours+1 根或窗口起收盘价非正时返回 None
    """
    if not klines_1h or len(klines_1h) < hours + 1:
        return None
    start_close = float(klines_1h[-(hours + 1)]["close"])
    cur_close = float(klines_1h[-1]["close"])
    if start_close <= 0:
        return None
    return (cur_close - start_close) / start_close


def parse_cron(expr: str) -> Tuple[Any, Any]:
    """解析 5 字段 cron 表达式，提取小时与分钟

    项目 cron 约定为标准顺序：第一段=分钟、第二段=小时（如 "5 * * * *" = 每小时 05 分）。

    Args:
        expr: cron 表达式，如 "3 * * * *"

    Returns:
        (hour, minute)：字段为 "*" 时保留字符串 "*"，否则转为 int
    """
    parts = expr.strip().split()
    minute_raw = parts[0] if parts else "*"
    hour_raw = parts[1] if len(parts) > 1 else "*"
    hour = "*" if hour_raw == "*" else int(hour_raw)
    minute = "*" if minute_raw == "*" else int(minute_raw)
    return hour, minute


class CircuitBreaker:
    """组合级熔断器（判定唯一实现）

    持有内存冷却表（按 symbol）与指数缓存（按 index_hour），
    提供 load_index / guard / combined_blocked / is_in_cooldown / start_cooldown。
    guard 为纯判定（不访问 DB），便于单测；指数读取由调用方经 load_index 负责。

    Args:
        config: market_circuit_breaker 配置节（含全部阈值，禁止硬编码）
        db: 数据库管理器（DatabaseManager，需有 fetch_one；None 时 load_index 返回 None）
        pool: 池标识，'mtpcs' 或 'hrs'（分池平权口径）
    """

    def __init__(self, config: Dict[str, Any], db, pool: str) -> None:
        if pool not in (POOL_MTPCS, POOL_HRS):
            raise ValueError(f"非法池标识: {pool}，仅支持 {POOL_MTPCS}/{POOL_HRS}")
        self.pool = pool
        self._db = db
        # 阈值全部从配置读取（禁止硬编码）
        # 注：release_short/release_long 为滞回解除设计预留（需求 4.2），
        # 当前实现按需求 6.3 简化版实时判定（只看最新一根指数），故不读取使用。
        self._trigger_short = float(config["trigger_short"])
        self._trigger_long = float(config["trigger_long"])
        self._single_coin_short = float(config["single_coin_short"])
        self._single_coin_long = float(config["single_coin_long"])
        self._cooldown_hours = float(config["single_coin_cooldown_hours"])
        # 二期：12h 累计维度（组合级，双向对称）；include_12h 关闭时回到一期纯 1h 行为
        self._include_12h = bool(config.get("include_12h", False))
        self._trigger_short_12h = float(config.get("trigger_short_12h", 0.0))
        self._trigger_long_12h = float(config.get("trigger_long_12h", 0.0))
        # 单币冷却截止时间 {symbol: datetime}，进程内存、跨周期保持
        self._cooldown_until: Dict[str, datetime] = {}
        # 指数缓存 {index_hour.isoformat(): Optional[float]}，同一整点只查一次库
        self._index_cache: Dict[str, Optional[float]] = {}

    async def _query_index(self, index_hour: datetime, column: str, cache_key: str) -> Optional[float]:
        """按 (pool, index_hour) 查熔断指数快照表的某列并缓存（公共查询，load_index/12h 共用）

        同一 index_hour 结果缓存；指数/列缺失返回 None（调用方按 fail-open 放行处理）。

        Args:
            index_hour: 北京整点时刻（由 floor_index_hour 生成）
            column: 目标列名（'equal_weight' 或 'equal_weight_12h'）
            cache_key: 缓存键（1h 用 index_hour.isoformat()，12h 用 isoformat()+':12h' 区分）

        Returns:
            该列指数值（小数，带符号）；缺失或 DB 不可用时返回 None
        """
        if cache_key in self._index_cache:
            return self._index_cache[cache_key]
        if self._db is None:
            logger.warning("熔断器未绑定数据库，指数按缺失处理（fail-open）")
            return None
        try:
            row = await self._db.fetch_one(
                f"SELECT {column} FROM {_INDEX_TABLE} "
                "WHERE pool = $1 AND index_hour = $2",
                self.pool,
                index_hour,
            )
            value = float(row[column]) if row and row.get(column) is not None else None
            self._index_cache[cache_key] = value
            if value is None:
                logger.warning(
                    "熔断指数缺失，按放行处理（fail-open）",
                    pool=self.pool,
                    index_hour=index_hour.isoformat(),
                    column=column,
                )
            return value
        except Exception as e:
            logger.warning(
                "读取熔断指数失败，按放行处理（fail-open）",
                pool=self.pool,
                index_hour=index_hour.isoformat(),
                column=column,
                error=str(e),
            )
            return None

    async def load_index(self, index_hour: datetime) -> Optional[float]:
        """读取指定整点的池等权 1h 指数（短尺度）

        Args:
            index_hour: 北京整点时刻（由 floor_index_hour 生成）

        Returns:
            池等权 1h 涨幅（小数，带符号）；缺失或 DB 不可用时返回 None
        """
        return await self._query_index(index_hour, "equal_weight", index_hour.isoformat())

    async def load_index_12h(self, index_hour: datetime) -> Optional[float]:
        """读取指定整点的池等权 12h 累计指数（二期，慢变尺度）

        与 1h 同表同整点、不同列（equal_weight_12h），缺失返回 None（fail-open）。

        Args:
            index_hour: 北京整点时刻（由 floor_index_hour 生成）

        Returns:
            池等权 12h 累计涨幅（小数，带符号）；缺失或 DB 不可用时返回 None
        """
        return await self._query_index(index_hour, "equal_weight_12h", index_hour.isoformat() + ":12h")

    @staticmethod
    def _is_blocked_1h(direction: str, v: Optional[float], trigger: float) -> bool:
        """组合级 1h 瞬时判定：short 涨幅>trigger / long 跌幅>trigger（None 放行）"""
        if v is None:
            return False
        return v > trigger if direction == "short" else v < -trigger

    def _is_blocked_12h(self, direction: str, v12: Optional[float]) -> bool:
        """组合级 12h 累计判定：short 累计涨幅>trigger_short_12h / long 累计跌幅>trigger_long_12h

        仅当 include_12h 开启且 12h 指数存在才判定；None 放行。（guard 与 combined_blocked 共用）
        """
        if not self._include_12h or v12 is None:
            return False
        if direction == "short":
            return v12 > self._trigger_short_12h
        return v12 < -self._trigger_long_12h

    def guard(
        self,
        direction: str,
        symbol: str,
        symbol_ret_1h: Optional[float],
        pool_index: Optional[float],
        pool_index_12h: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """开仓前熔断判定（需求 4.6 + 二期 4.7 唯一实现）

        判定顺序：冷却检查（不分方向）→ 组合级（1h 瞬时与 12h 累计任一命中即拦）→
        单币级（仍只看 1h）。指数/涨幅缺失（None）不参与对应判定（fail-open 放行）。

        Args:
            direction: 开仓方向，'short'/'long'（大小写不敏感，兼容 LONG/SHORT）
            symbol: 目标币种
            symbol_ret_1h: 目标币自身 1h 涨跌幅（可 None）
            pool_index: 所属池等权 1h 指数（可 None）
            pool_index_12h: 所属池等权 12h 累计指数（可 None；include_12h=False 时忽略）

        Returns:
            (allow, level)：allow=True 放行（level="allow"）；
            allow=False 拦截，level 取值：cooldown / pool_short / pool_short_12h /
            coin_short / pool_long / pool_long_12h / coin_long
        """
        if self.is_in_cooldown(symbol):
            return False, "cooldown"

        norm = direction.lower()
        if norm == "short":
            # S1 组合级 瞬时：池等权 1h 涨幅 > trigger_short → 拦该策略本轮全部新空
            if self._is_blocked_1h(norm, pool_index, self._trigger_short):
                return False, "pool_short"
            # （二期）组合级累计：12h 累计涨幅 > trigger_short_12h → 拦全部新空（慢牛）
            if self._is_blocked_12h(norm, pool_index_12h):
                return False, "pool_short_12h"
            # S2 单币级：目标币自身 1h 涨幅 > single_coin_short → 拦该币新空并进冷却
            if symbol_ret_1h is not None and symbol_ret_1h > self._single_coin_short:
                self.start_cooldown(symbol)
                return False, "coin_short"
            # S3 放行
            return True, "allow"

        # 多头方向
        # L1 组合级 瞬时：池等权 1h 跌幅 > trigger_long（指数 < -trigger_long）→ 拦全部新多
        if self._is_blocked_1h(norm, pool_index, self._trigger_long):
            return False, "pool_long"
        # （二期）组合级累计：12h 累计跌幅 > trigger_long_12h → 拦全部新多（阴跌）
        if self._is_blocked_12h(norm, pool_index_12h):
            return False, "pool_long_12h"
        # L2 单币级：目标币自身 1h 跌幅 > single_coin_long → 拦该币新多并进冷却
        if symbol_ret_1h is not None and symbol_ret_1h < -self._single_coin_long:
            self.start_cooldown(symbol)
            return False, "coin_long"
        # L3 放行
        return True, "allow"

    def combined_blocked(
        self,
        direction: str,
        pool_index: Optional[float],
        pool_index_12h: Optional[float] = None,
    ) -> bool:
        """仅组合级判定（S1/L1 + 二期 12h），供 HRS 信号级预过滤使用

        Args:
            direction: 'short'/'long'
            pool_index: 池等权 1h 指数（None 视为未触发）
            pool_index_12h: 池等权 12h 累计指数（None 视为未触发；include_12h=False 时忽略）

        Returns:
            True 表示该方向应整体拦截；False 放行
        """
        norm = direction.lower()
        if self._is_blocked_1h(norm, pool_index, self._trigger_short if norm == "short" else self._trigger_long):
            return True
        if self._is_blocked_12h(norm, pool_index_12h):
            return True
        return False

    def is_in_cooldown(self, symbol: str) -> bool:
        """判断某币是否处于冷却期（不分方向）

        过期条目自动恢复并顺带清理，无需额外解除动作。

        Args:
            symbol: 目标币种

        Returns:
            True 表示冷却期内应拦截该币任何方向新开仓
        """
        until = self._cooldown_until.get(symbol)
        if until is None:
            return False
        if datetime.now() >= until:
            # 冷却到期，自动恢复并清理条目
            del self._cooldown_until[symbol]
            return False
        return True

    def start_cooldown(self, symbol: str) -> None:
        """为某币启动/刷新冷却（到期 = now + single_coin_cooldown_hours）

        Args:
            symbol: 目标币种
        """
        self._cooldown_until[symbol] = datetime.now() + timedelta(
            hours=self._cooldown_hours
        )
        logger.info(
            "单币熔断触发，进入冷却",
            symbol=symbol,
            cooldown_hours=self._cooldown_hours,
            until=self._cooldown_until[symbol].isoformat(),
        )
