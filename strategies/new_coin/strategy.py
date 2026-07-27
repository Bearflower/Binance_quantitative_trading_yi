"""
新币做空策略主类
继承 BaseStrategy，实现新币做空的核心逻辑
"""
from typing import Dict, Any, Optional, List, Tuple
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import structlog

from shared.base_strategy import BaseStrategy
from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.database import DatabaseManager
from shared.condition_orders import get_open_orders

from .scoring_engine import ScoringEngine, ScoringResult
from .pattern import PatternRecognizer
from .detector import ListingDetector
from .executor import TradingExecutor


logger = structlog.get_logger()


# 预定义的跳过原因常量
SKIP_REASON_TOO_LONG = '上线时间过长'

class NewCoinStrategy(BaseStrategy):
    """新币做空策略

    针对新上市币种的做空策略，利用新币上市后的价格下跌趋势获利。

    核心逻辑：
    1. 监控新币上市信息
    2. 分析新币价格走势（形态识别）
    3. 评估做空机会（评分引擎）
    4. 执行做空交易
    5. 风险控制和止损
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化策略

        Args:
            config: 策略配置字典
        """
        super().__init__(config)

        # 策略名称
        self.strategy_name = config.get('strategy', {}).get('name', 'new_coin')

        # 初始化各模块（延迟到 initialize 方法）
        self.scoring_engine: Optional[ScoringEngine] = None
        self.pattern_recognizer: Optional[PatternRecognizer] = None
        self.listing_detector: Optional[ListingDetector] = None
        self.trading_executor: Optional[TradingExecutor] = None

        # 当前持仓
        self.positions: Dict[str, Dict[str, Any]] = {}

        # 一币一单黑名单（已交易币种）
        self.traded_symbols: List[str] = []

        # 连续亏损暂停机制
        self.consecutive_losses: int = 0
        self.pause_until: Optional[datetime] = None

        # 永久黑名单（反向暴涨币种）
        self.blacklist: List[str] = []

        # 止损监控列表（用于黑名单检测）
        self.stop_loss_monitor: Dict[str, Dict[str, Any]] = {}

        # 单日开仓限制
        self.daily_trade_count = 0
        self.last_trade_date = None  # UTC日期

        # 最大回撤熔断
        self.cumulative_pnl = Decimal('0')
        self.peak_pnl = None  # 历史最高累计盈亏（基于cumulative_pnl序列）
        self.drawdown_pause_until = None

        # K线服务已注册币种集合（避免重复注册）
        self._registered_symbols: set = set()

        # 无效币种缓存（-9999错误，避免重复请求已废弃/无效币种）
        self._invalid_symbols: set = set()

        # 重启后条件单补全标志（只补一次）
        self._replenish_done: bool = False

        # 检测间隔
        detector_config = config.get('detector', {})
        self.check_interval = detector_config.get('check_interval', 300)

        logger.info(
            "新币做空策略初始化",
            strategy_name=self.strategy_name,
            check_interval=self.check_interval
        )

    async def initialize(self) -> None:
        """
        初始化策略资源

        初始化：
        - 评分引擎
        - 形态识别器
        - 新币检测器
        - 交易执行器
        """
        logger.info("开始初始化新币做空策略资源")

        # 验证必要客户端已设置
        if not self.binance_client:
            raise ValueError("币安客户端未设置")
        if not self.kline_service:
            raise ValueError("K线服务未设置")
        if not self.notification_client:
            raise ValueError("通知客户端未设置")
        if not self.db:
            raise ValueError("数据库管理器未设置")

        # 初始化评分引擎
        self.scoring_engine = ScoringEngine(self.config)
        logger.info("评分引擎初始化完成")

        # 初始化形态识别器
        self.pattern_recognizer = PatternRecognizer(self.config)
        logger.info("形态识别器初始化完成")

        # 初始化新币检测器
        self.listing_detector = ListingDetector(
            binance_api=self.binance_client,
            db=self.db,
            config=self.config
        )
        logger.info("新币检测器初始化完成")

        # 初始化交易执行器
        self.trading_executor = TradingExecutor(
            binance_api=self.binance_client,
            db=self.db,
            notification=self.notification_client,
            config=self.config,
            kline_service=self.kline_service
        )
        logger.info("交易执行器初始化完成")

        # 恢复策略状态
        await self._restore_state()

        logger.info("新币做空策略资源初始化完成")

    async def analyze(self, symbol: str) -> Dict[str, Any]:
        """
        分析市场数据

        Args:
            symbol: 交易对

        Returns:
            分析结果字典，包含：
            - symbol: 交易对
            - score_result: 评分结果
            - patterns: 形态识别结果
            - market_data: 市场数据
        """
        logger.info(f"开始分析新币: {symbol}")

        try:
            # 获取币种信息
            coin_info = await self._get_coin_info(symbol)
            if not coin_info:
                logger.warning(f"获取币种信息失败: {symbol}")
                return {'symbol': symbol, 'error': '获取币种信息失败'}

            listing_hours = coin_info.get('listing_hours', 0)

            # 检查上线时间
            detector_config = self.config.get('detector', {})
            min_hours = detector_config.get('min_listing_hours', 1)
            max_hours = detector_config.get('max_listing_hours', 48)

            if listing_hours < min_hours:
                logger.info(f"上线时间过短: {listing_hours:.1f}小时 < {min_hours}小时")
                return {'symbol': symbol, 'skip': True, 'reason': '上线时间过短'}

            if listing_hours > max_hours:
                logger.info(f"上线时间过长: {listing_hours:.1f}小时 > {max_hours}小时")
                return {'symbol': symbol, 'skip': True, 'reason': '上线时间过长'}

            # 获取K线数据
            kline_config = self.config.get('kline', {})
            klines = await self.kline_service.get_klines(
                symbol=symbol,
                interval=kline_config.get('interval', '15m'),
                limit=kline_config.get('limit', 200)
            )

            min_klines = kline_config.get('min_klines_for_analysis', 14)
            if not klines or len(klines) < min_klines:
                logger.warning("K线数据不足", symbol=symbol, kline_count=len(klines) if klines else 0, min_required=min_klines)
                return {'symbol': symbol, 'error': 'K线数据不足'}

            # 形态识别
            patterns = self.pattern_recognizer.detect(klines)

            # 获取合约数据
            oi_usd = await self._get_open_interest(symbol)
            total_volume = await self._get_total_volume(symbol)
            funding_rate = await self._get_funding_rate(symbol)
            current_price = float(klines[-1].get('close', 0))

            # 计算最近N根K线的最高价（用于反弹放弃检查）
            rebound_config = self.config.get('scoring', {}).get('rebound_check', {})
            recent_high = 0.0
            if rebound_config.get('enabled', True):
                lookback = rebound_config.get('lookback_klines', 5)
                recent_klines = klines[-lookback:] if len(klines) >= lookback else klines
                if recent_klines:
                    recent_high = max(float(k.get('high', 0)) for k in recent_klines)

            # 计算OI变化率（A方案：多层fallback）
            oi_config = self.config.get('scoring', {}).get('sentiment', {}).get('oi_change', {})
            lookback_hours = oi_config.get('lookback_hours', 3)
            oi_change_rate = None  # None表示无法计算

            # 尝试1：配置的回溯时长（默认3小时）
            oi_hours_ago = await self._get_open_interest_ahead(symbol, hours_ago=lookback_hours)
            if oi_hours_ago > 0:
                oi_change_rate = (oi_usd - oi_hours_ago) / oi_hours_ago
            else:
                # 尝试2：1小时前OI（fallback）
                oi_1h = await self._get_open_interest_ahead(symbol, hours_ago=1)
                if oi_1h > 0:
                    oi_change_rate = (oi_usd - oi_1h) / oi_1h
                    logger.info(f"OI变化率使用1小时fallback: {symbol}")

                # 尝试3：上线以来最短窗口（在_get_open_interest_ahead的fallback中已实现）
                # 如果以上都失败，oi_change_rate保持None

            # C方案：判断是否使用降级模式
            degraded_config = self.config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {})
            degraded_enabled = degraded_config.get('enabled', True)
            degraded_threshold = degraded_config.get('listing_hours_threshold', 3)
            sentiment_degraded = degraded_enabled and (oi_change_rate is None or listing_hours < degraded_threshold)

            if oi_change_rate is None:
                oi_change_rate = 0.0  # 传给scoring_engine时用0，但通过degraded标志区分

            # 获取最近新币OI（用于排名）
            recent_coins_oi = await self.listing_detector.get_recent_coins_oi()

            # 执行评分
            score_result = self.scoring_engine.score(
                symbol=symbol,
                oi_usd=oi_usd,
                total_volume_usd=total_volume,
                funding_rate=funding_rate,
                oi_change_rate=oi_change_rate,
                sentiment_degraded=sentiment_degraded,
                three_tops_detected=patterns['three_tops'][0],
                three_tops_score=patterns['three_tops'][1],
                long_upper_shadow=patterns['long_upper_shadow'][0],
                long_upper_shadow_score=patterns['long_upper_shadow'][1],
                volume_divergence=patterns['volume_divergence'][0],
                volume_divergence_score=patterns['volume_divergence'][1],
                listing_hours=listing_hours,
                current_price=current_price,
                recent_coins_oi=recent_coins_oi
            )

            # 构建分析结果
            result = {
                'symbol': symbol,
                'score_result': score_result.to_dict(),
                'patterns': patterns,
                'market_data': {
                    'listing_hours': listing_hours,
                    'current_price': current_price,
                    'oi_usd': oi_usd,
                    'total_volume': total_volume,
                    'funding_rate': funding_rate,
                    'recent_high': recent_high
                }
            }

            logger.info(
                f"分析完成: {symbol}",
                total_score=score_result.total_score,
                veto=score_result.veto
            )

            return result

        except Exception as e:
            logger.error(
                f"分析失败: {symbol}",
                error=str(e),
                exc_info=True
            )
            return {'symbol': symbol, 'error': str(e)}

    async def execute_signal(self, signal: Dict[str, Any]) -> Tuple[bool, str]:
        """
        执行交易信号

        Args:
            signal: 交易信号字典，包含：
                - symbol: 交易对
                - score_result: 评分结果
                - action: 交易动作

        Returns:
            (是否成功, 失败原因)
        """
        try:
            symbol = signal.get('symbol')
            score_result_dict = signal.get('score_result', {})
            action = signal.get('action', 'SHORT')

            logger.info(
                f"执行交易信号: {symbol}",
                action=action,
                score=score_result_dict.get('total_score')
            )

            # 1. 检查连续亏损暂停
            if await self._check_pause_status():
                logger.warning("策略暂停中，跳过交易")
                return False, "策略连续亏损暂停中"

            # 2. 检查一币一单黑名单
            if symbol in self.traded_symbols:
                logger.warning(f"币种已在黑名单中（已交易过）: {symbol}")
                return False, "该币种已交易过，不可重复开仓"

            # 3. 检查永久黑名单
            if symbol in self.blacklist:
                logger.warning(f"币种在永久黑名单中: {symbol}")
                return False, "币种在永久黑名单中"

            # 4. 检查持仓数量限制
            if len(self.positions) >= self.trading_executor.get_max_positions():
                logger.info(
                    f"持仓数量已达上限: {len(self.positions)} >= {self.trading_executor.get_max_positions()}"
                )
                return False, f"持仓数量已达上限({len(self.positions)}/{self.trading_executor.get_max_positions()})"

            # 5. 检查单日开仓限制
            daily_limit = self.config.get('trading', {}).get('daily_trade_limit', 2)
            today = datetime.now(timezone.utc).date()
            if self.last_trade_date != today:
                self.daily_trade_count = 0
                self.last_trade_date = today
            if self.daily_trade_count >= daily_limit:
                logger.warning(f"今日开仓已达上限: {self.daily_trade_count}/{daily_limit}")
                return False, f"今日开仓已达上限({self.daily_trade_count}/{daily_limit})"

            # 获取当前价格
            market_data = signal.get('market_data', {})
            current_price = market_data.get('current_price', 0)

            if current_price <= 0:
                logger.error(f"当前价格无效: {current_price}")
                return False, "当前价格无效"

            # 执行做空
            order = await self.trading_executor.execute_short(
                symbol=symbol,
                score_result=score_result_dict,
                current_price=current_price
            )

            if order:
                # 记录持仓
                self.positions[symbol] = {
                    'order_id': order.get('orderId'),
                    'entry_price': current_price,
                    'entry_time': datetime.now().isoformat(),
                    'score': score_result_dict.get('total_score')
                }

                # 添加到已交易币种列表
                if symbol not in self.traded_symbols:
                    self.traded_symbols.append(symbol)

                # 更新单日开仓计数
                self.daily_trade_count += 1

                # 保存状态
                await self._save_state()

                logger.info(f"交易信号执行成功: {symbol}")
                return True, ""
            else:
                logger.error(f"交易信号执行失败: {symbol}")
                return False, "做空限价单未成交或失败"

        except Exception as e:
            logger.error(
                f"执行交易信号失败",
                error=str(e),
                exc_info=True
            )
            return False, f"执行异常: {str(e)}"

    async def run(self) -> None:
        """
        运行策略

        主循环逻辑：
        1. 检测新币
        2. 分析新币
        3. 执行交易
        4. 监控持仓
        """
        logger.info("新币做空策略开始运行")
        self._running = True

        try:
            # 对齐到下一个整点时刻（确保每次评分在K线收盘后立即执行）
            now = datetime.now()
            next_hour = now.replace(minute=0, second=0, microsecond=0)
            # 当前分钟>1时，跳到下一个整点（留1分钟缓冲让K线服务采集最新数据）
            if now.minute >= 1:
                # timedelta 已在文件顶部导入
                next_hour += timedelta(hours=1)
            wait_seconds = (next_hour - now).total_seconds()
            if wait_seconds > 0:
                logger.info(f"对齐整点周期，等待 {wait_seconds:.0f} 秒到 {next_hour.strftime('%H:%M')}")
                await asyncio.sleep(wait_seconds)

            # 等待K线服务采集最新数据（约需60秒，在HH:01左右完成）
            kline_wait = self.config.get('kline', {}).get('data_delay_seconds', 90)
            logger.info(f"等待K线服务采集最新数据（{kline_wait}秒缓冲）...")
            await asyncio.sleep(kline_wait)

            while self._running:
                try:
                    await self._execute_cycle()
                    await asyncio.sleep(self.check_interval)
                except Exception as e:
                    logger.error(f"执行周期异常: {e}", exc_info=True)
                    await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"策略运行异常: {e}", exc_info=True)
            raise

    async def stop(self) -> None:
        """停止策略"""
        logger.info("停止新币做空策略")
        self._running = False

        # 保存状态
        await self._save_state()

        # 清理资源
        await self.cleanup()

        logger.info("新币做空策略已停止")

    async def _execute_cycle(self) -> None:
        """
        执行一个周期
        
        流程：
        1. 检查黑名单监控
        2. 检测新币
        3. 分析新币
        4. 执行交易
        5. 监控持仓
        """
        logger.info(f"开始执行周期: {datetime.now()}")

        # 重启后首次执行：为现有持仓补全条件单（止损止盈）
        if not self._replenish_done:
            # 优先从数据库状态获取持仓，如为空则从交易所同步
            positions_to_replenish = dict(self.positions) if self.positions else None
            sync_failed = False
            
            if not positions_to_replenish:
                # 从交易所查询实际持仓
                logger.info("数据库状态无持仓，尝试从交易所同步持仓...")
                try:
                    exchange_positions = await self.binance_client._request(
                        "GET", "/papi/v1/um/positionRisk", signed=True
                    )
                    short_positions = [
                        p for p in exchange_positions
                        if float(p.get('positionAmt', 0)) < 0
                    ]
                    if short_positions:
                        positions_to_replenish = {}
                        for p in short_positions:
                            symbol = p['symbol']
                            entry_price = float(p.get('entryPrice', 0))
                            positions_to_replenish[symbol] = {
                                'entry_price': entry_price,
                                'entry_time': datetime.now(timezone.utc).isoformat()
                            }
                        logger.info(
                            f"从交易所同步到 {len(short_positions)} 个持仓",
                            symbols=list(positions_to_replenish.keys())
                        )
                    else:
                        logger.info("交易所无做空持仓")
                except Exception as e:
                    logger.error(f"从交易所同步持仓失败: {e}")
                    sync_failed = True
            
            if positions_to_replenish:
                logger.info(
                    f"检测到 {len(positions_to_replenish)} 个持仓需要补全条件单",
                    symbols=list(positions_to_replenish.keys())
                )
                
                # 同步到 self.positions，确保 _monitor_positions 能正常跟踪
                if not self.positions:
                    self.positions = positions_to_replenish
                
                all_success = True
                for symbol, pos in positions_to_replenish.items():
                    entry_price = pos.get('entry_price', 0)
                    if entry_price > 0:
                        result = await self.trading_executor.replenish_conditional_orders(
                            symbol=symbol,
                            entry_price=Decimal(str(entry_price))
                        )
                        if not result:
                            all_success = False
                            logger.warning(
                                f"补全条件单失败，下周期将重试: {symbol}"
                            )
                    else:
                        logger.warning(
                            f"持仓入场价格无效，跳过补全条件单: {symbol}",
                            entry_price=entry_price
                        )
                
                # 仅所有操作成功时才标记完成，否则下周期重试
                if all_success and not sync_failed:
                    self._replenish_done = True
                    logger.info("条件单补全检查完成")
                else:
                    logger.warning(
                        "条件单补全部分失败，下周期将重试",
                        all_success=all_success,
                        sync_failed=sync_failed
                    )
            else:
                # 无持仓或同步失败
                if not sync_failed:
                    self._replenish_done = True
                    logger.debug("无持仓，跳过条件单补全")
                # 同步失败时保留标志为 False，下周期重试

        # 保留无效币种缓存（不清空）：-4108（交割/结算中）和-9999（未知币种）均为永久状态
        # 已交割/结算的币种不会重新上线，新币种名称不同不会出现在缓存中
        # 保持缓存持久化可避免每周期重复查询产生-4108警告日志

        # 1. 检查黑名单监控（检测反向暴涨）
        await self._check_blacklist_monitor()

        # 刷新回撤熔断状态（基于cumulative_pnl序列，捕获平仓间隔内的回撤变化）
        await self._refresh_drawdown_status()

        # 检查回撤熔断状态
        if self.drawdown_pause_until:
            if datetime.now(timezone.utc) < self.drawdown_pause_until:
                logger.warning(f"回撤熔断中，暂停至 {self.drawdown_pause_until}")
                return
            else:
                logger.info("回撤熔断期已结束，恢复交易")
                self.drawdown_pause_until = None

        # 2. 检测新币
        new_coins = await self.listing_detector.detect_new_listings()

        if not new_coins:
            logger.debug("未检测到新币")
            return

        logger.info(f"检测到 {len(new_coins)} 个新币")

        # 2.5 向K线服务注册新币种（自动创建数据表）
        kline_interval = self.config.get('kline', {}).get('interval', '1h')
        for coin in new_coins:
            try:
                symbol = coin['symbol']
                if symbol not in self._registered_symbols:
                    registered = await self.kline_service.register_symbol(symbol, intervals=[kline_interval])
                    if registered:
                        logger.info(f"已向K线服务注册新币种: {symbol}")
                        self._registered_symbols.add(symbol)
                    else:
                        logger.warning(f"K线服务注册失败: {symbol}，将在下次周期重试")
                else:
                    logger.debug(f"币种已注册，跳过: {symbol}")
            except (ConnectionError, TimeoutError, Exception) as e:
                logger.error(f"K线服务注册异常: {symbol}: {e}")

        # 3. 分析每个新币
        cycle_results: list = []  # 记录本周期所有币种的评分结果
        for coin in new_coins:
            try:
                symbol = coin['symbol']
                logger.info(f"分析新币: {symbol}")

                # 分析市场
                analysis = await self.analyze(symbol)

                # 检查是否跳过
                if analysis.get('skip'):
                    reason = analysis.get('reason', '')
                    logger.info(f"跳过新币: {symbol}, 原因: {reason}")

                    # 如果上线时间过长（超48h），自动加入已知列表并注销K线
                    if reason == SKIP_REASON_TOO_LONG:
                        self.listing_detector.known_symbols.add(symbol)
                        await self.listing_detector._save_known_symbols()
                        # 从K线服务注销，停止采集数据
                        try:
                            await self.kline_service.unregister_symbol(symbol)
                            logger.info(f"已从K线服务注销过期币种: {symbol}")
                        except Exception as e:
                            logger.warning(f"K线服务注销失败: {symbol}: {e}")
                        continue  # 不加入cycle_results，不在通知中显示

                    # 其他skip原因（如上线时间过短）仍加入cycle_results
                    result_item = {
                        'symbol': symbol,
                        'total_score': 0,
                        'contract_score': 0,
                        'technical_score': 0,
                        'sentiment_score': 0,
                        'veto': False,
                        'veto_reason': '',
                        'should_entry': False,
                        'skipped': True,
                        'skip_reason': reason,
                    }
                    cycle_results.append(result_item)
                    continue

                # 检查是否有错误
                if analysis.get('error'):
                    err_msg = analysis.get('error')
                    if 'K线数据不足' in err_msg:
                        logger.warning(f"跳过评分: {symbol}, 原因: {err_msg}")
                    else:
                        logger.error(f"分析失败: {symbol}, 错误: {err_msg}")
                    result_item = {
                        'symbol': symbol,
                        'total_score': 0,
                        'contract_score': 0,
                        'technical_score': 0,
                        'sentiment_score': 0,
                        'veto': False,
                        'veto_reason': '',
                        'should_entry': False,
                        'error': True,
                        'error_reason': analysis.get('error', '未知'),
                    }
                    cycle_results.append(result_item)
                    continue

                # 获取评分结果
                score_result_dict = analysis.get('score_result', {})
                score_result = ScoringResult(
                    symbol=score_result_dict['symbol'],
                    total_score=score_result_dict['total_score'],
                    contract_score=score_result_dict['contract_score'],
                    technical_score=score_result_dict['technical_score'],
                    sentiment_score=score_result_dict['sentiment_score'],
                    veto=score_result_dict['veto'],
                    veto_reason=score_result_dict['veto_reason'],
                    details=score_result_dict['details']
                )

                # 检查是否应该入场
                patterns = analysis.get('patterns', {})
                three_tops_score = patterns.get('three_tops', (False, 0.0))[1]
                total_technical_score = (
                    patterns.get('three_tops', (False, 0.0))[1] +
                    patterns.get('long_upper_shadow', (False, 0.0))[1] +
                    patterns.get('volume_divergence', (False, 0.0))[1]
                )

                should_entry = self.scoring_engine.should_entry(
                    score_result,
                    three_tops_score,
                    total_technical_score
                )

                # 反弹放弃检查：价格反弹超过最近高点则放弃入场
                rebound_abandoned = False
                rebound_config = self.config.get('scoring', {}).get('rebound_check', {})
                if should_entry and rebound_config.get('enabled', True):
                    lookback = rebound_config.get('lookback_klines', 5)
                    market_data = analysis.get('market_data', {})
                    recent_high = market_data.get('recent_high', 0)
                    current_price = market_data.get('current_price', 0)
                    if current_price > 0 and recent_high > 0 and current_price > recent_high:
                        should_entry = False
                        rebound_abandoned = True
                        logger.info(
                            f"反弹放弃: {symbol}",
                            current_price=current_price,
                            recent_high=recent_high,
                            lookback=lookback
                        )

                # 记录本轮评分结果
                result_item = {
                    'symbol': symbol,
                    'total_score': score_result_dict['total_score'],
                    'contract_score': score_result_dict['contract_score'],
                    'technical_score': score_result_dict['technical_score'],
                    'sentiment_score': score_result_dict['sentiment_score'],
                    'veto': score_result_dict['veto'],
                    'veto_reason': score_result_dict.get('veto_reason', ''),
                    'should_entry': should_entry,
                    'rebound_abandoned': rebound_abandoned,
                }
                cycle_results.append(result_item)

                if should_entry:
                    # 执行交易
                    signal = {
                        'symbol': symbol,
                        'score_result': score_result_dict,
                        'action': 'SHORT',
                        'market_data': analysis.get('market_data', {})
                    }

                    entry_success, fail_reason = await self.execute_signal(signal)
                    result_item['entry_success'] = entry_success
                    result_item['entry_fail_reason'] = fail_reason

                    if entry_success:
                        # 入场成功后立即停止对该币种的监控
                        self.listing_detector.known_symbols.add(symbol)
                        await self.listing_detector._save_known_symbols()
                        try:
                            await self.kline_service.unregister_symbol(symbol)
                            logger.info(f"入场成功，已停止监控: {symbol}")
                        except Exception as e:
                            logger.warning(f"K线服务注销失败: {symbol}: {e}")

            except Exception as e:
                logger.error(
                    f"处理新币失败: {coin.get('symbol')}",
                    error=str(e),
                    exc_info=True
                )

        # 3.5 发送本周期评分汇总通知
        if cycle_results:
            # 构建通知消息
            summary_lines = []
            summary_lines.append("【新币评分周期汇总】")
            summary_lines.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            summary_lines.append(f"检测币种数: {len(new_coins)}，实际评分: {len(cycle_results)}")
            summary_lines.append("")

            entries = []  # 有入场信号的币种
            for result in cycle_results:
                # 跳过和错误优先处理
                if result.get('skipped'):
                    status = f"跳过: {result.get('skip_reason', '')}"
                    icon = '⏭️'
                    line = f"{icon} {result['symbol']} [{status}]"
                    summary_lines.append(line)
                    continue
                if result.get('error'):
                    status = f"错误: {result.get('error_reason', '')}"
                    icon = '❌'
                    line = f"{icon} {result['symbol']} [{status}]"
                    summary_lines.append(line)
                    continue

                if result['veto']:
                    status = f"否决: {result['veto_reason']}" if result['veto_reason'] else "否决"
                elif result.get('rebound_abandoned'):
                    status = "反弹放弃"
                elif result['should_entry']:
                    if result.get('entry_success') is True:
                        status = "已入场(已停止监控)"
                    elif result.get('entry_success') is False:
                        fail_reason = result.get('entry_fail_reason', '')
                        status = f"入场失败: {fail_reason}" if fail_reason else "入场失败"
                    else:
                        status = "已入场"
                else:
                    status = "监控中"

                line = (
                    f"{'🟢' if result['should_entry'] else '⚪'} {result['symbol']} "
                    f"总分{result['total_score']:.1f} "
                    f"(合约{result['contract_score']:.1f} 技术{result['technical_score']:.1f} 情绪{result['sentiment_score']:.1f}) "
                    f"[{status}]"
                )
                summary_lines.append(line)
                if result['should_entry']:
                    entries.append(result['symbol'])

            if not entries:
                summary_lines.append("")
                summary_lines.append("本轮无开仓信号")

            message = "\n".join(summary_lines)

            try:
                await self.notification_client.send(
                    message=message,
                    level="info",
                    project="new_coin"
                )
            except Exception as e:
                logger.warning(f"发送评分汇总通知失败: {e}")

        # 4. 监控现有持仓
        await self._monitor_positions()

        # 5. 检查是否需要发送周报（周一0点UTC，防重复：记录上次发送日期）
        now = datetime.now(timezone.utc)
        if now.weekday() == 0 and now.hour == 0:
            last_review_date = getattr(self, '_last_weekly_review_date', None)
            today_str = now.strftime('%Y-%m-%d')
            if last_review_date != today_str:
                await self._weekly_review()
                self._last_weekly_review_date = today_str

    async def _weekly_review(self) -> None:
        """
        每周自动复盘统计

        统计内容：
        - 本周交易笔数
        - 胜率
        - 盈亏比
        - 净盈亏

        触发时机：周一0点UTC（在_execute_cycle中调用）
        """
        try:
            now = datetime.now(timezone.utc)
            # 本周一0点UTC
            week_start = now - timedelta(days=now.weekday())
            week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

            # 从配置读取策略名称（用于数据库查询）
            strategy_name = self.config.get('strategy', {}).get('db_strategy_name', '新币做空策略')

            # 从数据库获取本周交易记录
            records = await self.db.fetch_all(
                """
                SELECT realized_pnl, executed_at 
                FROM trading.trade_records 
                WHERE strategy = $1 
                AND executed_at >= $2
                AND realized_pnl IS NOT NULL
                ORDER BY executed_at
                """,
                strategy_name,
                week_start.replace(tzinfo=None)  # 传入数据库需要去掉时区标记
            )

            if not records:
                logger.info("本周无交易记录，跳过复盘")
                return

            total_trades = len(records)
            wins = [r for r in records if float(r['realized_pnl']) > 0]
            losses = [r for r in records if float(r['realized_pnl']) <= 0]
            win_count = len(wins)
            loss_count = len(losses)
            win_rate = win_count / total_trades if total_trades > 0 else 0

            total_profit = sum(float(r['realized_pnl']) for r in wins)
            total_loss = abs(sum(float(r['realized_pnl']) for r in losses))
            profit_ratio = total_profit / total_loss if total_loss > 0 else float('inf')

            # 发送周报通知
            message = (
                f"【新币做空策略周报】\n"
                f"周期: {week_start.strftime('%m/%d')} - {now.strftime('%m/%d')}\n"
                f"交易笔数: {total_trades}\n"
                f"胜率: {win_rate:.0%} ({win_count}胜/{loss_count}负)\n"
                f"盈亏比: {profit_ratio:.2f}\n"
                f"总盈利: {total_profit:.2f} USDT\n"
                f"总亏损: {total_loss:.2f} USDT\n"
                f"净盈亏: {total_profit - total_loss:.2f} USDT"
            )

            try:
                await self.notification_client.send(
                    message=message,
                    level="info",
                    project="new_coin"
                )
            except Exception as e:
                logger.warning(f"发送周报通知失败: {e}")

            logger.info(f"周报复盘完成: {total_trades}笔交易, 胜率{win_rate:.0%}")

        except Exception as e:
            logger.error(f"周报复盘失败: {e}")

    async def _monitor_positions(self) -> None:
        """
        监控现有持仓
        
        功能：
        1. 检查持仓状态
        2. 调用交易执行器的持仓管理（移动止盈、时间止损）
        3. 完全平仓后取消孤儿条件单（止盈止损单）
        4. 清理持仓跟踪记录
        5. 记录平仓结果（盈利/亏损）
        6. 更新连续亏损计数
        7. 触发止损监控（用于黑名单检测）
        """
        logger.debug(f"监控 {len(self.positions)} 个持仓")

        for symbol, position in list(self.positions.items()):
            try:
                # 调用交易执行器的持仓管理（移动止盈、时间止损）
                await self.trading_executor.check_position_management(symbol)
                
                # 获取当前持仓状态
                positions = await self.binance_client.get_position(symbol)

                # 查找做空持仓
                short_position = None
                for pos in positions:
                    if pos.get('positionSide') == 'SHORT':
                        short_position = pos
                        break

                if not short_position or float(short_position.get('positionAmt', 0)) == 0:
                    # 持仓已平仓
                    logger.info(f"持仓已平仓: {symbol}")
                    
                    # 取消孤儿条件单（止盈止损单），防止后续价格波动触发非预期交易
                    cancel_result = await self.trading_executor.cancel_all_algo_orders(symbol)
                    if cancel_result['failed'] > 0:
                        logger.warning(
                            "部分孤儿条件单取消失败",
                            symbol=symbol,
                            failed=cancel_result['failed'],
                        )
                    
                    # 清理持仓跟踪（幂等）
                    if symbol in self.trading_executor.position_tracking:
                        del self.trading_executor.position_tracking[symbol]
                    
                    # 计算盈亏
                    entry_price = position.get('entry_price', 0)
                    entry_time = position.get('entry_time')
                    
                    # 获取平仓价格（从最近的交易记录）
                    pnl = await self._get_position_pnl(symbol, entry_price)

                    # 查不到平仓记录时跳过回撤检查与连续亏损计数，避免污染统计
                    if pnl is None:
                        logger.warning(
                            "未获取到平仓盈亏，跳过回撤检查与连续亏损计数",
                            symbol=symbol,
                            entry_price=entry_price
                        )
                        # 删除持仓记录
                        del self.positions[symbol]
                        await self._save_state()
                        continue

                    # 回写平仓盈亏到交易记录（用于周报复盘统计）
                    # 注意：回写失败不影响主流程；positions 中的 order_id 是开仓单，
                    # 平仓单 order_id 需从 trade_records 表按 side='BUY' 查询
                    try:
                        entry_time_obj = None
                        if entry_time:
                            try:
                                entry_time_obj = datetime.fromisoformat(entry_time)
                            except (ValueError, TypeError):
                                entry_time_obj = None

                        if entry_time_obj:
                            # 查询本笔平仓订单的 order_id（做空平仓是 BUY 单）
                            close_order = await self.db.fetch_one(
                                """
                                SELECT order_id
                                FROM trading.trade_records
                                WHERE strategy = $1 AND symbol = $2 AND side = 'BUY'
                                AND executed_at >= $3
                                ORDER BY executed_at DESC
                                LIMIT 1
                                """,
                                self.config.get('strategy', {}).get('db_strategy_name', '新币做空策略'),
                                symbol,
                                entry_time_obj
                            )

                            if close_order and close_order.get('order_id'):
                                trade_logger = getattr(self.binance_client, 'trade_logger', None)
                                if trade_logger:
                                    await trade_logger.update_realized_pnl(
                                        order_id=close_order['order_id'],
                                        realized_pnl=Decimal(str(pnl))
                                    )
                            else:
                                logger.warning(
                                    "未找到平仓订单记录，无法回写盈亏",
                                    symbol=symbol
                                )
                        else:
                            logger.warning(
                                "entry_time 缺失或解析失败，跳过盈亏回写",
                                symbol=symbol,
                                entry_time=entry_time
                            )
                    except Exception as e:
                        logger.warning(
                            "回写平仓盈亏失败",
                            symbol=symbol,
                            error=str(e)
                        )

                    # 检查最大回撤
                    await self._check_max_drawdown(pnl)

                    # 更新连续亏损计数
                    if pnl < 0:
                        self.consecutive_losses += 1
                        logger.warning(
                            f"连续亏损计数: {self.consecutive_losses}",
                            symbol=symbol,
                            pnl=pnl
                        )
                        
                        # 检查是否触发暂停
                        await self._check_consecutive_loss_pause()
                        
                        # 添加到止损监控列表（用于黑名单检测）
                        await self._add_to_stop_loss_monitor(
                            symbol=symbol,
                            entry_price=entry_price,
                            entry_time=entry_time
                        )
                    else:
                        # 盈利则重置连续亏损计数
                        self.consecutive_losses = 0
                        logger.info(
                            f"盈利，重置连续亏损计数",
                            symbol=symbol,
                            pnl=pnl
                        )
                    
                    # 删除持仓记录
                    del self.positions[symbol]
                    await self._save_state()
                    continue

                # TODO: 实现动态止损止盈调整

            except Exception as e:
                logger.error(
                    f"监控持仓失败: {symbol}",
                    error=str(e)
                )

    async def _get_coin_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取币种信息"""
        try:
            # 从交易所信息获取
            exchange_info = await self.binance_client._request(
                "GET",
                "/fapi/v1/exchangeInfo",
                signed=False
            )

            for s in exchange_info.get('symbols', []):
                if s['symbol'] == symbol:
                    listing_time = s.get('onboardDate', 0)
                    if listing_time:
                        listing_datetime = datetime.fromtimestamp(listing_time / 1000)
                    else:
                        listing_datetime = datetime.now()

                    listing_hours = (datetime.now() - listing_datetime).total_seconds() / 3600

                    return {
                        'symbol': symbol,
                        'base_asset': s.get('baseAsset', ''),
                        'quote_asset': s.get('quoteAsset', ''),
                        'listing_time': listing_datetime,
                        'listing_hours': listing_hours,
                        'status': s.get('status', '')
                    }

            return None

        except Exception as e:
            logger.error(f"获取币种信息失败: {symbol}, 错误: {e}")
            return None

    async def _get_open_interest(self, symbol: str) -> float:
        """获取持仓量（无效币种会自动缓存跳过）"""
        if symbol in self._invalid_symbols:
            return 0.0
        try:
            data = await self.binance_client._request(
                "GET",
                "/fapi/v1/openInterest",
                params={'symbol': symbol},
                signed=False
            )
            oi = float(data.get('openInterest', 0))
            logger.debug(f"获取OI: {symbol} = {oi}")
            return oi
        except Exception as e:
            err_msg = str(e)
            if '-9999' in err_msg or '-4108' in err_msg:
                self._invalid_symbols.add(symbol)
                logger.info(f"币种无效（无法获取OI），加入跳过列表: {symbol}")
            else:
                logger.error(f"获取OI失败: {symbol}, 错误: {e}")
            return 0.0

    async def _get_open_interest_ahead(self, symbol: str, hours_ago: int = 3) -> float:
        """
        获取N小时前的OI数据（通过币安OI历史K线接口）（无效币种会自动缓存跳过）

        GET /futures/data/openInterestHist

        Args:
            symbol: 交易对
            hours_ago: 回溯小时数

        Returns:
            N小时前的OI（美元），如果数据不足返回0
        """
        if symbol in self._invalid_symbols:
            return 0.0
        try:
            # datetime, timezone, timedelta 已在文件顶部导入
            now = datetime.now(timezone.utc)
            end_time = int(now.timestamp() * 1000)
            start_time = int((now - timedelta(hours=hours_ago + 1)).timestamp() * 1000)

            data = await self.binance_client._request(
                "GET",
                "/futures/data/openInterestHist",
                params={
                    'symbol': symbol,
                    'period': '5m',
                    'startTime': start_time,
                    'endTime': end_time,
                    'limit': 100
                },
                signed=False
            )
            if data and len(data) > 0:
                # 取第一个数据点（最接近hours_ago时刻的OI值）
                oi_value = float(data[0].get('sumOpenInterest',
                          data[0].get('sumOpenInterestValue', 0)))
                logger.debug(f"获取{hours_ago}h前OI: {symbol} = {oi_value}")
                return oi_value

            # fallback：逐步减少回溯时长，适用于新币上线时间较短导致数据不足的情况
            if hours_ago > 1:
                for fallback_hours in range(hours_ago - 1, 0, -1):
                    try:
                        fallback_start = int((now - timedelta(hours=fallback_hours + 1)).timestamp() * 1000)
                        fallback_end = int((now - timedelta(hours=max(fallback_hours - 1, 0))).timestamp() * 1000)
                        fallback_data = await self.binance_client._request(
                            "GET",
                            "/futures/data/openInterestHist",
                            params={
                                'symbol': symbol,
                                'period': '5m',
                                'startTime': fallback_start,
                                'endTime': fallback_end,
                                'limit': 100
                            },
                            signed=False
                        )
                        if fallback_data and len(fallback_data) > 0:
                            oi_value = float(fallback_data[0].get('sumOpenInterest',
                                      fallback_data[0].get('sumOpenInterestValue', 0)))
                            if oi_value > 0:
                                logger.info(
                                    f"OI fallback成功: {symbol}",
                                    fallback_hours=fallback_hours
                                )
                                return oi_value
                    except Exception:
                        continue

            return 0.0
        except Exception as e:
            err_msg = str(e)
            if '-9999' in err_msg or '-4108' in err_msg:
                self._invalid_symbols.add(symbol)
                logger.info(f"币种无效（无法获取历史OI），加入跳过列表: {symbol}")
            else:
                logger.warning(f"获取{hours_ago}h前OI失败: {symbol}, {e}")
            return 0.0

    async def _get_total_volume(self, symbol: str) -> float:
        """
        获取币种上线以来的总交易量（USD）（无效币种会自动缓存跳过）

        改进：不再限制100根K线，通过币安API分页获取所有历史K线，
        使用 startTime/endTime 参数实现分页，每次最多1500根。

        Args:
            symbol: 交易对

        Returns:
            总交易量（USD）
        """
        if symbol in self._invalid_symbols:
            return 0.0
        try:
            # 从配置读取分页参数
            volume_config = self.config.get('kline', {}).get('total_volume', {})
            max_total_klines = volume_config.get('max_total_klines', 500)
            klines_per_request = volume_config.get('klines_per_request', 1500)
            interval = volume_config.get('interval', '1h')  # 默认1h周期减少请求次数

            total_volume = 0.0
            fetched = 0
            start_time = None  # 首次请求不设起始时间，从最早数据开始

            while fetched < max_total_klines:
                remaining = max_total_klines - fetched
                limit = min(klines_per_request, remaining)

                # 构建请求参数
                params = {
                    'symbol': symbol,
                    'interval': interval,
                    'limit': limit
                }
                if start_time is not None:
                    params['startTime'] = start_time

                # 直接调用币安API获取K线（绕过K线服务100根限制）
                data = await self.binance_client._request(
                    "GET",
                    "/fapi/v1/klines",
                    params=params,
                    signed=False
                )

                if not data or not isinstance(data, list):
                    break

                # 累加交易量
                for kline in data:
                    if len(kline) >= 12:
                        total_volume += float(kline[7])  # kline[7] = quote_volume

                fetched += len(data)

                # 如果返回的K线数量少于请求数量，说明已到末尾
                if len(data) < limit:
                    break

                # 下一页起始时间 = 最后一根K线的 close_time + 1ms
                last_close_time = data[-1][6]  # kline[6] = close_time
                start_time = last_close_time + 1

            logger.debug(
                f"获取总交易量: {symbol}",
                kline_count=fetched,
                total_volume=total_volume
            )

            return total_volume

        except Exception as e:
            err_msg = str(e)
            if '-9999' in err_msg or '-4108' in err_msg:
                self._invalid_symbols.add(symbol)
                logger.info(f"币种无效（无法获取总交易量），加入跳过列表: {symbol}")
            else:
                logger.error(f"获取总交易量失败: {symbol}, 错误: {e}")
            return 0.0

    async def _get_funding_rate(self, symbol: str) -> float:
        """获取资金费率（无效币种会自动缓存跳过）"""
        if symbol in self._invalid_symbols:
            return 0.0
        try:
            data = await self.binance_client._request(
                "GET",
                "/fapi/v1/fundingRate",
                params={'symbol': symbol, 'limit': 1},
                signed=False
            )
            if data and len(data) > 0:
                rate = float(data[0].get('fundingRate', 0))
                logger.debug(f"获取资金费率: {symbol} = {rate}")
                return rate
            return 0.0
        except Exception as e:
            err_msg = str(e)
            if '-9999' in err_msg or '-4108' in err_msg:
                self._invalid_symbols.add(symbol)
                logger.info(f"币种无效（无法获取资金费率），加入跳过列表: {symbol}")
            else:
                logger.error(f"获取资金费率失败: {symbol}, 错误: {e}")
            return 0.0

    async def _restore_state(self) -> None:
        """
        恢复策略状态
        
        恢复内容：
        1. 持仓信息
        2. 已交易币种列表
        3. 连续亏损计数
        4. 暂停状态
        5. 永久黑名单
        6. 止损监控列表
        """
        try:
            state = await self.db.fetch_one(
                "SELECT state_data FROM strategy_states WHERE strategy_name = $1 AND state_key = $2",
                self.strategy_name,
                'main'
            )

            if state:
                # 恢复持仓
                self.positions = state.get('positions', {})
                
                # 恢复已交易币种列表
                self.traded_symbols = state.get('traded_symbols', [])
                
                # 恢复连续亏损计数
                self.consecutive_losses = state.get('consecutive_losses', 0)
                
                # 恢复暂停状态
                pause_until_str = state.get('pause_until')
                if pause_until_str:
                    self.pause_until = datetime.fromisoformat(pause_until_str)
                
                # 恢复永久黑名单
                self.blacklist = state.get('blacklist', [])
                
                # 恢复止损监控列表
                self.stop_loss_monitor = state.get('stop_loss_monitor', {})

                # 恢复单日开仓限制
                self.daily_trade_count = state.get('daily_trade_count', 0)
                last_trade_date_str = state.get('last_trade_date')
                if last_trade_date_str:
                    self.last_trade_date = datetime.fromisoformat(last_trade_date_str).date() if isinstance(last_trade_date_str, str) else last_trade_date_str

                # 恢复最大回撤熔断
                self.cumulative_pnl = Decimal(str(state.get('cumulative_pnl', '0')))

                # state 版本迁移：旧版peak_equity基于错误公式(balance+cumulative_pnl)，必须重置
                state_version = int(state.get('state_version', 1))
                peak_pnl_str = state.get('peak_pnl') or state.get('peak_equity')  # 兼容旧字段名

                if state_version < 2:
                    logger.warning(
                        f"检测到旧版 state (v{state_version})，重置 peak_pnl（旧值基于错误公式）",
                        old_peak_equity=peak_pnl_str
                    )
                    self.peak_pnl = None
                else:
                    if peak_pnl_str:
                        self.peak_pnl = Decimal(str(peak_pnl_str))
                    else:
                        self.peak_pnl = None

                drawdown_pause_str = state.get('drawdown_pause_until')
                if drawdown_pause_str:
                    self.drawdown_pause_until = datetime.fromisoformat(drawdown_pause_str)
                
                logger.info(
                    f"恢复策略状态完成",
                    positions=len(self.positions),
                    traded_symbols=len(self.traded_symbols),
                    consecutive_losses=self.consecutive_losses,
                    blacklist=len(self.blacklist)
                )

                # 从 condition_orders 表恢复条件单 algoId 到 position_tracking
                # 解决容器重启后 position_tracking 丢失，导致平仓时无法取消条件单的问题
                try:
                    open_orders = await get_open_orders(self.db, self.strategy_name)
                    restored_count = 0
                    for order in open_orders:
                        symbol = order.get('symbol')
                        algo_id = order.get('algo_id')
                        if not symbol or not algo_id:
                            continue
                        # 仅恢复有持仓的币种的条件单，避免无持仓时误操作
                        if symbol not in self.positions:
                            continue
                        # 初始化 position_tracking 条目
                        if symbol not in self.trading_executor.position_tracking:
                            self.trading_executor.position_tracking[symbol] = {
                                'algo_ids': {},
                            }
                        role = f'db_{order.get("order_type", "UNKNOWN")}_{algo_id}'
                        self.trading_executor.position_tracking[symbol]['algo_ids'][role] = algo_id
                        restored_count += 1
                        logger.info(
                            "从 condition_orders 恢复条件单",
                            symbol=symbol,
                            algo_id=algo_id,
                            role=role,
                        )
                    if restored_count > 0:
                        logger.info(
                            "条件单恢复完成",
                            count=restored_count,
                        )
                except Exception as e:
                    logger.warning(
                        "从 condition_orders 恢复条件单失败",
                        error=str(e),
                    )
            else:
                logger.info("无历史状态，从头开始")

        except Exception as e:
            logger.error(f"恢复策略状态失败: {e}")
            self.positions = {}
            self.traded_symbols = []
            self.consecutive_losses = 0
            self.pause_until = None
            self.blacklist = []
            self.stop_loss_monitor = {}
            # 补充回撤熔断字段重置，避免异常后残留脏状态导致误触发熔断
            self.cumulative_pnl = Decimal('0')
            self.peak_pnl = None
            self.drawdown_pause_until = None

    async def _save_state(self) -> None:
        """
        保存策略状态
        
        保存内容：
        1. 持仓信息
        2. 已交易币种列表
        3. 连续亏损计数
        4. 暂停状态
        5. 永久黑名单
        6. 止损监控列表
        """
        try:
            import json
            state_data = {
                'positions': self.positions,
                'traded_symbols': self.traded_symbols,
                'consecutive_losses': self.consecutive_losses,
                'pause_until': self.pause_until.isoformat() if self.pause_until else None,
                'blacklist': self.blacklist,
                'stop_loss_monitor': self.stop_loss_monitor,

                # 单日开仓限制
                'daily_trade_count': self.daily_trade_count,
                'last_trade_date': str(self.last_trade_date) if self.last_trade_date else None,

                # 最大回撤熔断
                'state_version': 2,  # state结构版本号（v2: 基于cumulative_pnl序列计算回撤）
                'cumulative_pnl': str(self.cumulative_pnl),
                'peak_pnl': str(self.peak_pnl) if self.peak_pnl is not None else None,
                'drawdown_pause_until': self.drawdown_pause_until.isoformat() if self.drawdown_pause_until else None,

                'last_update': datetime.now().isoformat()
            }

            await self.db.execute(
                """
                INSERT INTO strategy_states (strategy_name, state_key, state_data, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (strategy_name, state_key)
                DO UPDATE SET state_data = $3, updated_at = $4
                """,
                self.strategy_name,
                'main',
                json.dumps(state_data),
                datetime.now()
            )

            logger.debug("策略状态已保存")

        except Exception as e:
            logger.error(f"保存策略状态失败: {e}")
    
    async def _check_pause_status(self) -> bool:
        """
        检查策略是否处于暂停状态
        
        Returns:
            True: 暂停中，False: 正常
        """
        # 如果没有暂停时间，返回正常
        if not self.pause_until:
            return False
        
        # 检查是否已过暂停期
        if datetime.now() >= self.pause_until:
            logger.info("暂停期已结束，恢复交易")
            self.pause_until = None
            self.consecutive_losses = 0
            await self._save_state()
            return False
        
        # 仍在暂停期
        remaining = (self.pause_until - datetime.now()).total_seconds() / 3600
        logger.info(f"策略暂停中，剩余时间: {remaining:.1f}小时")
        return True
    
    async def _check_consecutive_loss_pause(self) -> None:
        """
        检查是否需要触发连续亏损暂停
        
        触发条件：
        - 连续亏损次数 >= 配置的最大次数
        """
        # 获取配置
        consecutive_config = self.config.get('trading', {}).get('consecutive_loss', {})
        
        if not consecutive_config.get('enabled', True):
            return
        
        max_losses = consecutive_config.get('max_consecutive_losses', 3)
        pause_hours = consecutive_config.get('pause_hours', 48)
        
        # 检查是否达到阈值
        if self.consecutive_losses >= max_losses:
            self.pause_until = datetime.now() + timedelta(hours=pause_hours)
            
            logger.warning(
                f"触发连续亏损暂停",
                consecutive_losses=self.consecutive_losses,
                pause_hours=pause_hours,
                resume_time=self.pause_until.isoformat()
            )
            
            # 发送通知
            await self.notification_client.send(
                message=f"【新币做空策略暂停】\n连续亏损次数: {self.consecutive_losses}\n暂停时长: {pause_hours}小时\n恢复时间: {self.pause_until.strftime('%Y-%m-%d %H:%M:%S')}",
                level="warning",
                project="new_coin"
            )
            
            await self._save_state()

    async def _check_max_drawdown(self, pnl: float) -> None:
        """
        检查最大回撤熔断（平仓后调用）

        基于 cumulative_pnl 序列计算回撤，与账户余额完全解耦，
        避免跨策略污染和重复计算。

        Args:
            pnl: 本次平仓盈亏金额
        """
        try:
            self.cumulative_pnl += Decimal(str(pnl))
            await self._evaluate_drawdown_and_maybe_trigger("平仓后检查")
        except Exception as e:
            logger.error(f"检查最大回撤失败: {e}")

    async def _refresh_drawdown_status(self) -> None:
        """
        刷新回撤熔断状态（在run_cycle开始时调用）

        基于 cumulative_pnl 刷新 peak_pnl，并检查是否触发熔断。
        不修改 cumulative_pnl，仅基于当前值检查。
        用于捕获平仓间隔内的回撤状态变化。
        """
        try:
            await self._evaluate_drawdown_and_maybe_trigger("周期刷新")
        except Exception as e:
            logger.error(f"刷新回撤熔断状态失败: {e}")

    async def _evaluate_drawdown_and_maybe_trigger(self, trigger_reason: str) -> None:
        """
        评估回撤并在达到阈值时触发熔断（公共逻辑）

        由 _check_max_drawdown（平仓后）和 _refresh_drawdown_status（周期刷新）
        共同调用，消除重复代码。本方法不修改 cumulative_pnl，仅基于当前值
        更新 peak_pnl、计算回撤率、按需触发熔断并发送通知。

        Args:
            trigger_reason: 触发场景描述（用于日志区分来源）
        """
        try:
            # 更新历史最高累计盈亏（all-time high）
            if self.peak_pnl is None or self.cumulative_pnl > self.peak_pnl:
                self.peak_pnl = self.cumulative_pnl

            # 计算回撤率（仅当存在盈利高点时）
            if self.peak_pnl is not None and self.peak_pnl > 0:
                drawdown = (self.peak_pnl - self.cumulative_pnl) / self.peak_pnl

                dd_config = self.config.get('trading', {}).get('max_drawdown', {})
                threshold = Decimal(str(dd_config.get('threshold', 0.15)))
                pause_days = dd_config.get('pause_days', 7)

                # 仅在未处于熔断状态时检查，避免重复触发
                if drawdown >= threshold and not self.drawdown_pause_until:
                    self.drawdown_pause_until = datetime.now(timezone.utc) + timedelta(days=pause_days)

                    logger.warning(
                        f"触发回撤熔断（{trigger_reason}）",
                        drawdown=float(drawdown),
                        threshold=float(threshold),
                        cumulative_pnl=float(self.cumulative_pnl),
                        peak_pnl=float(self.peak_pnl),
                        pause_until=self.drawdown_pause_until.isoformat()
                    )

                    # 发送飞书通知
                    try:
                        await self.notification_client.send(
                            message=(
                                f"【回撤熔断触发】\n"
                                f"当前回撤: {float(drawdown):.1%}\n"
                                f"阈值: {float(threshold):.1%}\n"
                                f"暂停至: {self.drawdown_pause_until.strftime('%Y-%m-%d %H:%M UTC')}\n"
                                f"累计盈亏: {float(self.cumulative_pnl):.2f} USDT"
                            ),
                            level="warning",
                            project="new_coin"
                        )
                    except Exception as e:
                        logger.error(f"发送回撤熔断通知失败: {e}")
        except Exception as e:
            logger.error(f"评估回撤熔断失败: {e}")

    async def _get_position_pnl(self, symbol: str, entry_price: float) -> Optional[float]:
        """
        获取持仓盈亏

        Args:
            symbol: 交易对
            entry_price: 入场价格

        Returns:
            盈亏金额（USDT）；查不到平仓记录时返回 None
        """
        try:
            # 从数据库获取最近的平仓记录
            order = await self.db.fetch_one(
                """
                SELECT quantity, price, side
                FROM orders
                WHERE symbol = $1 AND strategy = 'new_coin' AND side = 'BUY'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                symbol
            )

            if not order:
                logger.warning(f"未找到平仓记录: {symbol}")
                return None

            # 计算盈亏
            quantity = float(order.get('quantity', 0))
            exit_price = float(order.get('price', 0))

            # 做空盈亏 = (入场价 - 出场价) * 数量
            pnl = (entry_price - exit_price) * quantity

            logger.debug(
                f"计算盈亏: {symbol}",
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl=pnl
            )

            return pnl

        except Exception as e:
            logger.error(f"获取持仓盈亏失败: {symbol}, 错误: {e}")
            return None
    
    async def _add_to_stop_loss_monitor(
        self,
        symbol: str,
        entry_price: float,
        entry_time: datetime
    ) -> None:
        """
        添加到止损监控列表
        
        用于检测止损后是否反向暴涨，触发黑名单机制
        
        Args:
            symbol: 交易对
            entry_price: 入场价格
            entry_time: 入场时间
        """
        # 获取配置
        blacklist_config = self.config.get('trading', {}).get('blacklist', {})
        
        if not blacklist_config.get('enabled', True):
            return
        
        check_hours = blacklist_config.get('check_hours', 24)
        
        # 添加到监控列表
        self.stop_loss_monitor[symbol] = {
            'entry_price': entry_price,
            'entry_time': entry_time.isoformat(),
            'monitor_until': (datetime.now() + timedelta(hours=check_hours)).isoformat()
        }
        
        logger.info(
            f"添加到止损监控列表: {symbol}",
            entry_price=entry_price,
            monitor_hours=check_hours
        )
        
        await self._save_state()
    
    async def _check_blacklist_monitor(self) -> None:
        """
        检查止损监控列表，检测反向暴涨
        
        逻辑：
        1. 遍历止损监控列表
        2. 获取当前价格
        3. 如果价格超过止损价5%，加入永久黑名单
        4. 移除过期的监控项
        """
        if not self.stop_loss_monitor:
            return
        
        # 获取配置
        blacklist_config = self.config.get('trading', {}).get('blacklist', {})
        reverse_surge_percent = blacklist_config.get('reverse_surge_percent', 0.05)
        
        for symbol, monitor_data in list(self.stop_loss_monitor.items()):
            try:
                # 检查是否过期
                monitor_until = datetime.fromisoformat(monitor_data['monitor_until'])
                if datetime.now() > monitor_until:
                    logger.info(f"止损监控过期，移除: {symbol}")
                    del self.stop_loss_monitor[symbol]
                    continue
                
                # 获取当前价格
                ticker = await self.binance_client._request(
                    "GET",
                    "/fapi/v1/ticker/price",
                    params={'symbol': symbol},
                    signed=False
                )
                
                current_price = float(ticker.get('price', 0))
                entry_price = monitor_data['entry_price']
                
                # 计算涨幅
                price_change = (current_price - entry_price) / entry_price
                
                # 检查是否反向暴涨
                if price_change >= reverse_surge_percent:
                    # 加入永久黑名单
                    if symbol not in self.blacklist:
                        self.blacklist.append(symbol)
                        logger.warning(
                            f"币种反向暴涨，加入永久黑名单: {symbol}",
                            entry_price=entry_price,
                            current_price=current_price,
                            price_change_percent=price_change * 100
                        )
                        
                        # 发送通知
                        await self.notification_client.send(
                            message=f"【新币黑名单警告】\n交易对: {symbol}\n入场价: {entry_price}\n当前价: {current_price}\n涨幅: {price_change*100:.2f}%\n已加入永久黑名单",
                            level="warning",
                            project="new_coin"
                        )
                    
                    # 移除监控
                    del self.stop_loss_monitor[symbol]
                
            except Exception as e:
                logger.error(f"检查黑名单监控失败: {symbol}, 错误: {e}")
        
        await self._save_state()
