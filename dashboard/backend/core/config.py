"""
Dashboard 配置管理
从环境变量和配置文件加载应用配置
"""
from typing import Dict, List, Any
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings
import structlog


logger = structlog.get_logger()


class Settings(BaseSettings):
    """
    应用配置（从环境变量加载）

    环境变量优先级高于配置文件，用于部署时动态调整参数。
    """

    # 应用配置
    app_version: str = "1.0.0"
    timezone_offset: int = 8

    # API 服务配置
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_debug: bool = False

    # 数据库配置
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "trading"
    db_user: str = "postgres"
    db_password: str = ""
    db_min_pool_size: int = 5
    db_max_pool_size: int = 20

    # Binance API 配置
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False

    # 缓存配置
    cache_enabled: bool = True
    cache_ttl_daily: int = 60         # 日报缓存 1 分钟（实时化后缩短）
    cache_ttl_weekly: int = 180       # 周报缓存 3 分钟
    cache_ttl_monthly: int = 300      # 月报缓存 5 分钟
    cache_ttl_metadata: int = 86400   # 元数据缓存 24 小时
    cache_ttl_account: int = 30       # 账户净资产缓存 30 秒（实时快照）

    class Config:
        """Pydantic 配置"""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # 忽略额外的环境变量


def load_config(config_path: str = None) -> Dict[str, Any]:
    """
    加载 YAML 配置文件

    支持环境变量替换，格式为 ${ENV_VAR}。

    Args:
        config_path: 配置文件路径，默认为 dashboard/backend/config.yaml

    Returns:
        配置字典
    """
    if config_path is None:
        # 默认配置文件路径
        config_path = Path(__file__).parent.parent / "config.yaml"

    config_file = Path(config_path)
    if not config_file.exists():
        logger.warning(
            "配置文件不存在，使用默认配置",
            config_path=str(config_path)
        )
        return {}

    with open(config_file, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # 递归替换环境变量
    config = _replace_env_vars(config)

    logger.info(
        "配置文件加载完成",
        config_path=str(config_path),
        keys=list(config.keys())
    )

    return config


def _replace_env_vars(obj: Any) -> Any:
    """
    递归替换配置中的环境变量

    支持格式：${ENV_VAR} 或 ${ENV_VAR:default_value}

    Args:
        obj: 配置对象（字典、列表、字符串等）

    Returns:
        替换后的配置对象
    """
    import os

    if isinstance(obj, dict):
        return {k: _replace_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_env_vars(item) for item in obj]
    elif isinstance(obj, str):
        # 匹配 ${VAR} 或 ${VAR:default}
        if obj.startswith("${") and obj.endswith("}"):
            var_spec = obj[2:-1]  # 去掉 ${ 和 }

            # 检查是否有默认值
            if ":" in var_spec:
                var_name, default_value = var_spec.split(":", 1)
            else:
                var_name = var_spec
                default_value = ""

            # 从环境变量读取，如果不存在则使用默认值
            return os.getenv(var_name, default_value)
        else:
            return obj
    else:
        return obj


def get_strategy_config(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    获取策略配置

    Args:
        config: 完整配置字典

    Returns:
        策略配置字典 {strategy_key: {name, emoji, symbols}}
    """
    strategies = config.get("strategies", {})

    # 如果配置文件中没有策略配置，使用默认配置
    if not strategies:
        strategies = {
            "btc_eth": {
                "name": "MTPCS策略",
                "description": "基于BTC和ETH的多时间框架价格通道突破策略",
                "emoji": "📈",
                "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT"]
            },
            "new_coin": {
                "name": "新币做空策略",
                "description": "新币上市做空策略，利用新币下跌趋势获利",
                "emoji": "📉",
                "symbols": []
            },
            "hrs": {
                "name": "HRS策略",
                "description": "混合反转策略，利用资金费率和OI/市值比进行做空和做多",
                "emoji": "🔄",
                "symbols": []
            }
        }
    else:
        # 确保每个策略都有description字段
        for strategy_id, strategy_data in strategies.items():
            if "description" not in strategy_data:
                strategy_data["description"] = strategy_data.get("name", strategy_id)

    return strategies


# ========================================
# 风控阈值配置中心
# ========================================

# 风控阈值默认值与 key 路径（缺失字段时回退默认并记 warning）
_RISK_DEFAULTS = {
    ("risk", "occupancy_warning_ratio"): 0.8,
    ("risk", "consecutive_loss_days"): 3,
    ("risk", "daily_drawdown_pct"): 5.0,
    ("risk", "stop_loss_days_window"): 7,
    ("risk", "account_ratio_caps"): {},          # 各策略 cap 对账，缺失则空 dict
}


def load_risk_config() -> Dict[str, Any]:
    """
    加载 Dashboard 风控阈值配置（唯一权威源）

    从 dashboard/backend/config/risk.yaml 读取风控阈值：
      - 文件缺失或任一关键阈值缺失 → 回退默认值并打印警告日志（强校验但优雅降级）。
      - 返回结构：{ "risk": { occupancy_warning_ratio, consecutive_loss_days,
              daily_drawdown_pct, stop_loss_days_window, account_ratio_caps } }

    Returns:
        dict: 风控配置字典；即使读不到文件也保证关键阈值存在（带默认值）。
    """
    risk_config_path = Path(__file__).parent.parent / "config" / "risk.yaml"
    if not risk_config_path.exists():
        logger.warning(
            "风控配置文件缺失，使用默认风控阈值",
            risk_config_path=str(risk_config_path),
        )
        config_data = {}
    else:
        try:
            with open(risk_config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(
                "风控配置文件读取异常，使用默认风控阈值",
                risk_config_path=str(risk_config_path),
                error=str(e),
            )
            config_data = {}

    # 递归读取嵌套节点，缺失字段回退默认值并记录警告
    def _read_nested(path: tuple):
        node = config_data
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                node = None
                break
        return node

    risk = config_data.get("risk", {})
    if not isinstance(risk, dict):
        risk = {}
        logger.warning("风控配置中 risk 节点缺失或非法，使用空 dict")

    for path, default in _RISK_DEFAULTS.items():
        value = _read_nested(path)
        if value is None:
            logger.warning(
                "风控阈值缺失，使用默认值",
                field=".".join(path),
                default=default,
            )
            _set_nested(risk, path[1:], default)

    resolved = {"risk": risk}
    logger.info(
        "风控阈值配置加载完成",
        occupancy_warning_ratio=risk.get("occupancy_warning_ratio"),
        consecutive_loss_days=risk.get("consecutive_loss_days"),
        daily_drawdown_pct=risk.get("daily_drawdown_pct"),
        stop_loss_days_window=risk.get("stop_loss_days_window"),
        account_ratio_caps=risk.get("account_ratio_caps"),
    )
    return resolved


def _set_nested(node: Dict[str, Any], keys: tuple, value: Any) -> None:
    """按嵌套路径设置配置值（用于填充默认值）"""
    for key in keys[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[keys[-1]] = value


# 全局风控阈值实例（模块加载时初始化，供所有新接口读取）
risk_settings = load_risk_config()


# 全局配置实例
settings = Settings()

# 全局配置字典（从配置文件加载）
config = load_config()

# 策略配置
strategy_config = get_strategy_config(config)


def init_settings_from_config():
    """
    从配置文件初始化 settings
    
    优先级：环境变量 > 配置文件 > 默认值
    """
    # 应用配置
    if "app" in config:
        app_config = config["app"]
        if "version" in app_config and not os.getenv("APP_VERSION"):
            settings.app_version = app_config["version"]
        if "timezone_offset" in app_config and not os.getenv("TIMEZONE_OFFSET"):
            settings.timezone_offset = app_config["timezone_offset"]
    
    # API 配置
    if "api" in config:
        api_config = config["api"]
        if "host" in api_config and not os.getenv("API_HOST"):
            settings.api_host = api_config["host"]
        if "port" in api_config and not os.getenv("API_PORT"):
            settings.api_port = api_config["port"]
        if "debug" in api_config and not os.getenv("API_DEBUG"):
            settings.api_debug = api_config["debug"]
    
    # 数据库配置
    if "database" in config:
        db_config = config["database"]
        if "host" in db_config and not os.getenv("DB_HOST"):
            settings.db_host = db_config["host"]
        if "port" in db_config and not os.getenv("DB_PORT"):
            settings.db_port = db_config["port"]
        if "database" in db_config and not os.getenv("DB_NAME"):
            settings.db_name = db_config["database"]
        if "user" in db_config and not os.getenv("DB_USER"):
            settings.db_user = db_config["user"]
        if "password" in db_config and not os.getenv("DB_PASSWORD"):
            settings.db_password = db_config["password"]
        if "min_pool_size" in db_config and not os.getenv("DB_MIN_POOL_SIZE"):
            settings.db_min_pool_size = db_config["min_pool_size"]
        if "max_pool_size" in db_config and not os.getenv("DB_MAX_POOL_SIZE"):
            settings.db_max_pool_size = db_config["max_pool_size"]
    
    # 缓存配置
    if "cache" in config:
        cache_config = config["cache"]
        if "enabled" in cache_config and not os.getenv("CACHE_ENABLED"):
            settings.cache_enabled = cache_config["enabled"]
        if "ttl_daily" in cache_config and not os.getenv("CACHE_TTL_DAILY"):
            settings.cache_ttl_daily = cache_config["ttl_daily"]
        if "ttl_weekly" in cache_config and not os.getenv("CACHE_TTL_WEEKLY"):
            settings.cache_ttl_weekly = cache_config["ttl_weekly"]
        if "ttl_monthly" in cache_config and not os.getenv("CACHE_TTL_MONTHLY"):
            settings.cache_ttl_monthly = cache_config["ttl_monthly"]
        if "ttl_metadata" in cache_config and not os.getenv("CACHE_TTL_METADATA"):
            settings.cache_ttl_metadata = cache_config["ttl_metadata"]
        if "ttl_account" in cache_config and not os.getenv("CACHE_TTL_ACCOUNT"):
            settings.cache_ttl_account = cache_config["ttl_account"]
    
    # Binance 配置
    if "binance" in config:
        binance_config = config["binance"]
        if "api_key" in binance_config and not os.getenv("BINANCE_API_KEY"):
            settings.binance_api_key = binance_config["api_key"]
        if "api_secret" in binance_config and not os.getenv("BINANCE_API_SECRET"):
            settings.binance_api_secret = binance_config["api_secret"]
        if "testnet" in binance_config and not os.getenv("BINANCE_TESTNET"):
            settings.binance_testnet = binance_config["testnet"]
    
    logger.info(
        "配置初始化完成",
        app_version=settings.app_version,
        timezone_offset=settings.timezone_offset,
        cache_ttl_daily=settings.cache_ttl_daily,
        db_pool_size=f"{settings.db_min_pool_size}-{settings.db_max_pool_size}"
    )


# 在模块加载时初始化配置
import os
init_settings_from_config()
