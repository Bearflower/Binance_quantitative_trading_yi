"""
新币做空策略回测引擎
核心回测逻辑，协调各模块完成回测流程
"""
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from decimal import Decimal
import structlog
import pandas as pd
import yaml

from .data_loader import DataLoader
from .order_executor import OrderExecutor
from .position_manager import PositionManager
from .statistics_analyzer import StatisticsAnalyzer
from .report_generator import ReportGenerator

# 导入策略模块（复用现有代码）
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '../../strategies/new_coin'))
from scoring_engine import ScoringEngine
from pattern import PatternRecognizer


logger = structlog.get_logger()


class BacktestEngine:
    """回测引擎核心类
    
    职责：
    - 协调各个模块运行
    - 管理回测时间线
    - 执行回测逻辑
    - 记录交易过程
    """
    
    def __init__(self, config_path: str):
        """
        初始化回测引擎
        
        Args:
            config_path: 配置文件路径
        """
        # 加载配置
        self.config = self._load_config(config_path)
        
        # 回测参数
        backtest_config = self.config.get('backtest', {})
        self.initial_balance = Decimal(str(backtest_config.get('initial_balance', 500)))
        self.commission_rate = Decimal(str(backtest_config.get('commission_rate', 0.0004)))
        self.slippage_rate = Decimal(str(backtest_config.get('slippage_rate', 0.0001)))
        self.leverage = backtest_config.get('leverage', 2)
        
        # 时间范围处理：end_date 包含整天
        start_date_str = backtest_config.get('start_date', '2025-01-01')
        end_date_str = backtest_config.get('end_date', '2025-12-31')
        
        # 如果日期字符串不包含时间，添加时间部分
        if ' ' not in start_date_str:
            self.start_date = start_date_str + ' 00:00:00'
        else:
            self.start_date = start_date_str
            
        if ' ' not in end_date_str:
            self.end_date = end_date_str + ' 23:59:59'
        else:
            self.end_date = end_date_str
        
        # 初始化模块
        self.data_loader = DataLoader(self.config)
        self.order_executor = OrderExecutor(
            commission_rate=self.commission_rate,
            slippage_rate=self.slippage_rate,
            leverage=self.leverage
        )
        self.position_manager = PositionManager(
            initial_balance=self.initial_balance,
            config=self.config
        )
        self.statistics_analyzer = StatisticsAnalyzer()
        self.report_generator = ReportGenerator()
        
        # 复用策略模块
        self.scoring_engine = ScoringEngine(self.config)
        self.pattern_recognizer = PatternRecognizer(self.config)
        
        # 交易记录
        self.trades: List[Dict[str, Any]] = []
        
        # 账户状态
        self.balance = self.initial_balance
        self.equity_curve: List[Dict[str, Any]] = []
        
        # 已交易币种列表（一币一单）
        self.traded_symbols: List[str] = []
        
        # 连续亏损计数
        self.consecutive_losses = 0
        
        logger.info(
            "回测引擎初始化完成",
            initial_balance=float(self.initial_balance),
            commission_rate=float(self.commission_rate),
            slippage_rate=float(self.slippage_rate),
            leverage=self.leverage,
            start_date=self.start_date,
            end_date=self.end_date
        )
    
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """
        加载配置文件
        
        Args:
            config_path: 配置文件路径
            
        Returns:
            配置字典
        """
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            logger.info(f"配置文件加载成功: {config_path}")
            return config
        except Exception as e:
            logger.error(f"配置文件加载失败: {e}")
            raise
    
    def run(self) -> Dict[str, Any]:
        """
        运行回测
        
        Returns:
            回测结果字典
        """
        logger.info("=" * 60)
        logger.info("开始回测")
        logger.info("=" * 60)
        
        try:
            # 1. 加载数据
            logger.info("步骤1: 加载数据")
            coin_list = self.data_loader.load_coin_list()
            logger.info(f"加载交易对列表: {len(coin_list)} 个币种")
            
            # 2. 遍历每个交易对
            logger.info("步骤2: 遍历交易对")
            for coin_info in coin_list:
                symbol = coin_info['symbol']
                # 兼容不同的字段名：listing_time 或 onboardDateStr
                listing_time = coin_info.get('listing_time') or coin_info.get('onboardDateStr')
                
                logger.info(f"\n处理交易对: {symbol}")
                
                # 检查是否已交易过（一币一单）
                if symbol in self.traded_symbols:
                    logger.info(f"跳过已交易币种: {symbol}")
                    continue
                
                # 加载K线数据
                klines = self.data_loader.load_klines(symbol)
                # 从配置读取分析阶段最少K线数（V4.1：避免硬编码14，对齐配置项 min_klines_for_analysis）
                min_klines_for_analysis = self.config.get('kline', {}).get('min_klines_for_analysis', 18)
                if not klines or len(klines) < min_klines_for_analysis:
                    logger.warning(f"K线数据不足: {symbol}, 跳过")
                    continue
                
                logger.info(f"加载K线数据: {len(klines)} 根")
                
                # 获取上线时间
                if listing_time:
                    # 处理UTC后缀
                    listing_time_clean = listing_time.replace(' UTC', '').strip()
                    listing_datetime = datetime.fromisoformat(listing_time_clean)
                else:
                    # 如果没有上线时间，使用第一根K线时间
                    listing_datetime = datetime.fromtimestamp(klines[0]['open_time'] / 1000)
                
                # 遍历每根K线（上线时间在每根K线中动态计算）
                self._process_klines(symbol, klines, listing_datetime)
            
            # 3. 统计分析
            logger.info("\n步骤3: 统计分析")
            statistics = self.statistics_analyzer.analyze(
                trades=self.trades,
                equity_curve=self.equity_curve,
                initial_balance=self.initial_balance
            )
            
            # 4. 生成报告
            logger.info("步骤4: 生成报告")
            report_path = self.report_generator.generate(
                statistics=statistics,
                trades=self.trades,
                equity_curve=self.equity_curve,
                config=self.config
            )
            
            logger.info("=" * 60)
            logger.info("回测完成")
            logger.info("=" * 60)
            
            return {
                'statistics': statistics,
                'trades': self.trades,
                'report_path': report_path
            }
            
        except Exception as e:
            logger.error(f"回测执行失败: {e}", exc_info=True)
            raise
    
    def _process_klines(
        self,
        symbol: str,
        klines: List[Dict[str, Any]],
        listing_datetime: datetime
    ) -> None:
        """
        处理K线数据
        
        Args:
            symbol: 交易对
            klines: K线数据列表
            listing_datetime: 上线时间
        """
        # 从配置读取分析阶段最少K线数（V4.1：避免硬编码，默认值对齐配置项 min_klines_for_analysis=18）
        min_klines = self.config.get('kline', {}).get('min_klines_for_analysis', 18)
        
        # 获取配置的上线时间窗口
        max_listing_hours = self.config.get('scoring', {}).get('veto_thresholds', {}).get('listing_hours', 48)
        
        for i in range(min_klines, len(klines)):
            current_kline = klines[i]
            current_time = datetime.fromtimestamp(current_kline['open_time'] / 1000)
            current_price = float(current_kline['close'])
            
            # 动态计算当前K线的上线时长
            listing_hours = (current_time - listing_datetime).total_seconds() / 3600
            
            # 检查上线时间是否超过窗口
            if listing_hours > max_listing_hours:
                logger.debug(f"跳过K线（上线时间过长）: {symbol}, K线索引={i}, 上线时长={listing_hours:.1f}小时")
                continue
            
            # 检查是否在回测时间范围内
            if current_time < datetime.fromisoformat(self.start_date):
                logger.debug(f"跳过K线（早于回测开始时间）: {symbol}, K线索引={i}, K线时间={current_time}")
                continue
            if current_time > datetime.fromisoformat(self.end_date):
                logger.debug(f"跳过K线（晚于回测结束时间）: {symbol}, K线索引={i}, K线时间={current_time}")
                break
            
            # 检查是否有持仓
            if self.position_manager.has_position(symbol):
                # 持仓管理（检查止损止盈）
                self._check_position_management(
                    symbol=symbol,
                    current_price=current_price,
                    current_time=current_time
                )
                continue
            
            # 计算OI/总交易量比率（模拟数据）
            # 注意：回测时需要从数据文件读取真实的OI和交易量数据
            oi_volume_ratio = self._calculate_oi_volume_ratio(symbol, klines[:i+1])
            
            # 检查一票否决（从配置读取阈值，避免硬编码）
            oi_veto_threshold = self.config.get('scoring', {}).get('oi_volume_ratio', {}).get('thresholds', {}).get('veto', 0.5)
            if oi_volume_ratio > oi_veto_threshold:
                logger.debug(f"一票否决: {symbol}, OI/交易量比率={oi_volume_ratio:.4f}")
                continue
            
            # 形态识别
            patterns = self.pattern_recognizer.detect(klines[:i+1])

            # V4.1新增：判断是否使用降级模式
            # 回测中 oi_change_rate=0.0（无OI历史数据），根据上线时间判断是否降级
            degraded_config = self.config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {})
            listing_hours_threshold = degraded_config.get('listing_hours_threshold', 3)
            sentiment_degraded = listing_hours < listing_hours_threshold

            # 计算评分
            # V4.1：保持funding_rate=0.0001（与V4.0一致），使回测结果可比
            # 情绪分优化（门槛50%、上限截断6.0）在实盘有真实费率数据时生效
            score_result = self.scoring_engine.score(
                symbol=symbol,
                oi_usd=1000000,  # 模拟数据
                total_volume_usd=1000000 / oi_volume_ratio if oi_volume_ratio > 0 else 0,
                funding_rate=0.0001,  # 模拟数据（与V4.0一致，年化10.95%，<50%不计分，情绪分=0）
                oi_change_rate=0.0,  # 回测无OI历史数据，默认为0
                three_tops_detected=patterns['three_tops'][0],
                three_tops_score=patterns['three_tops'][1],
                long_upper_shadow=patterns['long_upper_shadow'][0],
                long_upper_shadow_score=patterns['long_upper_shadow'][1],
                volume_divergence=patterns['volume_divergence'][0],
                volume_divergence_score=patterns['volume_divergence'][1],
                listing_hours=listing_hours,
                current_price=current_price,
                recent_coins_oi=[],
                sentiment_degraded=sentiment_degraded
            )

            # 判断是否入场
            three_tops_score = patterns['three_tops'][1]
            total_technical_score = (
                patterns['three_tops'][1] +
                patterns['long_upper_shadow'][1] +
                patterns['volume_divergence'][1]
            )

            if self.scoring_engine.should_entry(
                score_result,
                three_tops_score,
                total_technical_score,
                sentiment_degraded=sentiment_degraded
            ):
                # 计算ATR
                atr = self._calculate_atr(klines[:i+1])
                
                # 执行开仓
                self._execute_entry(
                    symbol=symbol,
                    current_price=current_price,
                    current_time=current_time,
                    atr=atr,
                    score_result=score_result
                )
    
    def _calculate_oi_volume_ratio(
        self,
        symbol: str,
        klines: List[Dict[str, Any]]
    ) -> float:
        """
        计算OI/总交易量比率（模拟数据）
        
        注意：实际回测时需要从数据文件读取真实的OI数据
        
        Args:
            symbol: 交易对
            klines: K线数据列表
            
        Returns:
            OI/总交易量比率
        """
        # 计算总交易量
        total_volume = sum(float(k.get('quote_asset_volume', 0)) for k in klines)
        
        # 模拟OI数据（实际应从数据文件读取）
        # 这里使用一个简单的模拟：OI = 总交易量 * 0.3
        oi = total_volume * 0.3
        
        # 计算比率
        ratio = oi / total_volume if total_volume > 0 else 0
        
        return ratio
    
    def _calculate_atr(
        self,
        klines: List[Dict[str, Any]],
        period: int = 14
    ) -> Decimal:
        """
        计算ATR（Average True Range）
        
        Args:
            klines: K线数据列表
            period: ATR周期
            
        Returns:
            ATR值
        """
        if len(klines) < period + 1:
            return Decimal('0')
        
        # 计算True Range
        tr_list = []
        for i in range(1, len(klines)):
            high = Decimal(str(klines[i]['high']))
            low = Decimal(str(klines[i]['low']))
            prev_close = Decimal(str(klines[i-1]['close']))
            
            # TR = max(High - Low, |High - PrevClose|, |Low - PrevClose|)
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            
            tr = max(tr1, tr2, tr3)
            tr_list.append(tr)
        
        # 计算ATR（取最近period个TR的平均值）
        if len(tr_list) >= period:
            atr = sum(tr_list[-period:]) / period
            return atr
        else:
            return Decimal('0')
    
    def _execute_entry(
        self,
        symbol: str,
        current_price: float,
        current_time: datetime,
        atr: Decimal,
        score_result: Any
    ) -> None:
        """
        执行开仓
        
        Args:
            symbol: 交易对
            current_price: 当前价格
            current_time: 当前时间
            atr: ATR值
            score_result: 评分结果
        """
        logger.info(
            f"\n开仓信号: {symbol}",
            time=current_time.strftime('%Y-%m-%d %H:%M:%S'),
            price=current_price,
            score=score_result.total_score
        )
        
        # 计算仓位大小
        quantity = self.position_manager.calculate_position_size(
            balance=self.balance,
            current_price=current_price,
            atr=atr
        )
        
        if quantity <= 0:
            logger.warning(f"仓位计算失败: {symbol}")
            return
        
        # 计算止损价格
        stop_loss_price = self.position_manager.calculate_stop_loss(
            entry_price=Decimal(str(current_price)),
            atr=atr
        )
        
        # 计算止盈价格
        take_profit_prices = self.position_manager.calculate_take_profit(
            entry_price=Decimal(str(current_price)),
            atr=atr
        )
        
        # 执行开仓（模拟）
        entry_result = self.order_executor.execute_short(
            symbol=symbol,
            quantity=quantity,
            price=current_price
        )
        
        if entry_result:
            # 记录持仓
            self.position_manager.open_position(
                symbol=symbol,
                entry_price=current_price,
                entry_time=current_time,
                quantity=quantity,
                stop_loss_price=float(stop_loss_price),
                take_profit_prices=[float(p) for p in take_profit_prices],
                atr=float(atr)
            )
            
            # 添加到已交易币种列表
            self.traded_symbols.append(symbol)
            
            logger.info(
                f"开仓成功: {symbol}",
                quantity=float(quantity),
                entry_price=current_price,
                stop_loss=float(stop_loss_price),
                atr=float(atr)
            )
    
    def _check_position_management(
        self,
        symbol: str,
        current_price: float,
        current_time: datetime
    ) -> None:
        """
        检查持仓管理（止损止盈）
        
        Args:
            symbol: 交易对
            current_price: 当前价格
            current_time: 当前时间
        """
        position = self.position_manager.get_position(symbol)
        if not position:
            return
        
        # 检查止损
        if current_price >= position['stop_loss_price']:
            logger.info(f"\n触发止损: {symbol}, 价格={current_price}")
            self._execute_exit(
                symbol=symbol,
                exit_price=current_price,
                exit_time=current_time,
                exit_reason="止损",
                close_percent=1.0
            )
            return
        
        # 检查止盈（分批）
        take_profit_prices = position['take_profit_prices']
        remaining_quantity = position['remaining_quantity']
        
        # 从配置读取分批止盈和时间止损参数（V4.1：避免硬编码，对齐配置项）
        batch_tp_config = self.config.get('trading', {}).get('batch_take_profit', {})
        target1_close_percent = batch_tp_config.get('target1_close_percent', 0.30)
        target2_close_percent = batch_tp_config.get('target2_close_percent', 0.40)
        trailing_stop_atr_multiplier = batch_tp_config.get('trailing_stop_atr_multiplier', 1.5)
        max_holding_hours = self.config.get('trading', {}).get('time_stop', {}).get('max_holding_hours', 72)
        
        # 第一目标
        if not position['target1_reached'] and current_price <= take_profit_prices[0]:
            logger.info(f"\n触发第一目标止盈: {symbol}, 价格={current_price}")
            self._execute_exit(
                symbol=symbol,
                exit_price=current_price,
                exit_time=current_time,
                exit_reason="第一目标止盈",
                close_percent=target1_close_percent
            )
            self.position_manager.update_target_status(symbol, 1)
            return
        
        # 第二目标
        if not position['target2_reached'] and current_price <= take_profit_prices[1]:
            logger.info(f"\n触发第二目标止盈: {symbol}, 价格={current_price}")
            self._execute_exit(
                symbol=symbol,
                exit_price=current_price,
                exit_time=current_time,
                exit_reason="第二目标止盈",
                close_percent=target2_close_percent
            )
            self.position_manager.update_target_status(symbol, 2)
            return
        
        # 移动止盈
        if position['target2_reached']:
            # 更新最低价
            if current_price < position['lowest_price']:
                self.position_manager.update_lowest_price(symbol, current_price)
            else:
                # 检查是否触发移动止盈（从配置读取ATR倍数）
                price_bounce = current_price - position['lowest_price']
                trailing_stop_threshold = position['atr'] * trailing_stop_atr_multiplier
                
                if price_bounce >= trailing_stop_threshold:
                    logger.info(f"\n触发移动止盈: {symbol}, 价格={current_price}")
                    self._execute_exit(
                        symbol=symbol,
                        exit_price=current_price,
                        exit_time=current_time,
                        exit_reason="移动止盈",
                        close_percent=1.0
                    )
                    return
        
        # 时间止损（从配置读取最大持仓时长）
        holding_hours = (current_time - position['entry_time']).total_seconds() / 3600
        if holding_hours >= max_holding_hours and not position['target1_reached']:
            logger.info(f"\n触发时间止损: {symbol}, 持仓时长={holding_hours:.1f}小时")
            self._execute_exit(
                symbol=symbol,
                exit_price=current_price,
                exit_time=current_time,
                exit_reason="时间止损",
                close_percent=1.0
            )
            return
    
    def _execute_exit(
        self,
        symbol: str,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
        close_percent: float
    ) -> None:
        """
        执行平仓
        
        Args:
            symbol: 交易对
            exit_price: 平仓价格
            exit_time: 平仓时间
            exit_reason: 平仓原因
            close_percent: 平仓比例
        """
        position = self.position_manager.get_position(symbol)
        if not position:
            return
        
        # 计算平仓数量（统一转为Decimal避免类型混用）
        quantity = position['quantity'] * Decimal(str(close_percent))
        
        # 执行平仓（模拟）
        exit_result = self.order_executor.close_position(
            symbol=symbol,
            quantity=quantity,
            price=exit_price
        )
        
        if exit_result:
            # 计算盈亏
            pnl = self.order_executor.calculate_pnl(
                entry_price=position['entry_price'],
                exit_price=exit_price,
                quantity=quantity
            )
            
            # 更新账户余额
            self.balance += Decimal(str(pnl))
            
            # 记录交易
            trade = {
                'symbol': symbol,
                'entry_time': position['entry_time'],
                'entry_price': position['entry_price'],
                'exit_time': exit_time,
                'exit_price': exit_price,
                'quantity': quantity,
                'pnl': float(pnl),
                'pnl_percent': float(pnl / (Decimal(str(position['entry_price'])) * quantity)),
                'exit_reason': exit_reason,
                'score': position.get('score', 0),
                'holding_hours': (exit_time - position['entry_time']).total_seconds() / 3600
            }
            self.trades.append(trade)
            
            # 记录资金曲线
            self.equity_curve.append({
                'time': exit_time,
                'balance': float(self.balance)
            })
            
            # 更新连续亏损计数
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0
            
            # 关闭持仓
            if close_percent >= 1.0:
                self.position_manager.close_position(symbol)
            else:
                self.position_manager.update_remaining_quantity(symbol, quantity)
            
            logger.info(
                f"平仓成功: {symbol}",
                quantity=float(quantity),
                exit_price=exit_price,
                pnl=float(pnl),
                exit_reason=exit_reason
            )
