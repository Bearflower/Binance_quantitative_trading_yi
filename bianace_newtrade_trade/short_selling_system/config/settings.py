"""
系统配置管理
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """系统配置类"""
    
    # 币安 API 配置
    binance_api_key: str = ""
    binance_api_secret: str = ""  # 用于交易 API 的密钥
    
    @property
    def binance_secret_key(self) -> str:
        """兼容旧代码，返回 binance_api_secret"""
        return self.binance_api_secret
    
    # 飞书通知配置
    feishu_webhook: Optional[str] = None
    
    # 数据库配置
    # PostgreSQL: postgresql://user:password@host:port/database?schema=schema_name
    # 本地开发（SSH 隧道）：postgresql://user:pass@localhost:5432/db?schema=schema
    # 服务器部署：postgresql://user:pass@postgres:5432/db?schema=schema
    database_url: str = "postgresql://short_selling_user:ShortSell@2024@localhost:5432/trading_platform?schema=schema_short_selling"
    
    # 日志配置
    log_level: str = "INFO"
    log_file: str = "logs/app.log"
    
    # 交易配置
    default_position_size: float = 4.0
    default_leverage: int = 5
    max_position_size: float = 10.0
    min_position_size: float = 2.0
    
    # 风控配置
    default_stop_loss_percent: float = 0.05
    default_take_profit_percent_1: float = 0.20
    default_take_profit_percent_2: float = 0.30
    max_holding_hours: int = 24
    
    # V4.1.1 ATR 止损止盈配置
    use_atr_sl_tp: bool = True  # 是否使用 ATR 止损止盈
    stop_loss_atr_multiplier: float = 2.0  # 止损：2.0 ATR
    take_profit_atr_multiplier: float = 2.5  # 止盈：2.5 ATR
    atr_period: int = 14  # ATR 计算周期
    
    # 评分配置
    min_signal_score: float = 7.0
    signal_expire_hours: int = 1
    
    # 报告配置
    save_reports: bool = True
    reports_dir: str = "reports"
    report_formats: list = ["json", "markdown"]
    
    # 监控配置
    new_coin_high_freq_interval: int = 60  # 0-24 小时新币扫描间隔 (秒)
    new_coin_normal_freq_interval: int = 300  # 1-7 天新币扫描间隔 (秒)
    no_new_coin_interval: int = 3600  # 无新币时扫描间隔 (秒)
    
    # 二次评分配置
    rescore_enabled: bool = True  # 是否启用二次评分
    max_rescore_attempts: int = 3  # 最大二次评分次数
    rescore_interval_minutes: int = 30  # 二次评分间隔（分钟）
    rescore_hours_limit: int = 72  # 二次评分时间窗口（小时）- 从 24 小时延长到 72 小时
    
    # 评分日志配置
    scoring_report_retention_days: int = 7  # 评分报告保留天数
    scoring_log_dir: str = "logs/scoring_reports"  # 评分报告目录
    
    # 汇总通知配置
    send_summary_notification: bool = True  # 是否发送汇总通知
    summary_notification_after_attempts: int = 3  # 完成几次评分后发送汇总通知
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# 全局配置实例
settings = Settings()


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent


def get_data_dir() -> Path:
    """获取数据目录"""
    return get_project_root() / "data"


def get_logs_dir() -> Path:
    """获取日志目录"""
    return get_project_root() / "logs"


def get_cache_dir() -> Path:
    """获取缓存目录"""
    return get_project_root() / "data" / "cache"


# 确保目录存在
for dir_func in [get_data_dir, get_logs_dir, get_cache_dir]:
    dir_path = dir_func()
    dir_path.mkdir(parents=True, exist_ok=True)
