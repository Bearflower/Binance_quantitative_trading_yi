"""
配置管理工具
提供配置验证、热更新等功能
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.utils.config_loader import ConfigLoader

logger = logging.getLogger(__name__)


class ConfigValidator:
    """配置验证器"""
    
    @staticmethod
    def validate_exchange_config(config: Dict[str, Any]) -> List[str]:
        """
        验证交易所配置
        
        Args:
            config: 交易所配置
            
        Returns:
            错误信息列表
        """
        errors = []
        
        # 检查 API 密钥
        api_key = config.get('api_key', '')
        api_secret = config.get('api_secret', '')
        
        if not api_key or 'your_api_key' in api_key.lower():
            errors.append("请配置有效的币安 API 密钥")
        
        if not api_secret or 'your_secret' in api_secret.lower():
            errors.append("请配置有效的币安 API 密钥密钥")
        
        # 检查交易对
        symbol = config.get('symbol', '')
        if not symbol or not symbol.endswith('USDT'):
            errors.append("交易对格式不正确，应为 XXXUSDT")
        
        # 检查测试网/主网设置
        testnet = config.get('testnet', True)
        if testnet:
            logger.warning("当前使用测试网模式")
        else:
            logger.warning("当前使用主网模式，请注意风险")
        
        return errors
    
    @staticmethod
    def validate_strategy_config(config: Dict[str, Any]) -> List[str]:
        """
        验证策略配置
        
        Args:
            config: 策略配置
            
        Returns:
            错误信息列表
        """
        errors = []
        
        # 检查指标参数
        indicators = config.get('indicators', {})
        
        adx_period = indicators.get('adx_period', 14)
        if adx_period <= 0:
            errors.append("ADX 周期必须大于 0")
        
        atr_period = indicators.get('atr_period', 14)
        if atr_period <= 0:
            errors.append("ATR 周期必须大于 0")
        
        # 检查网格参数
        grid = config.get('grid', {})
        
        base_grid_count = grid.get('base_grid_count', 30)
        min_grid_count = grid.get('min_grid_count', 20)
        max_grid_count = grid.get('max_grid_count', 50)
        
        if min_grid_count < 2:
            errors.append("最小网格数不能小于 2")
        
        if max_grid_count > 100:
            errors.append("最大网格数不能大于 100")
        
        if min_grid_count >= max_grid_count:
            errors.append("最小网格数必须小于最大网格数")
        
        if base_grid_count < min_grid_count or base_grid_count > max_grid_count:
            errors.append(f"基准网格数应在 [{min_grid_count}, {max_grid_count}] 范围内")
        
        # 检查风险参数
        risk = config.get('risk', {})
        
        hard_stop_loss = risk.get('hard_stop_loss', -0.08)
        if hard_stop_loss >= 0:
            errors.append("硬止损必须为负数")
        
        if hard_stop_loss < -1:
            errors.append("硬止损不能小于 -100%")
        
        trailing_profit_start = risk.get('trailing_profit_start', 0.15)
        if trailing_profit_start <= 0:
            errors.append("移动止盈启动阈值必须为正数")
        
        return errors
    
    @staticmethod
    def validate_execution_config(config: Dict[str, Any]) -> List[str]:
        """
        验证执行配置
        
        Args:
            config: 执行配置
            
        Returns:
            错误信息列表
        """
        errors = []
        
        # 检查巡检间隔
        inspection_interval = config.get('inspection_interval', 3600)
        if inspection_interval < 60:
            errors.append("巡检间隔不能小于 60 秒")
        
        # 检查参数调整配置
        param_adjustment = config.get('parameter_adjustment', {})
        
        if param_adjustment:
            enabled = param_adjustment.get('enabled', True)
            min_interval = param_adjustment.get('min_interval', 14400)
            max_per_day = param_adjustment.get('max_adjustments_per_day', 6)
            
            if enabled and min_interval < 3600:
                errors.append("参数调整最小间隔不能小于 1 小时")
            
            if enabled and max_per_day < 1:
                errors.append("每日最大调整次数不能小于 1")
        
        # 检查滑点保护
        slippage = config.get('slippage_protection', {})
        
        if slippage:
            limit_timeout = slippage.get('limit_order_timeout', 3)
            if limit_timeout <= 0:
                errors.append("限价单超时必须大于 0")
        
        return errors
    
    @staticmethod
    def validate_monitoring_config(config: Dict[str, Any]) -> List[str]:
        """
        验证监控配置
        
        Args:
            config: 监控配置
            
        Returns:
            错误信息列表
        """
        errors = []
        
        # 检查日志配置
        logging_config = config.get('logging', {})
        
        log_level = logging_config.get('level', 'INFO')
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        
        if log_level not in valid_levels:
            errors.append(f"日志级别必须是 {valid_levels} 之一")
        
        # 检查报警配置
        alert_config = config.get('alert', {})
        
        if alert_config.get('enabled', False):
            # 检查是否配置了至少一个报警渠道
            has_channel = any([
                alert_config.get('feishu_webhook'),
                alert_config.get('dingding_webhook'),
                alert_config.get('telegram_bot_token')
            ])
            
            if not has_channel:
                errors.append("启用报警但未配置任何报警渠道")
        
        return errors


class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        """
        初始化配置管理器
        
        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path
        self._config_loader = ConfigLoader(config_path)
        self._config: Dict[str, Any] = {}
        self._last_modified: Optional[datetime] = None
        self._validation_errors: List[str] = []
    
    def load(self, validate: bool = True) -> bool:
        """
        加载配置
        
        Args:
            validate: 是否验证配置
            
        Returns:
            是否成功加载
        """
        try:
            self._config = self._config_loader.load()
            self._last_modified = datetime.now()
            
            if validate:
                self._validation_errors = self.validate_all()
                if self._validation_errors:
                    logger.warning(f"配置验证发现 {len(self._validation_errors)} 个问题")
                    for error in self._validation_errors:
                        logger.warning(f"  - {error}")
                    return False
            
            logger.info("配置加载成功")
            return True
            
        except Exception as e:
            logger.error(f"配置加载失败：{e}")
            return False
    
    def validate_all(self) -> List[str]:
        """
        验证所有配置
        
        Returns:
            错误信息列表
        """
        all_errors = []
        
        # 验证交易所配置
        exchange_config = self._config.get('exchange', {})
        all_errors.extend(ConfigValidator.validate_exchange_config(exchange_config))
        
        # 验证策略配置
        strategy_config = self._config.get('strategy', {})
        all_errors.extend(ConfigValidator.validate_strategy_config(strategy_config))
        
        # 验证执行配置
        execution_config = self._config.get('execution', {})
        all_errors.extend(ConfigValidator.validate_execution_config(execution_config))
        
        # 验证监控配置
        monitoring_config = self._config.get('monitoring', {})
        all_errors.extend(ConfigValidator.validate_monitoring_config(monitoring_config))
        
        return all_errors
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        return self._config_loader.get(key, default)
    
    def get_all(self) -> Dict[str, Any]:
        """获取完整配置"""
        return self._config.copy()
    
    def is_modified(self) -> bool:
        """
        检查配置文件是否被修改
        
        Returns:
            是否被修改
        """
        config_file = Path(self.config_path)
        if config_file.exists():
            mtime = datetime.fromtimestamp(config_file.stat().st_mtime)
            return mtime > self._last_modified if self._last_modified else True
        return False
    
    def reload_if_needed(self) -> bool:
        """
        如果配置文件被修改则重新加载
        
        Returns:
            是否重新加载
        """
        if self.is_modified():
            logger.info("检测到配置文件修改，重新加载...")
            return self.load()
        return False
    
    def get_validation_errors(self) -> List[str]:
        """获取验证错误列表"""
        return self._validation_errors.copy()
    
    def is_valid(self) -> bool:
        """检查配置是否有效"""
        return len(self._validation_errors) == 0
    
    def print_config_summary(self) -> None:
        """打印配置摘要"""
        logger.info("=" * 60)
        logger.info("配置摘要")
        logger.info("=" * 60)
        
        # 交易所配置
        exchange = self.get('exchange')
        if exchange:
            logger.info(f"交易所：{'测试网' if exchange.get('testnet') else '主网'}")
            logger.info(f"交易对：{exchange.get('symbol')}")
        
        # 策略配置
        strategy = self.get('strategy')
        if strategy:
            indicators = strategy.get('indicators', {})
            logger.info(f"ADX 周期：{indicators.get('adx_period')}")
            logger.info(f"ATR 周期：{indicators.get('atr_period')}")
            
            grid = strategy.get('grid', {})
            logger.info(f"基准网格数：{grid.get('base_grid_count')}")
            
            risk = strategy.get('risk', {})
            logger.info(f"硬止损：{risk.get('hard_stop_loss')*100}%")
        
        # 执行配置
        execution = self.get('execution')
        if execution:
            logger.info(f"巡检间隔：{execution.get('inspection_interval')}秒")
            
            param_adj = execution.get('parameter_adjustment', {})
            if param_adj:
                logger.info(f"参数调整：{'启用' if param_adj.get('enabled') else '禁用'}")
        
        logger.info("=" * 60)


def create_default_config(output_path: str = "config/config.yaml") -> bool:
    """
    创建默认配置文件
    
    Args:
        output_path: 输出路径
        
    Returns:
        是否成功创建
    """
    default_config = {
        'exchange': {
            'api_key': '${BINANCE_API_KEY}',
            'api_secret': '${BINANCE_API_SECRET}',
            'testnet': False,
            'symbol': 'BTCUSDT',
            'contract_type': 'PERPETUAL'
        },
        'strategy': {
            'indicators': {
                'adx_period': 14,
                'adx_trend_threshold': 25,
                'adx_weak_threshold': 20,
                'ema_fast': 20,
                'ema_slow': 50,
                'atr_period': 14,
                'atr_smoothing': 14
            },
            'grid': {
                'base_grid_count': 30,
                'min_grid_count': 20,
                'max_grid_count': 50,
                'base_atr_window': 90,
                'leverage': 10
            },
            'risk': {
                'hard_stop_loss': -0.08,
                'trailing_profit_start': 0.15,
                'trailing_profit_retrace': 0.5,
                'emergency_break_layers': 3,
                'emergency_break_window': 300,
                'position_coefficient': 0.5
            }
        },
        'execution': {
            'inspection_interval': 3600,
            'atr_change_threshold': 0.2,
            'parameter_adjustment': {
                'enabled': True,
                'min_interval': 14400,
                'max_adjustments_per_day': 6
            },
            'slippage_protection': {
                'limit_order_timeout': 3,
                'optimal_price_timeout': 2,
                'market_order_fallback': True
            }
        },
        'monitoring': {
            'logging': {
                'level': 'INFO',
                'file': 'logs/adaptive_grid.log',
                'max_size': 10485760,
                'backup_count': 5,
                'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            },
            'alert': {
                'enabled': False,
                'alert_on_parameter_adjustment': True,
                'feishu_webhook': '${FEISHU_WEBHOOK}',
                'dingding_webhook': '${DINGDING_WEBHOOK}',
                'telegram_bot_token': '${TELEGRAM_BOT_TOKEN}',
                'telegram_chat_id': '${TELEGRAM_CHAT_ID}'
            },
            'metrics': {
                'enabled': True,
                'report_interval': 3600
            }
        }
    }
    
    try:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("# 自适应趋势网格策略系统 - 配置文件\n")
            f.write("# 最后更新：2026-03-19\n\n")
            yaml.dump(default_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        logger.info(f"默认配置文件已创建：{output_path}")
        return True
        
    except Exception as e:
        logger.error(f"创建配置文件失败：{e}")
        return False
