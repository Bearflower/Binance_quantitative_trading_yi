"""
资金分配管理器

提供策略可用的「月度分配额度（保证金口径）」上限，用于约束实盘开仓。

限额优先级链（方案 D：运行时读 DB，见需求文档 R2）：
    1. DB public.capital_allocation —— 当月 status='active' 记录的 entries 中
       strategy_id 对应的 allocated_amount（单一真相来源，部署不擦除）
    2. 本策略 config.yaml 的 capital_limits.monthly_limit（兜底）
    3. 本策略 config.yaml 的 trading.total_position_margin_limit（静态兜底）
    4. 三级均不可用 —— fail-open：回退静态兜底，仍可开仓并记录告警（决策 D2）

约定：
- DB 无当月记录时**不回退上月**，直接走 ②→③ 并告警（决策 D1）
- 对外唯一限额入口为 `get_effective_margin_limit()`，口径为「占用保证金」
- 既有同步方法（get_allocated_capital / get_total_margin_limit /
  can_open_position / is_allocated）保留以兼容未改造调用方，但会打废弃告警

配置来源（各策略 config.yaml）：
    capital_limits:
      monthly_limit: 360.0       # 当月分配额度（USDT，保证金口径）
      allocated_ratio: 0.36
      allocation_month: "2026-07"
      db_cache_ttl_seconds: 300     # DB 分配额缓存有效期（秒）
      db_query_timeout_seconds: 3.0 # DB 查询超时（秒）
    trading:
      total_position_margin_limit: 150   # 静态兜底（USDT）
      min_position_margin: 25.0          # 缩仓后最小可开保证金（USDT）
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import structlog
import yaml

logger = structlog.get_logger()

# 中国标准时间（UTC+8），用于确定"当月"
_CST = timezone(timedelta(hours=8))

# DB 分配额缓存 / 超时的兜底默认值
# 说明：这两项优先从 config.capital_limits 读取；但该节点会被月度分配流程整体覆盖，
# 故保留代码级默认值，避免配置被覆盖后失去缓存与超时保护（非业务阈值）。
_DEFAULT_DB_CACHE_TTL_SECONDS = 300
_DEFAULT_DB_QUERY_TIMEOUT_SECONDS = 3.0

# 限额来源标签（用于日志审计）
SOURCE_DB = "db"                 # DB 当月记录
SOURCE_DB_CACHE = "db_cache"     # DB 结果缓存命中
SOURCE_DB_STALE = "db_stale"     # DB 异常，沿用最近一次成功值
SOURCE_CONFIG = "config"         # config.capital_limits.monthly_limit
SOURCE_STATIC = "static"         # config.trading.total_position_margin_limit
SOURCE_NONE = "none"             # 全部不可用（fail-open）


class CapitalManager:
    """
    资金分配管理器

    从 DB public.capital_allocation（主）与策略 config.yaml（兜底）读取月度分配额度，
    对外提供唯一的保证金口径限额入口。

    说明：config 类读取每次重新读文件，确保月度分配写入后立即生效；
    DB 类读取按 TTL 缓存，避免每笔开仓都查库。
    """

    def __init__(
        self,
        config_path: str,
        db: Optional[Any] = None,
        strategy_id: Optional[str] = None,
    ):
        """
        初始化资金分配管理器

        Args:
            config_path: 策略配置文件路径（相对或绝对路径）
            db: 数据库管理器（提供 fetch_one），传入后启用 DB 主来源
            strategy_id: 策略标识（用于在 capital_allocation.entries 中匹配本策略）
        """
        self.config_path = config_path
        self.db = db
        self.strategy_id = strategy_id

        # DB 分配额缓存：值 / 时间戳
        self._db_cache_value: Optional[float] = None
        self._db_cache_ts: float = 0.0

        # 最近一次成功读取到的分配额（DB 异常时沿用，避免限额失效）
        self._db_last_success_value: Optional[float] = None

        # 已告警过的废弃方法名，避免同步方法在循环中刷屏
        self._deprecation_warned: set = set()

    def bind_database(self, db: Optional[Any], strategy_id: Optional[str] = None) -> None:
        """
        延迟绑定数据库与策略标识

        用于依赖在 __init__ 之后才注入的策略（如 hrs 的 self.db 由 set_database 异步注入）。
        绑定后会清空 DB 分配额缓存，避免沿用旧来源的值。

        Args:
            db: 数据库管理器（提供 fetch_one）
            strategy_id: 策略标识；为 None 时保留原有值
        """
        self.db = db
        if strategy_id:
            self.strategy_id = strategy_id
        self._db_cache_value = None
        self._db_cache_ts = 0.0

    # ------------------------------------------------------------------
    # 对外唯一入口（异步）
    # ------------------------------------------------------------------

    async def get_effective_margin_limit(self) -> Tuple[Optional[float], str]:
        """
        获取生效的保证金限额（唯一对外限额入口）

        优先级链（取第一个可用值，不做 min）：
        1. 月度分配额（DB → config.capital_limits.monthly_limit）可用 → 直接用月度额；
        2. 月度额不可用 → 用 trading.total_position_margin_limit（静态兜底）；
        3. 两者都不可用 → fail-open（返回 (None, SOURCE_NONE) 并告警）。

        说明：月度额代表当月资金分配的真实额度，必须优先于静态兜底生效，
        否则月度分配会被固定兜底值压制而失效（如 new_coin 月度额 156.33 被静态 150 压制）。

        Returns:
            (限额 USDT 或 None, 来源标签)：
            - 限额为 None 表示三级均不可用，调用方按 fail-open 放行
        """
        monthly, monthly_source = await self.resolve_monthly_limit()
        static = self._get_nested_float(
            "trading", "total_position_margin_limit",
            log_msg="读取静态兜底保证金上限失败",
        )

        # 月度额可用（DB / config，非静态兜底）→ 直接采用，月度分配优先
        if monthly is not None and monthly_source != SOURCE_STATIC:
            if static is not None and static != monthly:
                logger.info(
                    "月度额与静态兜底不一致，月度额优先",
                    monthly_limit=monthly,
                    static_limit=static,
                    strategy_id=self.strategy_id,
                )
            limit, source = monthly, monthly_source
        elif static is not None:
            # 月度额不可用 → 回退静态兜底
            limit, source = static, SOURCE_STATIC
        else:
            # 两者都不可用：fail-open，不阻断策略主循环
            logger.warning(
                "月度额与静态兜底均不可用，fail-open 放行开仓",
                config_path=self.config_path,
                strategy_id=self.strategy_id,
            )
            return None, SOURCE_NONE

        logger.info(
            "生效保证金限额",
            limit=limit,
            source=source,
            monthly_limit=monthly,
            static_limit=static,
            strategy_id=self.strategy_id,
        )
        return limit, source

    async def resolve_monthly_limit(self) -> Tuple[Optional[float], str]:
        """
        解析月度分配额度（DB → config → 静态兜底）

        Returns:
            (额度 USDT 或 None, 来源标签)
        """
        # 1) DB 主来源
        db_value, db_source = await self._query_db_allocation()
        if db_value is not None:
            return db_value, db_source

        # 2) config 兜底（部署后可能缺失）
        monthly = self._get_nested_float(
            "capital_limits", "monthly_limit",
            log_msg="读取分配资金失败",
        )
        if monthly is not None:
            return monthly, SOURCE_CONFIG

        # 3) 静态兜底
        static = self._get_nested_float(
            "trading", "total_position_margin_limit",
            log_msg="读取静态兜底保证金上限失败",
        )
        if static is not None:
            return static, SOURCE_STATIC

        # 4) 全部不可用
        return None, SOURCE_NONE

    async def can_open_within_limit(
        self,
        occupied_margin: float,
        new_margin: float,
    ) -> Tuple[bool, str]:
        """
        保证金口径的通用开仓校验（4 个策略共用，避免重复实现）

        允许开仓 ⟺ occupied_margin + new_margin ≤ 生效限额。

        Args:
            occupied_margin: 策略当前总占用保证金（USDT）
            new_margin: 拟开新仓保证金（USDT）

        Returns:
            (是否允许开仓, 拒绝原因；允许时原因为空串)
        """
        limit, source = await self.get_effective_margin_limit()
        if limit is None:
            # 三级均不可用：fail-open 放行
            return True, ""

        total = occupied_margin + new_margin
        if total > limit:
            logger.warning(
                "总持仓保证金超限，拒绝开仓",
                occupied=round(occupied_margin, 4),
                new_margin=round(new_margin, 4),
                total=round(total, 4),
                limit=limit,
                source=source,
                strategy_id=self.strategy_id,
            )
            return False, f"总持仓保证金超限({total:.2f}/{limit:.2f})"

        return True, ""

    def get_min_position_margin(self) -> Optional[float]:
        """
        读取最小可开仓保证金（缩仓后低于此值则拒开，决策 D3）

        Returns:
            float: 最小保证金（USDT）；未配置返回 None（调用方不做门槛限制）
        """
        return self._get_nested_float(
            "trading", "min_position_margin",
            log_msg="读取最小开仓保证金失败",
        )

    # ------------------------------------------------------------------
    # DB 读取（带 TTL 缓存与异常降级）
    # ------------------------------------------------------------------

    async def _query_db_allocation(self) -> Tuple[Optional[float], str]:
        """
        查询 DB 当月分配额（带 TTL 缓存与异常降级）

        Returns:
            (额度 USDT 或 None, 来源标签)
        """
        if self.db is None or not self.strategy_id:
            # 未注入 DB 或策略标识：跳过 DB 来源
            return None, SOURCE_NONE

        now = time.monotonic()
        ttl = self._get_db_cache_ttl()
        if self._db_cache_ts > 0 and (now - self._db_cache_ts) < ttl:
            return self._db_cache_value, SOURCE_DB_CACHE

        month = self._current_month()
        try:
            value = await asyncio.wait_for(
                self._fetch_allocation_from_db(month),
                timeout=self._get_db_query_timeout(),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "DB 分配额查询超时，沿用最近一次成功值或降级",
                month=month,
                strategy_id=self.strategy_id,
            )
            return self._fallback_to_last_success()
        except Exception as e:
            logger.warning(
                "DB 分配额查询异常，沿用最近一次成功值或降级",
                month=month,
                strategy_id=self.strategy_id,
                error=str(e),
            )
            return self._fallback_to_last_success()

        # 查询成功：写入缓存（含"无记录"结果，避免频繁查库）
        self._db_cache_value = value
        self._db_cache_ts = now

        if value is None:
            logger.warning(
                "DB 无当月分配记录，不回退上月，转配置兜底",
                month=month,
                strategy_id=self.strategy_id,
            )
            return None, SOURCE_NONE

        self._db_last_success_value = value
        logger.info(
            "读取到当月分配额（DB）",
            month=month,
            strategy_id=self.strategy_id,
            allocated_amount=value,
        )
        return value, SOURCE_DB

    async def _fetch_allocation_from_db(self, month: str) -> Optional[float]:
        """
        从 public.capital_allocation 读取指定月份本策略的分配额

        Args:
            month: 目标月份（格式 YYYY-MM）

        Returns:
            float: 分配额（USDT）；无记录 / 无本策略条目 / 解析失败返回 None
        """
        row = await self.db.fetch_one(
            """
            SELECT entries
            FROM public.capital_allocation
            WHERE month = $1 AND status = 'active'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            month,
        )
        if not row:
            return None

        entries = self._parse_entries(row.get("entries"))
        if entries is None:
            logger.warning("capital_allocation.entries 解析失败", month=month)
            return None

        for item in entries:
            if not isinstance(item, dict):
                continue
            if item.get("strategy_id") != self.strategy_id:
                continue
            amount = item.get("allocated_amount")
            try:
                return float(amount) if amount is not None else None
            except (TypeError, ValueError):
                logger.warning("分配额字段非法", month=month, allocated_amount=amount)
                return None

        logger.warning(
            "DB 当月记录中无本策略条目，转配置兜底",
            month=month,
            strategy_id=self.strategy_id,
        )
        return None

    def _fallback_to_last_success(self) -> Tuple[Optional[float], str]:
        """
        DB 异常时的降级：沿用最近一次成功读取的分配额

        Returns:
            (额度 USDT 或 None, 来源标签)
        """
        if self._db_last_success_value is not None:
            logger.warning(
                "沿用最近一次成功读取的分配额",
                strategy_id=self.strategy_id,
                allocated_amount=self._db_last_success_value,
            )
            return self._db_last_success_value, SOURCE_DB_STALE
        return None, SOURCE_NONE

    def _parse_entries(self, raw: Any) -> Optional[list]:
        """
        解析 capital_allocation.entries 字段（兼容 JSON 字符串 / 已解析列表）

        Args:
            raw: 数据库返回的 entries 原始值

        Returns:
            list: 解析成功返回列表；类型非法返回 None
        """
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, list) else None
        return None

    def _get_nested_float_with_default(
        self,
        section: str,
        key: str,
        default: float,
        log_msg: str,
    ) -> float:
        """
        读取嵌套配置中的正浮点数，缺失 / 非法 / ≤ 0 时使用代码级默认值

        Args:
            section: 配置一级节点名（如 "capital_limits"）
            key: 配置二级键名（如 "db_cache_ttl_seconds"）
            default: 代码级兜底默认值（非业务阈值，仅防配置被覆盖后失去保护）
            log_msg: 读取失败时的日志消息（中文）

        Returns:
            float: 生效值（配置合法则用配置值，否则用 default）
        """
        value = self._get_nested_float(section, key, log_msg=log_msg)
        return value if value is not None and value > 0 else default

    def _get_db_cache_ttl(self) -> float:
        """读取 DB 缓存 TTL（秒），配置缺失时使用代码级默认值"""
        return self._get_nested_float_with_default(
            "capital_limits", "db_cache_ttl_seconds",
            _DEFAULT_DB_CACHE_TTL_SECONDS, "读取 DB 缓存 TTL 失败，使用默认值",
        )

    def _get_db_query_timeout(self) -> float:
        """读取 DB 查询超时（秒），配置缺失时使用代码级默认值"""
        return self._get_nested_float_with_default(
            "capital_limits", "db_query_timeout_seconds",
            _DEFAULT_DB_QUERY_TIMEOUT_SECONDS, "读取 DB 查询超时失败，使用默认值",
        )

    @staticmethod
    def _current_month() -> str:
        """返回当前月份（CST 时区，格式 YYYY-MM）"""
        return datetime.now(_CST).strftime("%Y-%m")

    # ------------------------------------------------------------------
    # 兼容保留的同步方法（已废弃，仅兼容未改造调用方）
    # ------------------------------------------------------------------

    def get_allocated_capital(self) -> Optional[float]:
        """
        【已废弃】读取分配资金上限（名义口径语义，存在歧义）

        请改用 `get_effective_margin_limit()`（保证金口径，异步，含 DB 来源）。

        Returns:
            float: 分配资金 USDT 金额
            None: 未配置 capital_limits，调用方应使用全账户余额
        """
        self._warn_deprecated("get_allocated_capital")
        return self._get_nested_float(
            "capital_limits", "monthly_limit",
            log_msg="读取分配资金失败，将使用全账户余额",
        )

    def get_total_margin_limit(self) -> Optional[float]:
        """
        【已废弃】动态读取总持仓保证金上限

        请改用 `get_effective_margin_limit()`。

        Returns:
            float: 总持仓保证金上限（USDT）
            None: 未配置任何来源，调用方不做限制
        """
        self._warn_deprecated("get_total_margin_limit")
        monthly = self._get_nested_float(
            "capital_limits", "monthly_limit",
            log_msg="读取总持仓保证金上限失败",
        )
        if monthly is not None:
            return monthly
        return self._get_nested_float(
            "trading", "total_position_margin_limit",
            log_msg="读取总持仓保证金上限失败",
        )

    def can_open_position(self, current_positions_value: float, new_position_value: float) -> bool:
        """
        【已废弃】检查是否可以开新仓（名义口径，与保证金口径混用）

        请改用 `can_open_within_limit()` / `get_effective_margin_limit()`。

        Args:
            current_positions_value: 当前所有持仓总价值（USDT）
            new_position_value: 新仓价值（USDT）

        Returns:
            bool: True 表示可以开仓，False 表示总仓位超限
        """
        self._warn_deprecated("can_open_position")
        allocated = self.get_allocated_capital()
        if allocated is None:
            return True

        total_after_opening = current_positions_value + new_position_value
        if total_after_opening > allocated:
            logger.warning(
                "总仓位超限，拒绝开仓（废弃口径）",
                current=current_positions_value,
                new=new_position_value,
                total=total_after_opening,
                limit=allocated,
            )
            return False
        return True

    def is_allocated(self) -> bool:
        """
        【已废弃】capital_limits 是否已配置

        Returns:
            bool: True 表示已配置，False 表示未配置
        """
        self._warn_deprecated("is_allocated")
        return self.get_allocated_capital() is not None

    def get_account_ratio_cap(self) -> Optional[float]:
        """
        动态读取总持仓保证金占账户权益的比例阈值

        读取根级 position_sizing.total.account_ratio_cap（每月由 AI 资金分配自动更新），
        每次调用重新读取配置文件，确保获取最新值（禁止调用方硬编码）。

        Returns:
            float: 比例阈值（如 0.3 表示总持仓保证金 ≤ 账户权益 30%）
            None: 未配置，调用方不做限制
        """
        return self._get_nested_float(
            "position_sizing", "total", "account_ratio_cap",
            log_msg="读取总持仓保证金比例阈值失败，调用方不做限制",
        )

    def get_allocated_ratio(self) -> Optional[float]:
        """
        读取分配比例

        Returns:
            float: 分配比例（如 0.36）
            None: 未配置 capital_limits
        """
        return self._get_nested_float(
            "capital_limits", "allocated_ratio",
            log_msg="读取分配比例失败",
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _warn_deprecated(self, method_name: str) -> None:
        """
        记录一次废弃告警（每个方法仅告警一次，避免循环刷屏）

        Args:
            method_name: 被调用的废弃方法名
        """
        if method_name in self._deprecation_warned:
            return
        self._deprecation_warned.add(method_name)
        logger.warning(
            "CapitalManager 同步方法已废弃，请改用 get_effective_margin_limit()/can_open_within_limit()",
            method=method_name,
            config_path=self.config_path,
        )

    def _get_nested_float(self, *keys: str, log_msg: str) -> Optional[float]:
        """
        按嵌套路径读取配置中的浮点值（任一节点缺失返回 None）

        供各读取方法复用，避免重复 try/except 与节点遍历模板。

        Args:
            keys: 配置嵌套键路径，如 ("trading", "total_position_margin_limit")
            log_msg: 读取失败时的日志消息（中文）

        Returns:
            float: 读取到的数值；未配置或读取异常返回 None
        """
        try:
            config = self._read_config()
            node = config
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return None
                node = node[key]
            if node is None:
                return None
            return float(node)
        except Exception as e:
            logger.warning(log_msg, config_path=self.config_path, error=str(e))
            return None

    def _read_config(self) -> Dict[str, Any]:
        """
        读取配置文件

        Returns:
            dict: 配置字典，读取失败返回空字典
        """
        if not self.config_path:
            logger.warning("配置文件路径为空", config_path=self.config_path)
            return {}

        # 尝试绝对路径
        if os.path.isabs(self.config_path):
            config_file = self.config_path
        else:
            # 相对路径：从项目根目录解析（当前文件所在目录的上一级）
            config_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                self.config_path,
            )

        if not os.path.exists(config_file):
            logger.warning("配置文件不存在", config_path=config_file)
            return {}

        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}