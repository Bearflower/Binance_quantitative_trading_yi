#!/usr/bin/env python3
"""
HRS 策略 - 完整时序回测

按历史时间点逐 K 线推进回测，模拟真实交易流程：
- 加载本地缓存的 K 线数据（CSV）
- 按时间顺序逐根 K 线推进
- 每根 K 线执行：候选池筛选 → 形态检测 → 评分 → 信号判断 → 模拟开仓/平仓
- 模拟止损止盈：按 K 线价格判断是否触发
- 统计绩效指标：总交易次数、胜率、平均盈亏比、最大回撤、夏普比率、总收益率
- 导出回测报告（Markdown 格式）

用法：
  python3 backtest/hrs/timeline_backtest.py --symbols LABUSDT,SAHARAUSDT --start 2026-01-01 --end 2026-06-01 --capital 10000
  python3 backtest/hrs/timeline_backtest.py --symbols LABUSDT --start 2026-03-01 --end 2026-06-01
"""
import sys
import os
import argparse
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from collections import defaultdict

import yaml
import pandas as pd
import numpy as np

# 添加项目根目录
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from strategies.hrs.pattern import PatternRecognizer
from strategies.hrs.scoring_engine import ScoringEngine, ScoringResult


# ============================================================
# 数据结构
# ============================================================

@dataclass
class Trade:
    """单笔交易记录"""
    symbol: str
    direction: str           # 'short' 或 'long'
    entry_time: datetime     # 开仓时间
    exit_time: datetime      # 平仓时间
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float               # 盈亏金额
    pnl_percent: float       # 盈亏百分比
    exit_reason: str         # 平仓原因：take_profit_1, take_profit_2, trailing_stop, stop_loss, time_stop
    max_favorable: float     # 最大浮盈百分比
    max_adverse: float       # 最大浮亏百分比
    holding_bars: int        # 持仓K线数


@dataclass
class BacktestResult:
    """回测结果汇总"""
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    initial_capital: float = 0.0
    final_capital: float = 0.0
    total_return: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_holding_bars: float = 0.0
    total_fees: float = 0.0


# ============================================================
# 工具函数
# ============================================================

def load_local_klines(csv_path: str) -> List[Dict]:
    """从本地CSV加载K线数据"""
    if not os.path.exists(csv_path):
        return []

    df = pd.read_csv(csv_path)
    if df.empty:
        return []

    klines = []
    for _, row in df.iterrows():
        dt = pd.to_datetime(row["open_time"])
        klines.append({
            "open_time": int(dt.timestamp() * 1000),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "quote_volume": float(row.get("quote_volume", 0)),
            "close_time": 0,
            "trades": 0,
        })
    return klines


def synthesize_4h_klines(klines_1h: List[Dict], interval_hours: int = 4) -> List[Dict]:
    """从1h K线合成4h K线"""
    if not klines_1h:
        return []

    klines_4h = []
    slot_klines = []

    for k in klines_1h:
        dt = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc)
        slot_hour = (dt.hour // interval_hours) * interval_hours
        slot_key = f"{dt.strftime('%Y%m%d')}_{slot_hour:02d}"

        if not slot_klines or slot_klines[-1].get("slot_key") != slot_key:
            if slot_klines:
                klines_4h.append(_merge_slot(slot_klines))
            slot_klines = [k]
            slot_klines[-1]["slot_key"] = slot_key
        else:
            slot_klines.append(k)

    if slot_klines:
        klines_4h.append(_merge_slot(slot_klines))

    return klines_4h


def _merge_slot(slot_klines: List[Dict]) -> Dict:
    return {
        "open_time": slot_klines[0]["open_time"],
        "open": slot_klines[0]["open"],
        "high": max(k["high"] for k in slot_klines),
        "low": min(k["low"] for k in slot_klines),
        "close": slot_klines[-1]["close"],
        "volume": sum(k["volume"] for k in slot_klines),
        "quote_volume": sum(k.get("quote_volume", 0) for k in slot_klines),
        "close_time": slot_klines[-1].get("close_time", 0),
    }


def calc_ema(data: List[float], period: int) -> float:
    """计算EMA"""
    if len(data) < period:
        return 0
    multiplier = 2.0 / (period + 1)
    ema = data[0]
    for price in data[1:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calc_atr(klines: List[Dict], period: int = 14) -> float:
    """计算ATR"""
    if len(klines) < period + 1:
        return 0

    tr_list = []
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    if len(tr_list) < period:
        return sum(tr_list) / len(tr_list)

    atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period

    return atr


# ============================================================
# 时序回测引擎
# ============================================================

class TimelineBacktest:
    """
    HRS 策略时序回测引擎

    按历史时间点逐 K 线推进，模拟完整的交易流程：
    候选池筛选 → 形态检测 → 评分 → 信号判断 → 模拟开仓 → 持仓监控 → 模拟平仓
    """

    def __init__(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        initial_capital: float = 10000.0,
        config_path: Optional[str] = None,
    ):
        """
        初始化回测引擎

        Args:
            symbols: 交易对列表
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            initial_capital: 初始资金 (USDT)
            config_path: 配置文件路径，默认使用策略配置
        """
        self.symbols = [s.strip().upper() for s in symbols]
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.peak_capital = initial_capital

        # 加载配置
        if config_path is None:
            config_path = os.path.join(
                project_root, "strategies", "hrs", "config.yaml"
            )
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        # 回测引擎模块
        self.pattern = PatternRecognizer(self.config)
        self.scoring = ScoringEngine(self.config)

        # 交易参数（从配置读取）
        trading_config = self.config.get("trading", {})
        self.leverage = trading_config.get("leverage", 2)
        self.max_loss_percent = trading_config.get("max_loss_percent", 0.02)
        self.max_daily_positions = trading_config.get("max_daily_positions", 3)
        self.max_daily_same_direction = trading_config.get("max_daily_same_direction", 2)

        # 止损止盈参数
        stop_loss_config = trading_config.get("stop_loss", {})
        self.stop_loss_atr_mult = stop_loss_config.get("atr_multiplier", 2.5)
        self.emergency_stop = stop_loss_config.get("emergency_percent", 0.015)
        self.min_absolute_stop = stop_loss_config.get("min_absolute_percent", 0.05)

        batch_config = trading_config.get("batch_take_profit", {})
        self.tp1_atr_mult = batch_config.get("target1_atr_multiplier", 1.5)
        self.tp1_close_pct = batch_config.get("target1_close_percent", 0.30)
        self.tp2_atr_mult = batch_config.get("target2_atr_multiplier", 3.5)
        self.tp2_close_pct = batch_config.get("target2_close_percent", 0.40)

        trailing_config = trading_config.get("trailing", {})
        self.trailing_atr_mult = trailing_config.get(
            "atr_multiplier",
            batch_config.get("trailing_stop_atr_multiplier", 1.5),
        )

        time_stop_config = trading_config.get("time_stop", {})
        self.max_holding_hours = time_stop_config.get("max_holding_hours", 72)

        # 候选池参数
        pool_config = self.config.get("candidate_pool", {})
        self.short_config = pool_config.get("short", {})
        self.long_config = pool_config.get("long", {})
        liquidity_config = pool_config.get("liquidity", {})
        self.min_volume_24h = liquidity_config.get("min_volume_24h", 50000000)
        self.min_oi_usd = liquidity_config.get("min_oi_usd", 10000000)

        # K线参数
        kline_config = self.config.get("kline", {})
        self.min_klines = kline_config.get("min_klines_for_analysis", 24)
        self.ema_period = kline_config.get("ema_period", 20)
        atr_config = self.config.get("atr", {})
        self.atr_period = atr_config.get("period", 14)

        # 手续费率
        self.fee_rate = trading_config.get("fee_rate", 0.0004)  # 默认 0.04%

        # 日期范围
        self.start_date = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc) if start_date else None
        self.end_date = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) if end_date else None

        # 数据存储
        self._all_klines: Dict[str, List[Dict]] = {}          # 每个币种的完整K线
        self._all_klines_4h: Dict[str, List[Dict]] = {}       # 每个币种的4h K线
        self._timeline: List[datetime] = []                    # 统一时间线
        self._timeline_index: Dict[datetime, int] = {}         # 时间线索引
        self._symbol_klines_index: Dict[str, Dict[datetime, int]] = {}  # 每个币种K线时间到索引的映射

        # 持仓跟踪
        # {symbol: {direction, entry_price, entry_time, quantity, atr, tp1_reached, tp2_reached,
        #            best_price, remaining_qty, entry_bar_index}}
        self._positions: Dict[str, Dict[str, Any]] = {}

        # 每日开仓计数
        self._daily_open_count: Dict[str, Dict[str, int]] = {}  # {date_str: {short: n, long: n}}

        # 风控状态
        self._consecutive_losses: int = 0
        self._blacklist: Set[str] = set()

        # 回测结果
        self.result = BacktestResult(initial_capital=initial_capital)

    # ============================================================
    # 数据加载
    # ============================================================

    def load_data(self) -> bool:
        """
        加载所有交易对的K线数据

        Returns:
            是否加载成功
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(script_dir, "data")

        print(f"\n📊 加载K线数据...")
        all_times = set()

        for symbol in self.symbols:
            csv_path = os.path.join(data_dir, f"{symbol.lower()}_1h.csv")
            klines = load_local_klines(csv_path)

            if not klines:
                print(f"  ❌ {symbol}: 未找到数据文件 {csv_path}")
                return False

            # 按日期范围过滤
            if self.start_date:
                start_ts = int(self.start_date.timestamp() * 1000)
                klines = [k for k in klines if k["open_time"] >= start_ts]
            if self.end_date:
                end_ts = int(self.end_date.timestamp() * 1000)
                klines = [k for k in klines if k["open_time"] <= end_ts]

            if len(klines) < self.min_klines:
                print(f"  ❌ {symbol}: K线数据不足 ({len(klines)} < {self.min_klines})")
                return False

            self._all_klines[symbol] = klines

            # 合成4h K线
            klines_4h = synthesize_4h_klines(klines)
            self._all_klines_4h[symbol] = klines_4h

            # 构建时间索引
            self._symbol_klines_index[symbol] = {}
            for i, k in enumerate(klines):
                t = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc)
                all_times.add(t)
                self._symbol_klines_index[symbol][t] = i

            print(f"  ✅ {symbol}: {len(klines)} 根1h K线, {len(klines_4h)} 根4h K线")

        # 构建统一时间线（排序）
        self._timeline = sorted(all_times)
        self._timeline_index = {t: i for i, t in enumerate(self._timeline)}

        print(f"\n  📅 时间范围: {self._timeline[0]} ~ {self._timeline[-1]}")
        print(f"  📊 总K线数: {len(self._timeline)}")
        return True

    # ============================================================
    # 主回测循环
    # ============================================================

    def run(self) -> BacktestResult:
        """
        执行回测主循环

        Returns:
            回测结果
        """
        print(f"\n{'=' * 70}")
        print(f"  HRS 策略 - 时序回测")
        print(f"{'=' * 70}")
        print(f"  交易对: {', '.join(self.symbols)}")
        print(f"  初始资金: {self.initial_capital:,.0f} USDT")
        print(f"  杠杆: {self.leverage}x")
        print(f"  手续费率: {self.fee_rate:.4%}")
        print()

        total_bars = len(self._timeline)
        last_progress = -1

        for bar_idx, current_time in enumerate(self._timeline):
            # 进度显示
            progress = int(bar_idx / total_bars * 100)
            if progress > last_progress and progress % 10 == 0:
                print(f"  进度: {progress}% ({bar_idx}/{total_bars})")
                last_progress = progress

            # 1. 监控已有持仓（止损/止盈检查）
            self._check_positions(current_time, bar_idx)

            # 2. 对每个币种执行分析
            for symbol in self.symbols:
                if symbol in self._blacklist:
                    continue
                if symbol in self._positions:
                    continue

                # 获取该币种在当前时间点之前的K线数据
                klines = self._get_klines_up_to(symbol, current_time)
                if len(klines) < self.min_klines:
                    continue

                # 检查候选池
                if not self._check_candidate_pool(symbol, klines, current_time):
                    continue

                # 形态检测和评分
                signal = self._analyze_and_score(symbol, klines, current_time)
                if signal:
                    # 执行开仓
                    self._open_position(symbol, signal, current_time, bar_idx)

        # 强制平仓所有剩余持仓
        self._close_all_positions(self._timeline[-1], len(self._timeline) - 1)

        # 计算绩效指标
        self._calculate_metrics()

        print(f"\n  ✅ 回测完成")
        print(f"  总交易次数: {self.result.total_trades}")
        print(f"  胜率: {self.result.win_rate:.1%}")
        print(f"  总收益率: {self.result.total_return:.1%}")
        print(f"  最大回撤: {self.result.max_drawdown_pct:.1%}")
        print(f"  夏普比率: {self.result.sharpe_ratio:.2f}")

        return self.result

    # ============================================================
    # 辅助方法
    # ============================================================

    def _get_klines_up_to(self, symbol: str, current_time: datetime) -> List[Dict]:
        """
        获取当前时间点之前的K线数据

        Args:
            symbol: 交易对
            current_time: 当前时间点

        Returns:
            K线列表
        """
        all_klines = self._all_klines.get(symbol, [])
        time_index = self._symbol_klines_index.get(symbol, {})

        # 找到当前时间之前的K线索引
        idx = time_index.get(current_time, -1)
        if idx < 0:
            # 时间点不在数据中，找最近的
            for t, i in sorted(time_index.items()):
                if t <= current_time:
                    idx = i
                else:
                    break

        if idx < 0:
            return []

        return all_klines[:idx + 1]

    def _check_candidate_pool(
        self,
        symbol: str,
        klines: List[Dict],
        current_time: datetime,
    ) -> bool:
        """
        检查币种是否满足候选池条件

        Args:
            symbol: 交易对
            klines: 当前K线数据
            current_time: 当前时间

        Returns:
            是否满足候选池条件
        """
        current_close = klines[-1]["close"]

        # 检查24h成交量
        recent_24 = klines[-24:]
        volume_24h = sum(k.get("quote_volume", 0) for k in recent_24)
        if volume_24h < self.min_volume_24h:
            return False

        # 计算24h涨跌幅
        if len(klines) >= 24:
            price_24h_ago = klines[-24]["close"]
            price_change_24h = (current_close - price_24h_ago) / price_24h_ago
        else:
            price_change_24h = 0

        # 计算EMA20(4h)偏离
        klines_4h = self._all_klines_4h.get(symbol, [])
        close_prices_4h = [k["close"] for k in klines_4h]
        ema20_4h = calc_ema(close_prices_4h, self.ema_period)
        deviation_4h = (current_close - ema20_4h) / ema20_4h if ema20_4h > 0 else 0

        # 简化：使用价格涨跌和EMA偏离作为近似候选条件
        # 做空候选：24h涨幅 >= 12%，EMA20偏离 >= 8%
        short_price_ok = price_change_24h >= self.short_config.get("price_change_24h", 0.12)
        short_ema_ok = deviation_4h >= self.short_config.get("ema20_deviation", 0.08)

        # 做多候选：24h跌幅 <= -10%，EMA20偏离 <= -6%
        long_price_ok = price_change_24h <= self.long_config.get("price_change_24h", -0.10)
        long_ema_ok = deviation_4h <= self.long_config.get("ema20_deviation", -0.06)

        # 任一方向满足候选条件即可
        return (short_price_ok and short_ema_ok) or (long_price_ok and long_ema_ok)

    def _analyze_and_score(
        self,
        symbol: str,
        klines: List[Dict],
        current_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        """
        执行形态检测和评分

        Args:
            symbol: 交易对
            klines: K线数据
            current_time: 当前时间

        Returns:
            交易信号字典，无信号返回 None
        """
        current_close = klines[-1]["close"]
        recent_klines = klines[-self.pattern.window_size:]

        # 计算24h涨跌幅用于判断方向
        if len(klines) >= 24:
            price_24h_ago = klines[-24]["close"]
            price_change_24h = (current_close - price_24h_ago) / price_24h_ago
        else:
            price_change_24h = 0

        # 计算EMA20(4h)偏离
        klines_4h = self._all_klines_4h.get(symbol, [])
        close_prices_4h = [k["close"] for k in klines_4h]
        ema20_4h = calc_ema(close_prices_4h, self.ema_period)
        deviation_4h = (current_close - ema20_4h) / ema20_4h if ema20_4h > 0 else 0

        # 计算ATR
        atr = calc_atr(klines, self.atr_period)
        if atr <= 0:
            return None

        # 模拟OI/市值比（使用波动率作为近似）
        oi_market_cap_ratio = atr / current_close

        # 模拟资金费率（使用价格偏离作为近似）
        funding_rate = deviation_4h / 100  # 近似

        # 做空方向分析
        short_candidate = (
            price_change_24h >= self.short_config.get("price_change_24h", 0.12)
            and deviation_4h >= self.short_config.get("ema20_deviation", 0.08)
        )
        # 做多方向分析
        long_candidate = (
            price_change_24h <= self.long_config.get("price_change_24h", -0.10)
            and deviation_4h <= self.long_config.get("ema20_deviation", -0.06)
        )

        best_signal = None
        best_score = 0

        # 做空评分
        if short_candidate:
            short_patterns = self.pattern.detect_short_patterns(recent_klines)
            short_result = self.scoring.score(
                symbol=symbol,
                direction="short",
                oi_market_cap_ratio=oi_market_cap_ratio,
                patterns=short_patterns,
                funding_rate=funding_rate,
                has_market_cap=True,
            )
            if self.scoring.should_entry(short_result) and short_result.total_score > best_score:
                best_score = short_result.total_score
                best_signal = {
                    "symbol": symbol,
                    "direction": "short",
                    "score": short_result.total_score,
                    "entry_price": current_close,
                    "atr": atr,
                    "score_result": short_result,
                }

        # 做多评分
        if long_candidate:
            long_patterns = self.pattern.detect_long_patterns(recent_klines)
            long_result = self.scoring.score(
                symbol=symbol,
                direction="long",
                oi_market_cap_ratio=oi_market_cap_ratio,
                patterns=long_patterns,
                funding_rate=funding_rate,
                has_market_cap=True,
            )
            if self.scoring.should_entry(long_result) and long_result.total_score > best_score:
                best_score = long_result.total_score
                best_signal = {
                    "symbol": symbol,
                    "direction": "long",
                    "score": long_result.total_score,
                    "entry_price": current_close,
                    "atr": atr,
                    "score_result": long_result,
                }

        return best_signal

    def _open_position(
        self,
        symbol: str,
        signal: Dict[str, Any],
        current_time: datetime,
        bar_idx: int,
    ) -> bool:
        """
        模拟开仓

        Args:
            symbol: 交易对
            signal: 交易信号
            current_time: 当前时间
            bar_idx: K线索引

        Returns:
            是否开仓成功
        """
        direction = signal["direction"]
        entry_price = signal["entry_price"]
        atr = signal["atr"]

        # 检查每日开仓限制
        date_str = current_time.strftime("%Y%m%d")
        if date_str not in self._daily_open_count:
            self._daily_open_count = {date_str: {"short": 0, "long": 0}}
        if date_str not in self._daily_open_count:
            self._daily_open_count[date_str] = {"short": 0, "long": 0}

        total_today = sum(self._daily_open_count[date_str].values())
        if total_today >= self.max_daily_positions:
            return False

        same_dir = self._daily_open_count[date_str].get(direction, 0)
        if same_dir >= self.max_daily_same_direction:
            return False

        # 计算仓位大小
        stop_loss_percent = self.stop_loss_atr_mult * atr / entry_price
        stop_loss_percent = max(stop_loss_percent, self.min_absolute_stop)
        max_loss = self.capital * self.max_loss_percent
        position_value = max_loss / stop_loss_percent
        quantity = position_value / entry_price

        if quantity <= 0:
            return False

        # 扣除手续费
        fee = position_value * self.fee_rate
        self.capital -= fee
        self.result.total_fees += fee

        # 记录持仓
        self._positions[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "entry_time": current_time,
            "quantity": quantity,
            "atr": atr,
            "entry_bar_index": bar_idx,
            "tp1_reached": False,
            "tp2_reached": False,
            "best_price": entry_price,
            "remaining_qty": quantity,
            "max_favorable": 0.0,
            "max_adverse": 0.0,
        }

        # 更新每日计数
        self._daily_open_count[date_str][direction] += 1

        return True

    def _check_positions(self, current_time: datetime, bar_idx: int) -> None:
        """
        检查所有持仓的止损止盈

        Args:
            current_time: 当前时间
            bar_idx: K线索引
        """
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            direction = pos["direction"]
            entry_price = pos["entry_price"]
            atr = pos["atr"]

            # 获取当前K线
            klines = self._get_klines_up_to(symbol, current_time)
            if not klines:
                continue

            current_bar = klines[-1]
            high = current_bar["high"]
            low = current_bar["low"]
            close = current_bar["close"]

            # 更新最佳价格
            if direction == "short":
                pos["best_price"] = min(pos["best_price"], low)
                current_pnl_pct = (entry_price - close) / entry_price
            else:
                pos["best_price"] = max(pos["best_price"], high)
                current_pnl_pct = (close - entry_price) / entry_price

            # 更新最大浮盈/浮亏
            pos["max_favorable"] = max(pos["max_favorable"], current_pnl_pct)
            pos["max_adverse"] = min(pos["max_adverse"], current_pnl_pct)

            # 检查止损
            exit_price = None
            exit_reason = None

            # 硬止损
            stop_loss_price = (
                entry_price * (1 + self.stop_loss_atr_mult * atr / entry_price)
                if direction == "short"
                else entry_price * (1 - self.stop_loss_atr_mult * atr / entry_price)
            )
            if direction == "short" and high >= stop_loss_price:
                exit_price = stop_loss_price
                exit_reason = "stop_loss"
            elif direction == "long" and low <= stop_loss_price:
                exit_price = stop_loss_price
                exit_reason = "stop_loss"

            # 时间止损
            if exit_price is None:
                holding_hours = (current_time - pos["entry_time"]).total_seconds() / 3600
                if holding_hours >= self.max_holding_hours and not pos["tp1_reached"]:
                    exit_price = close
                    exit_reason = "time_stop"

            # 第一目标止盈
            if exit_price is None and not pos["tp1_reached"]:
                tp1_price = (
                    entry_price * (1 - self.tp1_atr_mult * atr / entry_price)
                    if direction == "short"
                    else entry_price * (1 + self.tp1_atr_mult * atr / entry_price)
                )
                if direction == "short" and low <= tp1_price:
                    pos["tp1_reached"] = True
                    pos["remaining_qty"] *= (1 - self.tp1_close_pct)
                    # 部分平仓
                    pnl = (entry_price - tp1_price) * pos["quantity"] * self.tp1_close_pct
                    if direction == "long":
                        pnl = (tp1_price - entry_price) * pos["quantity"] * self.tp1_close_pct
                    fee = pos["quantity"] * self.tp1_close_pct * tp1_price * self.fee_rate
                    self.capital += pnl - fee
                    self.result.total_fees += fee

            # 第二目标止盈
            if exit_price is None and pos["tp1_reached"] and not pos["tp2_reached"]:
                tp2_price = (
                    entry_price * (1 - self.tp2_atr_mult * atr / entry_price)
                    if direction == "short"
                    else entry_price * (1 + self.tp2_atr_mult * atr / entry_price)
                )
                if direction == "short" and low <= tp2_price:
                    pos["tp2_reached"] = True
                    pos["remaining_qty"] *= (1 - self.tp2_close_pct)
                    pnl = (entry_price - tp2_price) * pos["quantity"] * self.tp2_close_pct * (1 - self.tp1_close_pct)
                    if direction == "long":
                        pnl = (tp2_price - entry_price) * pos["quantity"] * self.tp2_close_pct * (1 - self.tp1_close_pct)
                    fee = pos["quantity"] * self.tp2_close_pct * (1 - self.tp1_close_pct) * tp2_price * self.fee_rate
                    self.capital += pnl - fee
                    self.result.total_fees += fee

            # 移动止盈（第二目标达成后激活）
            if exit_price is None and pos["tp2_reached"]:
                trailing_threshold = atr * self.trailing_atr_mult
                if direction == "short":
                    bounce = high - pos["best_price"]
                    if bounce >= trailing_threshold:
                        exit_price = high
                        exit_reason = "trailing_stop"
                else:
                    drawdown = pos["best_price"] - low
                    if drawdown >= trailing_threshold:
                        exit_price = low
                        exit_reason = "trailing_stop"

            # 执行平仓
            if exit_price is not None:
                self._close_position(symbol, exit_price, exit_reason, current_time, bar_idx)

    def _close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        exit_time: datetime,
        bar_idx: int,
    ) -> None:
        """
        平仓并记录交易

        Args:
            symbol: 交易对
            exit_price: 平仓价格
            exit_reason: 平仓原因
            exit_time: 平仓时间
            bar_idx: K线索引
        """
        pos = self._positions.pop(symbol, None)
        if pos is None:
            return

        direction = pos["direction"]
        entry_price = pos["entry_price"]
        remaining_qty = pos["remaining_qty"]

        if remaining_qty <= 0:
            return

        # 计算盈亏
        if direction == "short":
            pnl = (entry_price - exit_price) * remaining_qty
            pnl_pct = (entry_price - exit_price) / entry_price
        else:
            pnl = (exit_price - entry_price) * remaining_qty
            pnl_pct = (exit_price - entry_price) / entry_price

        # 扣除手续费
        fee = remaining_qty * exit_price * self.fee_rate
        self.capital += pnl - fee
        self.result.total_fees += fee

        # 更新峰值资金
        if self.capital > self.peak_capital:
            self.peak_capital = self.capital

        # 记录交易
        holding_bars = bar_idx - pos["entry_bar_index"]
        trade = Trade(
            symbol=symbol,
            direction=direction,
            entry_time=pos["entry_time"],
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=remaining_qty,
            pnl=pnl,
            pnl_percent=pnl_pct,
            exit_reason=exit_reason,
            max_favorable=pos["max_favorable"],
            max_adverse=pos["max_adverse"],
            holding_bars=holding_bars,
        )
        self.result.trades.append(trade)

        # 更新连续亏损计数
        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.config.get("trading", {}).get("consecutive_loss", {}).get("max_count", 3):
                # 连续亏损，加入黑名单
                self._blacklist.add(symbol)
        else:
            self._consecutive_losses = 0

    def _close_all_positions(self, final_time: datetime, final_bar_idx: int) -> None:
        """
        强制平仓所有剩余持仓

        Args:
            final_time: 最终时间
            final_bar_idx: 最终K线索引
        """
        for symbol in list(self._positions.keys()):
            klines = self._get_klines_up_to(symbol, final_time)
            if klines:
                final_price = klines[-1]["close"]
            else:
                final_price = self._positions[symbol]["entry_price"]
            self._close_position(symbol, final_price, "force_close", final_time, final_bar_idx)

    # ============================================================
    # 绩效指标计算
    # ============================================================

    def _calculate_metrics(self) -> None:
        """计算回测绩效指标"""
        trades = self.result.trades

        self.result.total_trades = len(trades)
        if self.result.total_trades == 0:
            return

        # 胜率
        winning_trades = [t for t in trades if t.pnl > 0]
        losing_trades = [t for t in trades if t.pnl <= 0]
        self.result.winning_trades = len(winning_trades)
        self.result.losing_trades = len(losing_trades)
        self.result.win_rate = self.result.winning_trades / self.result.total_trades

        # 平均盈亏
        if winning_trades:
            self.result.avg_win = sum(t.pnl for t in winning_trades) / len(winning_trades)
        if losing_trades:
            self.result.avg_loss = sum(t.pnl for t in losing_trades) / len(losing_trades)

        # 盈亏比
        if self.result.avg_loss != 0:
            self.result.profit_factor = abs(self.result.avg_win / self.result.avg_loss) if self.result.avg_win else 0

        # 总收益率
        self.result.final_capital = self.capital
        self.result.total_return = (self.result.final_capital - self.result.initial_capital) / self.result.initial_capital

        # 最大回撤（基于 equity_curve）
        if self.result.equity_curve:
            peak = self.result.initial_capital
            max_dd = 0.0
            for point in self.result.equity_curve:
                equity = point["equity"]
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
            self.result.max_drawdown = max_dd * self.result.initial_capital
            self.result.max_drawdown_pct = max_dd

        # 平均持仓K线数
        self.result.avg_holding_bars = sum(t.holding_bars for t in trades) / len(trades)

        # 夏普比率
        if self.result.equity_curve and len(self.result.equity_curve) > 1:
            returns = []
            for i in range(1, len(self.result.equity_curve)):
                prev = self.result.equity_curve[i - 1]["equity"]
                curr = self.result.equity_curve[i]["equity"]
                if prev > 0:
                    returns.append((curr - prev) / prev)

            if returns:
                avg_return = np.mean(returns)
                std_return = np.std(returns)
                if std_return > 0:
                    # 年化夏普（假设每小时一根K线，一年8760小时）
                    self.result.sharpe_ratio = (avg_return / std_return) * math.sqrt(8760)

    def _record_equity(self, current_time: datetime) -> None:
        """
        记录权益曲线

        Args:
            current_time: 当前时间
        """
        # 计算当前持仓的浮动盈亏
        unrealized_pnl = 0.0
        for pos in self._positions.values():
            klines = self._get_klines_up_to(pos["symbol"], current_time) if "symbol" in pos else []
            # 简化：使用持仓的 best_price 近似
            # 实际应从当前K线获取最新价格
            pass

        self.result.equity_curve.append({
            "time": current_time,
            "equity": self.capital,
            "positions": len(self._positions),
        })

    # ============================================================
    # 报告生成
    # ============================================================

    def generate_report(self, output_path: Optional[str] = None) -> str:
        """
        生成 Markdown 格式回测报告

        Args:
            output_path: 输出路径，默认使用 backtest/hrs/reports/

        Returns:
            报告文件路径
        """
        if output_path is None:
            output_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "reports"
            )
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(output_dir, f"hrs_timeline_backtest_{timestamp}.md")

        lines = self._build_report()

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return output_path

    def _build_report(self) -> List[str]:
        """构建报告内容"""
        lines = []
        r = self.result

        lines.append("# HRS 策略 - 时序回测报告")
        lines.append("")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
        lines.append(f"**交易对**: {', '.join(self.symbols)}")
        lines.append("")

        # ========== 一、回测参数 ==========
        lines.append("## 一、回测参数")
        lines.append("")
        lines.append("| 参数 | 值 |")
        lines.append("|------|----|")
        lines.append(f"| 初始资金 | {r.initial_capital:,.0f} USDT |")
        lines.append(f"| 杠杆倍数 | {self.leverage}x |")
        lines.append(f"| 手续费率 | {self.fee_rate:.4%} |")
        lines.append(f"| 每笔最大亏损 | {self.max_loss_percent:.1%} |")
        lines.append(f"| 单日最大开仓 | {self.max_daily_positions} |")
        lines.append(f"| 单日同向最大 | {self.max_daily_same_direction} |")
        if self._timeline:
            lines.append(f"| 回测区间 | {self._timeline[0].strftime('%Y-%m-%d')} ~ {self._timeline[-1].strftime('%Y-%m-%d')} |")
            lines.append(f"| K线总数 | {len(self._timeline)} |")
        lines.append("")

        # ========== 二、绩效汇总 ==========
        lines.append("## 二、绩效汇总")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| 总交易次数 | {r.total_trades} |")
        lines.append(f"| 盈利次数 | {r.winning_trades} |")
        lines.append(f"| 亏损次数 | {r.losing_trades} |")
        lines.append(f"| 胜率 | {r.win_rate:.1%} |")
        lines.append(f"| 平均盈利 | {r.avg_win:,.2f} USDT |")
        lines.append(f"| 平均亏损 | {r.avg_loss:,.2f} USDT |")
        lines.append(f"| 盈亏比 | {r.profit_factor:.2f} |")
        lines.append(f"| 总收益率 | {r.total_return:+.1%} |")
        lines.append(f"| 最终资金 | {r.final_capital:,.0f} USDT |")
        lines.append(f"| 最大回撤 | {r.max_drawdown_pct:.1%} ({r.max_drawdown:,.0f} USDT) |")
        lines.append(f"| 夏普比率 | {r.sharpe_ratio:.2f} |")
        lines.append(f"| 平均持仓K线数 | {r.avg_holding_bars:.1f} |")
        lines.append(f"| 总手续费 | {r.total_fees:,.2f} USDT |")
        lines.append("")

        # ========== 三、交易明细 ==========
        lines.append("## 三、交易明细")
        lines.append("")
        if r.trades:
            lines.append(
                "| # | 交易对 | 方向 | 开仓时间 | 平仓时间 | 开仓价 | 平仓价 | "
                "盈亏(USDT) | 盈亏% | 原因 | 持仓K线 |"
            )
            lines.append(
                "|---|--------|------|----------|----------|--------|--------|"
                "-----------|-------|------|---------|"
            )
            for i, t in enumerate(r.trades, 1):
                pnl_str = f"+{t.pnl:,.2f}" if t.pnl > 0 else f"{t.pnl:,.2f}"
                pnl_pct_str = f"+{t.pnl_percent:.2%}" if t.pnl_percent > 0 else f"{t.pnl_percent:.2%}"
                dir_label = "做空" if t.direction == "short" else "做多"
                lines.append(
                    f"| {i} | {t.symbol} | {dir_label} | "
                    f"{t.entry_time.strftime('%m-%d %H:%M')} | "
                    f"{t.exit_time.strftime('%m-%d %H:%M')} | "
                    f"{t.entry_price:.6f} | {t.exit_price:.6f} | "
                    f"{pnl_str} | {pnl_pct_str} | {t.exit_reason} | {t.holding_bars} |"
                )
        else:
            lines.append("> 无交易记录")
        lines.append("")

        # ========== 四、平仓原因分布 ==========
        lines.append("## 四、平仓原因分布")
        lines.append("")
        if r.trades:
            reason_counts = defaultdict(int)
            for t in r.trades:
                reason_counts[t.exit_reason] += 1

            reason_labels = {
                "stop_loss": "止损",
                "time_stop": "时间止损",
                "take_profit_1": "第一目标止盈",
                "take_profit_2": "第二目标止盈",
                "trailing_stop": "移动止盈",
                "force_close": "强制平仓",
            }
            lines.append("| 原因 | 次数 | 占比 |")
            lines.append("|------|------|------|")
            for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
                label = reason_labels.get(reason, reason)
                lines.append(f"| {label} | {count} | {count / len(r.trades):.1%} |")
        lines.append("")

        # ========== 五、按币种统计 ==========
        lines.append("## 五、按币种统计")
        lines.append("")
        if r.trades:
            symbol_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for t in r.trades:
                s = symbol_stats[t.symbol]
                s["trades"] += 1
                if t.pnl > 0:
                    s["wins"] += 1
                s["pnl"] += t.pnl

            lines.append("| 交易对 | 交易次数 | 胜率 | 总盈亏(USDT) |")
            lines.append("|--------|----------|------|-------------|")
            for symbol, stats in sorted(symbol_stats.items()):
                win_rate = stats["wins"] / stats["trades"] if stats["trades"] > 0 else 0
                pnl_str = f"+{stats['pnl']:,.2f}" if stats["pnl"] > 0 else f"{stats['pnl']:,.2f}"
                lines.append(f"| {symbol} | {stats['trades']} | {win_rate:.1%} | {pnl_str} |")
        lines.append("")

        lines.append("---")
        lines.append("*报告由 HRS 时序回测系统自动生成*")

        return lines


# ============================================================
# 入口
# ============================================================

def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="HRS策略 - 完整时序回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 backtest/hrs/timeline_backtest.py --symbols LABUSDT --start 2026-01-01 --end 2026-06-01
  python3 backtest/hrs/timeline_backtest.py --symbols LABUSDT,SAHARAUSDT --start 2026-03-01 --end 2026-06-01 --capital 20000
        """,
    )
    parser.add_argument(
        "--symbols",
        default="LABUSDT",
        help="交易对列表，逗号分隔（默认: LABUSDT）",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="开始日期 YYYY-MM-DD（默认: 数据最早日期）",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="结束日期 YYYY-MM-DD（默认: 数据最晚日期）",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=10000.0,
        help="初始资金 USDT（默认: 10000）",
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("❌ 未指定有效交易对")
        sys.exit(1)

    # 创建回测引擎
    backtest = TimelineBacktest(
        symbols=symbols,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
    )

    # 加载数据
    if not backtest.load_data():
        print("❌ 数据加载失败")
        sys.exit(1)

    # 执行回测
    result = backtest.run()

    # 生成报告
    report_path = backtest.generate_report()
    print(f"\n📝 回测报告已生成: {report_path}")


if __name__ == "__main__":
    main()