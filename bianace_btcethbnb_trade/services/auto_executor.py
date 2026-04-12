#!/usr/bin/env python3
"""
自动交易执行器 - 根据 DeepSeek 的分析建议自动开单
"""

import logging
from typing import Dict, Any, List, Optional
from decimal import Decimal
from utils.binance_trade_api import BinanceTradeAPI
from utils.json_extractor import TradeRecommendExtractor

logger = logging.getLogger('auto_executor')

class AutoTradeExecutor:
    """自动交易执行器"""
    
    def __init__(self, api: BinanceTradeAPI):
        """
        初始化自动交易执行器
        
        Args:
            api: 币安交易 API 实例
        """
        self.api = api
        self.extractor = TradeRecommendExtractor()
        
        # 500U 阶段一配置
        self.total_capital = Decimal('500')  # 总资金
        self.single_position_margin = Decimal('30')  # 单仓保证金（固定 30U）
        self.max_positions = 2  # 最大同时持仓数
        self.allowed_signal_grades = ['S', 'A']  # 允许的信号等级
        self.min_recommendation_score = 70  # 最小推荐度
        
        # 重试配置
        self.max_retries = 2  # 最大重试次数
        self.retry_delay = 1  # 重试延迟（秒）
        
    def execute_analysis(self, report_content: str) -> Dict[str, Any]:
        """
        执行分析报告并自动开单
        
        Args:
            report_content: DeepSeek 返回的完整报告文本
            
        Returns:
            执行结果字典
        """
        logger.info("=" * 60)
        logger.info("开始执行自动交易流程")
        logger.info(f"报告内容长度：{len(report_content)} 字符")
        
        # 步骤 1: 提取 JSON 交易建议
        logger.info("-" * 60)
        logger.info("步骤 1: 提取 JSON 交易建议...")
        recommendations = self.extractor.extract_json(report_content)
        
        if not recommendations:
            logger.error("❌ 未能从报告中提取交易建议")
            logger.error("可能原因：")
            logger.error("  1. AI 返回的报告格式不正确，缺少 JSON 代码块")
            logger.error("  2. JSON 代码块中的字段不完整或格式错误")
            logger.error("  3. 验证规则过于严格，误杀了有效数据")
            logger.debug(f"报告内容前 1000 字符：\n{report_content[:1000]}")
            return {
                'success': False,
                'message': '未能从报告中提取交易建议，请检查 AI 返回的报告格式',
                'executed_trades': [],
                'failure_stage': 'json_extraction'  # 失败阶段标记
            }
        
        logger.info(f"✅ 成功提取 {len(recommendations)} 条交易建议")
        for i, rec in enumerate(recommendations):
            logger.info(f"  建议{i+1}: {rec.get('币种')} - {rec.get('开仓方向')} (推荐度:{rec.get('开仓推荐度')}, 等级:{rec.get('信号等级')})")
        
        # 步骤 2: 过滤有效信号
        logger.info("-" * 60)
        logger.info("步骤 2: 过滤有效信号...")
        valid_signals = self.extractor.extract_valid_signals(
            recommendations,
            min_score=self.min_recommendation_score,
            allowed_grades=self.allowed_signal_grades
        )
        
        if not valid_signals:
            logger.info("⚠️ 没有符合条件的交易信号")
            logger.info("可能原因：")
            logger.info("  1. 所有信号的推荐度都 < 70 分")
            logger.info("  2. 所有信号的等级都不是 S 或 A")
            logger.info("  3. 所有信号都是'观望'方向")
            logger.info("  4. 信号的价格字段（开仓价、止损价）无效")
            # 详细记录每个信号被过滤的原因
            for i, rec in enumerate(recommendations):
                reason = self._check_signal_filter_reason(rec, self.min_recommendation_score, self.allowed_signal_grades)
                if reason:
                    logger.info(f"  信号{i+1} ({rec.get('币种')}) 被过滤：{reason}")
            return {
                'success': True,
                'message': '没有符合条件的交易信号',
                'executed_trades': [],
                'failure_stage': 'signal_filtering',  # 失败阶段标记
                'total_recommendations': len(recommendations)
            }
        
        logger.info(f"✅ 筛选出 {len(valid_signals)} 条有效信号")
        for i, signal in enumerate(valid_signals):
            logger.info(f"  有效信号{i+1}: {signal['币种']} - {signal['开仓方向']} (推荐度:{signal['开仓推荐度']}, 等级:{signal['信号等级']})")
        
        # 步骤 3: 检查当前持仓
        logger.info("-" * 60)
        logger.info("步骤 3: 检查当前持仓...")
        current_positions = self._get_current_positions()
        available_slots = self.max_positions - len(current_positions)
        
        if available_slots <= 0:
            logger.warning(f"⚠️ 当前已满仓（{len(current_positions)} 个持仓），无法开新仓")
            logger.warning("持仓列表:")
            for pos in current_positions:
                logger.warning(f"  - {pos.get('symbol')}: {pos.get('positionAmt')} {pos.get('positionSide')}")
            return {
                'success': False,
                'message': f'当前已满仓，无法开新仓',
                'current_positions': current_positions,
                'executed_trades': [],
                'failure_stage': 'position_check'  # 失败阶段标记
            }
        
        logger.info(f"✅ 可用仓位：{available_slots}/{self.max_positions}")
        if current_positions:
            logger.info(f"  当前持仓:")
            for pos in current_positions:
                logger.info(f"    - {pos.get('symbol')}: {pos.get('positionAmt')} {pos.get('positionSide')}")
        
        # 步骤 4: 执行开单
        logger.info("-" * 60)
        logger.info("步骤 4: 执行开单...")
        executed_trades = []
        for signal in valid_signals[:available_slots]:  # 只执行可用仓位数量的交易
            logger.info(f"  准备执行：{signal['币种']} {signal['开仓方向']}")
            
            # 带重试的交易执行
            trade_result = None
            for attempt in range(self.max_retries + 1):
                if attempt > 0:
                    logger.warning(f"  重试 {attempt}/{self.max_retries} for {signal['币种']}")
                
                trade_result = self._execute_trade_with_retry(signal, attempt > 0)
                
                if trade_result['success']:
                    logger.info(f"  ✅ {signal['币种']}: 开单成功")
                    break
                else:
                    # 如果是最后一次尝试仍然失败，记录错误
                    if attempt == self.max_retries:
                        logger.error(f"  ❌ {signal['币种']}: 开单失败 - {trade_result['message']} (已重试{self.max_retries}次)")
                    else:
                        logger.warning(f"  ⚠️ {signal['币种']}: 开单失败 - {trade_result['message']}，准备重试...")
            
            executed_trades.append(trade_result)
        
        success_count = sum(1 for t in executed_trades if t['success'])
        fail_count = len(executed_trades) - success_count
        
        logger.info("-" * 60)
        logger.info(f"执行完成：成功 {success_count}/{len(valid_signals)} 笔，失败 {fail_count} 笔")
        if fail_count > 0:
            logger.error("失败详情:")
            for trade in executed_trades:
                if not trade['success']:
                    logger.error(f"  - {trade.get('symbol', 'UNKNOWN')}: {trade['message']}")
        
        logger.info("=" * 60)
        
        return {
            'success': success_count > 0,
            'message': f'成功执行 {success_count}/{len(valid_signals)} 笔交易',
            'executed_trades': executed_trades,
            'total_signals': len(valid_signals),
            'successful_trades': success_count
        }
    
    def _check_signal_filter_reason(self, rec: Dict[str, Any], min_score: int, allowed_grades: List[str]) -> Optional[str]:
        """
        检查信号被过滤的原因
        
        Args:
            rec: 交易建议
            min_score: 最小推荐度
            allowed_grades: 允许的信号等级
            
        Returns:
            过滤原因，如果没有被过滤则返回 None
        """
        # 检查观望
        if rec.get('开仓方向') == '观望':
            return "开仓方向为'观望'"
        
        # 检查信号等级
        signal_grade = rec.get('信号等级', '')
        grade_base = signal_grade.rstrip('+-')
        allowed_grades_base = [g.rstrip('+-') for g in allowed_grades]
        if grade_base not in allowed_grades_base:
            return f"信号等级 {signal_grade} 不在允许范围内 (允许：{allowed_grades})"
        
        # 检查推荐度
        recommendation_score = rec.get('开仓推荐度', 0)
        if recommendation_score < min_score:
            return f"推荐度 {recommendation_score} < {min_score}"
        
        # 检查价格信息
        if rec.get('开仓价') in ['N/A', None, '']:
            return "开仓价无效"
        
        if rec.get('止损价') in ['N/A', None, '']:
            return "止损价无效"
        
        return None
    
    def _get_current_positions(self) -> List[Dict[str, Any]]:
        """获取当前持仓列表"""
        try:
            positions = self.api.get_position_risk()
            
            # 过滤出有持仓的位置
            active_positions = [
                pos for pos in positions 
                if Decimal(pos.get('positionAmt', '0')) != 0
            ]
            
            logger.info(f"当前持仓数：{len(active_positions)}")
            return active_positions
            
        except Exception as e:
            logger.error(f"获取持仓失败：{str(e)}")
            return []
    
    def _get_position_quantity(self, symbol: str) -> Decimal:
        """获取指定交易对的持仓数量"""
        try:
            positions = self.api.get_position_risk(symbol)
            
            if not positions:
                return Decimal('0')
            
            # 获取第一个持仓的数量
            position = positions[0]
            position_amt = Decimal(position.get('positionAmt', '0'))
            
            return abs(position_amt)  # 返回绝对值
            
        except Exception as e:
            logger.error(f"获取持仓数量失败：{str(e)}")
            return Decimal('0')
    
    def _execute_trade_with_retry(self, signal: Dict[str, Any], is_retry: bool = False) -> Dict[str, Any]:
        """
        执行单笔交易（带重试逻辑）
        
        Args:
            signal: 交易信号
            is_retry: 是否是重试
            
        Returns:
            交易结果
        """
        # 如果是重试，添加额外日志
        if is_retry:
            logger.warning(f"    🔄 重试执行：{signal['币种']} {signal['开仓方向']}")
        
        return self._execute_trade(signal)
    
    def _execute_trade(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行单笔交易
        
        Args:
            signal: 交易信号（来自 JSON）
            
        Returns:
            交易结果
        """
        symbol = signal['币种'].replace('/', '')  # BTC/USDT -> BTCUSDT
        direction = signal['开仓方向']
        quantity = self._calculate_quantity(signal)
        
        logger.info(f"    准备开单：{symbol} {direction} 数量={quantity}")
        
        # 开仓前检查：资金预检查
        pre_check_result = self._pre_trade_check(symbol, quantity)
        if not pre_check_result['success']:
            logger.error(f"    ❌ {symbol}: 开仓前检查失败 - {pre_check_result['message']}")
            return {
                'success': False,
                'symbol': symbol,
                'message': pre_check_result['message'],
                'error_type': 'balance_error',
                'account_info': pre_check_result.get('account_info')
            }
        
        try:
            # 步骤 1: 设置杠杆
            leverage = int(signal.get('实际杠杆', 5))
            if leverage < 1 or leverage > 20:
                logger.warning(f"    杠杆 {leverage} 超出范围 (1-20)，使用默认值 5x")
                leverage = 5  # 默认杠杆
            
            logger.info(f"    步骤 1: 设置杠杆 {leverage}x")
            self.api.set_um_leverage(symbol, leverage)
            logger.info(f"    ✅ {symbol}: 杠杆已设置为 {leverage}x")
            
            # 步骤 2: 开仓
            logger.info(f"    步骤 2: 开仓...")
            side = 'BUY' if direction == '多' else 'SELL'
            position_side = 'BOTH'  # 单向持仓模式
            
            logger.info(f"      参数：side={side}, position_side={position_side}, quantity={quantity}")
            order = self.api.place_market_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                quantity=quantity
            )
            
            if not order:
                logger.error(f"    ❌ {symbol}: 开仓订单失败，返回值为空")
                return {
                    'success': False,
                    'symbol': symbol,
                    'message': '开仓订单失败，返回值为空',
                    'failure_stage': 'place_order'
                }
            
            logger.info(f"    ✅ {symbol}: 开仓成功，订单号：{order.get('orderId')}")
            
            # 步骤 3: 设置止损止盈
            logger.info(f"    步骤 3: 设置止损止盈...")
            stop_loss = signal.get('止损价')
            take_profit_levels = signal.get('止盈设置', {})
            
            if stop_loss and stop_loss != 'N/A':
                try:
                    self._set_stop_loss(symbol, direction, stop_loss)
                    logger.info(f"      ✅ {symbol}: 止损已设置 @ {stop_loss}")
                except Exception as e:
                    logger.error(f"      ⚠️ {symbol}: 止损设置失败 - {str(e)}，但不影响交易成功")
            
            # 设置止盈（分批）
            if isinstance(take_profit_levels, dict):
                for tp_name, tp_data in take_profit_levels.items():
                    if isinstance(tp_data, dict) and '价格' in tp_data:
                        tp_price = tp_data['价格']
                        if tp_price and tp_price != 'N/A':
                            try:
                                self._set_take_profit(symbol, direction, tp_price, tp_name)
                                logger.info(f"      ✅ {symbol}: {tp_name}止盈已设置 @ {tp_price}")
                            except Exception as e:
                                logger.error(f"      ⚠️ {symbol}: {tp_name}止盈设置失败 - {str(e)}，但不影响交易成功")
            
            logger.info(f"    ✅ {symbol}: 所有步骤完成")
            return {
                'success': True,
                'symbol': symbol,
                'direction': direction,
                'quantity': float(quantity),
                'leverage': leverage,
                'order_id': order.get('orderId'),
                'stop_loss': stop_loss,
                'take_profit': take_profit_levels,
                'message': '开仓成功'
            }
            
        except Exception as e:
            logger.error(f"    ❌ {symbol}: 开仓失败 - {str(e)}", exc_info=True)
            error_type = self._classify_error(str(e))
            
            # 如果是保证金不足错误，增强错误信息
            error_msg = str(e)
            if error_type == 'balance_error' and ('insufficient' in error_msg.lower() or 'margin' in error_msg.lower()):
                enhanced_msg = self._enhance_insufficient_margin_error(error_msg)
                logger.error(f"    💰 {symbol}: 资金不足详情:\n{enhanced_msg}")
                return {
                    'success': False,
                    'symbol': symbol,
                    'message': enhanced_msg,
                    'error_type': error_type,
                    'account_info': self._get_account_balance_info()
                }
            
            return {
                'success': False,
                'symbol': symbol,
                'message': f'开仓失败：{error_msg}',
                'error_type': error_type
            }
    
    def _classify_error(self, error_message: str) -> str:
        """
        分类错误类型，便于诊断问题
        
        Args:
            error_message: 错误消息
            
        Returns:
            错误类型
        """
        error_lower = error_message.lower()
        
        # 资金相关错误（优先检查，因为 API 错误也可能包含 margin/insufficient）
        if 'balance' in error_lower or 'margin' in error_lower or '资金' in error_lower or 'insufficient' in error_lower:
            return 'balance_error'
        
        # API 相关错误
        if 'api' in error_lower or 'http' in error_lower or 'request' in error_lower:
            return 'api_error'
        
        # 持仓相关错误
        if 'position' in error_lower or '持仓' in error_lower:
            return 'position_error'
        
        # 订单相关错误
        if 'order' in error_lower or '订单' in error_lower:
            return 'order_error'
        
        # 精度相关错误
        if 'precision' in error_lower or 'quantity' in error_lower or '精度' in error_lower:
            return 'precision_error'
        
        # 权限相关错误
        if 'permission' in error_lower or 'unauthorized' in error_lower:
            return 'permission_error'
        
        # 默认未知错误
        return 'unknown_error'
    
    def _pre_trade_check(self, symbol: str, quantity: Decimal) -> Dict[str, Any]:
        """
        开仓前检查：验证账户资金是否足够
        
        Args:
            symbol: 交易对
            quantity: 开仓数量
            
        Returns:
            检查结果字典
        """
        logger.info(f"    开仓前检查：验证账户资金...")
        
        # 获取账户信息
        account_info = self._get_account_balance_info()
        
        available_balance = Decimal(account_info['available_balance'])
        total_wallet_balance = Decimal(account_info['total_wallet_balance'])
        
        # 计算预估所需保证金（包含 15% 缓冲）
        # 缓冲用于覆盖：精度调整、价格波动、手续费
        buffer_factor = Decimal('1.15')  # 15% 缓冲
        required_margin = self.single_position_margin * buffer_factor
        
        logger.info(f"      账户可用余额：{available_balance} USDT")
        logger.info(f"      账户总余额：{total_wallet_balance} USDT")
        logger.info(f"      预估所需保证金：{required_margin} USDT（含 15% 缓冲）")
        
        # 检查资金是否足够
        if available_balance < required_margin:
            # 资金不足
            is_testnet = self.api.testnet if hasattr(self.api, 'testnet') else False
            
            if is_testnet and total_wallet_balance == 0:
                # 测试网且余额为 0
                message = (
                    f"测试网账户余额为 0，无法执行交易。\n"
                    f"【测试网环境说明】\n"
                    f"  - 当前使用测试网 API，账户余额：0 USDT\n"
                    f"  - 测试网用于 API 功能测试，不需要真实资金\n"
                    f"  - 如需测试完整交易流程，请申请测试网赠金或切换到生产环境\n"
                    f"\n"
                    f"【解决方案】\n"
                    f"  1. 申请测试网赠金（联系币安官方）\n"
                    f"  2. 切换到生产环境并充值（设置 BINANCE_TESTNET=false）\n"
                    f"  3. 降低每笔交易的保证金配置（修改 auto_executor.py 中的 single_position_margin）"
                )
                logger.warning(f"    💰 {symbol}: {message}")
                return {
                    'success': False,
                    'message': message,
                    'account_info': account_info,
                    'is_testnet': True
                }
            else:
                # 生产环境资金不足或测试网有部分余额
                message = (
                    f"账户可用余额不足，无法执行交易。\n"
                    f"【资金不足详情】\n"
                    f"  - 账户可用余额：{available_balance} USDT\n"
                    f"  - 账户总余额：{total_wallet_balance} USDT\n"
                    f"  - 预估所需保证金：{required_margin} USDT（含 15% 缓冲）\n"
                    f"  - 资金缺口：{required_margin - available_balance} USDT\n"
                )
                
                if account_info['positions']:
                    message += "\n【当前持仓占用】\n"
                    for pos in account_info['positions']:
                        message += f"  - {pos['symbol']}: 占用保证金 {pos['positionMargin']} USDT\n"
                
                message += "\n【解决方案】\n"
                message += "  1. 等待现有持仓平仓后释放保证金\n"
                message += f"  2. 向账户充值，至少需要补充 {required_margin - available_balance} USDT\n"
                message += "  3. 降低每笔交易的保证金配置（需修改配置文件）"
                
                logger.error(f"    💰 {symbol}: {message}")
                return {
                    'success': False,
                    'message': message,
                    'account_info': account_info,
                    'is_testnet': is_testnet
                }
        
        # 资金充足
        logger.info(f"    ✅ {symbol}: 资金检查通过")
        return {
            'success': True,
            'message': '资金检查通过',
            'account_info': account_info
        }
    
    def _get_account_balance_info(self) -> Dict[str, Any]:
        """
        获取账户余额和持仓信息，用于诊断资金不足问题
        
        Returns:
            包含账户余额和持仓详情的字典
        """
        try:
            # 获取账户信息
            account_info = self.api.futures_account()
            
            # 提取可用余额（优先使用 get_umfut_balance 方法，它对 PM 账户更准确）
            try:
                available_balance = self.api.get_umfut_balance('USDT')
            except Exception:
                # 如果失败，回退到从账户信息提取
                available_balance = Decimal('0')
            
            total_wallet_balance = Decimal('0')
            
            for asset in account_info.get('assets', []):
                if asset.get('asset') == 'USDT':
                    total_wallet_balance = Decimal(asset.get('walletBalance', '0'))
                    # 如果 get_umfut_balance 失败，尝试从 availableBalance 获取
                    if available_balance == 0:
                        available_balance = Decimal(asset.get('availableBalance', '0'))
                    break
            
            # 获取当前持仓
            positions = []
            total_position_margin = Decimal('0')
            
            for pos in account_info.get('positions', []):
                position_amt = Decimal(pos.get('positionAmt', '0'))
                if position_amt != 0:
                    symbol = pos.get('symbol')
                    position_margin = Decimal(pos.get('positionInitialMargin', '0'))
                    unrealized_pnl = Decimal(pos.get('unrealizedProfit', '0'))
                    
                    positions.append({
                        'symbol': symbol,
                        'positionAmt': str(position_amt),
                        'positionMargin': str(position_margin),
                        'unrealizedPnl': str(unrealized_pnl),
                        'entryPrice': pos.get('entryPrice'),
                        'markPrice': pos.get('markPrice')
                    })
                    total_position_margin += position_margin
            
            return {
                'available_balance': str(available_balance),
                'total_wallet_balance': str(total_wallet_balance),
                'total_position_margin': str(total_position_margin),
                'positions': positions,
                'position_count': len(positions)
            }
            
        except Exception as e:
            logger.error(f"获取账户余额信息失败：{str(e)}")
            return {
                'available_balance': 'N/A',
                'total_wallet_balance': 'N/A',
                'total_position_margin': 'N/A',
                'positions': [],
                'position_count': 0,
                'error': str(e)
            }
    
    def _enhance_insufficient_margin_error(self, error_message: str) -> str:
        """
        增强保证金不足错误的提示信息
        
        Args:
            error_message: 原始错误消息
            
        Returns:
            增强后的错误消息
        """
        # 获取账户详细信息
        account_info = self._get_account_balance_info()
        
        detailed_msg = f"{error_message}\n\n"
        detailed_msg += "【资金不足原因分析】\n"
        detailed_msg += f"  - 账户可用余额：{account_info['available_balance']} USDT\n"
        detailed_msg += f"  - 账户总余额：{account_info['total_wallet_balance']} USDT\n"
        detailed_msg += f"  - 当前持仓占用保证金：{account_info['total_position_margin']} USDT\n"
        detailed_msg += f"  - 当前持仓数量：{account_info['position_count']}\n"
        
        if account_info['positions']:
            detailed_msg += "\n【当前持仓详情】\n"
            for pos in account_info['positions']:
                detailed_msg += f"  - {pos['symbol']}: {pos['positionAmt']} (占用保证金：{pos['positionMargin']} USDT, 未实现盈亏：{pos['unrealizedPnl']} USDT)\n"
        
        detailed_msg += "\n【解决方案】\n"
        detailed_msg += "  1. 等待现有持仓平仓（触达止盈/止损）后释放保证金\n"
        detailed_msg += f"  2. 向账户充值，至少需要补充 {max(0, 30 - float(account_info['available_balance'])):.2f} USDT\n"
        detailed_msg += "  3. 降低每笔交易的保证金配置（需修改配置文件）\n"
        
        return detailed_msg
    
    def _calculate_quantity(self, signal: Dict[str, Any]) -> Decimal:
        """
        计算开仓数量
        
        Args:
            signal: 交易信号
            
        Returns:
            开仓数量
        """
        # 使用固定保证金计算
        margin = self.single_position_margin  # 30U
        leverage = int(signal.get('实际杠杆', 5))
        price = signal.get('开仓价')
        
        if not price or price == 'N/A':
            # 如果价格无效，使用当前市场价格
            price = self._get_current_price(signal['币种'].replace('/', ''))
        
        # 数量 = (保证金 * 杠杆) / 价格
        quantity = (margin * leverage) / Decimal(str(price))
        
        # 根据交易对精度调整（使用 API 获取真实精度）
        quantity = self._adjust_quantity_precision(signal['币种'].replace('/', ''), quantity)
        
        # 应用 5% 缓冲，确保实际下单时保证金充足
        # 精度调整可能导致数量略微增加，需要预留缓冲空间
        buffer_factor = Decimal('0.95')
        quantity = quantity * buffer_factor
        
        # 再次调整精度
        quantity = self._adjust_quantity_precision(signal['币种'].replace('/', ''), quantity)
        
        logger.info(f"计算数量：保证金={margin}U, 杠杆={leverage}x, 价格={price}, 数量={quantity} (已应用 5% 缓冲)")
        return quantity
    
    def _get_current_price(self, symbol: str) -> Decimal:
        """获取当前市场价格"""
        try:
            ticker = self.api.get_ticker_price(symbol)
            return Decimal(ticker.get('price', '0'))
        except Exception as e:
            logger.error(f"获取价格失败：{str(e)}")
            return Decimal('0')
    
    def _adjust_quantity_precision(self, symbol: str, quantity: Decimal) -> Decimal:
        """调整数量精度以符合币安要求"""
        try:
            # 使用 API 获取交易对的真实精度
            tick_size, step_size = self.api.get_symbol_precision(symbol)
            
            # 计算 step_size 的小数位数
            step_str = str(step_size)
            if '.' in step_str:
                decimals = len(step_str.split('.')[1])
            else:
                decimals = 0
            
            # 将数量四舍五入到正确的精度
            quantize_str = '0.' + '0' * decimals if decimals > 0 else '1'
            adjusted_quantity = quantity.quantize(Decimal(quantize_str))
            
            logger.debug(f"{symbol} 精度调整：step_size={step_size}, decimals={decimals}, {quantity} → {adjusted_quantity}")
            return adjusted_quantity
            
        except Exception as e:
            logger.warning(f"{symbol} 获取精度失败：{str(e)}，使用默认精度")
            # 回退到简化处理：BTC/ETH 保留 3 位小数，其他保留 2 位
            if 'BTC' in symbol:
                return quantity.quantize(Decimal('0.001'))
            elif 'ETH' in symbol:
                return quantity.quantize(Decimal('0.001'))
            else:
                return quantity.quantize(Decimal('0.01'))
    
    def _set_stop_loss(self, symbol: str, direction: str, stop_price: Any):
        """设置止损 - 使用条件单接口"""
        try:
            stop_price = Decimal(str(stop_price))
            side = 'SELL' if direction == '多' else 'BUY'
            position_side = 'BOTH'
            
            # 获取当前持仓数量
            quantity = self._get_position_quantity(symbol)
            if quantity <= 0:
                logger.warning(f"{symbol}: 无持仓，跳过止损设置")
                return
            
            # 使用条件单接口设置止损
            self.api.place_pm_conditional_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                strategy_type='STOP_MARKET',  # 止损市单
                quantity=quantity,
                stop_price=stop_price,
                reduce_only=True  # 只减仓，平仓
            )
        except Exception as e:
            logger.error(f"设置止损失败：{str(e)}")
    
    def _set_take_profit(self, symbol: str, direction: str, take_profit_price: Any, 
                        tp_name: str, quantity_ratio: str = '100%'):
        """设置止盈 - 使用条件单接口"""
        try:
            take_profit_price = Decimal(str(take_profit_price))
            side = 'SELL' if direction == '多' else 'BUY'
            position_side = 'BOTH'
            
            # 获取当前持仓数量
            quantity = self._get_position_quantity(symbol)
            if quantity <= 0:
                logger.warning(f"{symbol}: 无持仓，跳过止盈设置")
                return
            
            # 根据止盈级别计算平仓数量
            ratio = Decimal(quantity_ratio.replace('%', '')) / 100 if '%' in quantity_ratio else Decimal('1')
            tp_quantity = quantity * ratio
            
            # 使用条件单接口设置止盈
            self.api.place_pm_conditional_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                strategy_type='TAKE_PROFIT_MARKET',  # 止盈市单
                quantity=tp_quantity,
                stop_price=take_profit_price,
                reduce_only=True  # 只减仓，平仓
            )
        except Exception as e:
            logger.error(f"设置止盈失败：{str(e)}")


def auto_execute_from_report(report_file_path: str, api: BinanceTradeAPI) -> Dict[str, Any]:
    """
    从报告文件自动执行交易
    
    Args:
        report_file_path: 报告文件路径
        api: 币安交易 API 实例
        
    Returns:
        执行结果
    """
    try:
        with open(report_file_path, 'r', encoding='utf-8') as f:
            report_content = f.read()
        
        executor = AutoTradeExecutor(api)
        return executor.execute_analysis(report_content)
        
    except Exception as e:
        logger.error(f"自动执行失败：{str(e)}", exc_info=True)
        return {
            'success': False,
            'message': f'自动执行失败：{str(e)}',
            'executed_trades': []
        }


if __name__ == '__main__':
    # 测试代码
    import sys
    from utils.binance_trade_api import BinanceTradeAPI
    
    if len(sys.argv) > 1:
        report_path = sys.argv[1]
        
        # 初始化 API
        api = BinanceTradeAPI()
        
        # 执行交易
        result = auto_execute_from_report(report_path, api)
        
        print(f"\n执行结果：{result['message']}")
        if result['executed_trades']:
            print(f"\n执行的交易:")
            for trade in result['executed_trades']:
                if trade['success']:
                    print(f"✅ {trade['symbol']}: {trade['direction']} {trade['quantity']} @ {trade['leverage']}x")
                else:
                    print(f"❌ {trade['symbol']}: {trade['message']}")
    else:
        print("用法：python auto_executor.py <报告文件路径>")
