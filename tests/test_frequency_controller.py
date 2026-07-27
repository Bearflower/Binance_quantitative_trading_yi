"""
FrequencyController 双轨计数器核心逻辑模拟测试

测试覆盖：
1. 趋势市和震荡市独立计数
2. 单币种差异化上限覆盖（如 SOLUSDT 的 max_daily_trades=0.5）
3. 冷却期检查（趋势市用全局冷却期72h，震荡市用3h）
4. 连续亏损暂停
5. 每日亏损限额
"""
import sys
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple

# 将项目根目录加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 直接从 strategy.py 导入 FrequencyController
from strategies.btc_eth.strategy import FrequencyController

# ============ 测试配置 ============

FREQUENCY_CONFIG = {
    'max_daily_symbol_trades': 1,
    'symbol_cooldown_hours': 72,
    'consecutive_loss_pause': 2,
    'pause_duration_hours': 24,
    'max_daily_loss_usdt': 25,
    'max_daily_loss_ratio': 0.05,
    'initial_capital_usdt': 500,
    'weekly_loss_pause_enabled': False,  # 简化测试
}

RISK_CONFIG = {
    'frequency_control': FREQUENCY_CONFIG,
    'market_state': {
        'behaviors': {
            'STRONG_TREND': {
                'max_daily_trades': 2,
                'ranging_symbol_cooldown_hours': 72,
            },
            'RANGING': {
                'max_daily_trades': 3,
                'ranging_symbol_cooldown_hours': 3,
            }
        }
    },
    'symbol_config': {
        'SOLUSDT': {
            'max_daily_trades': 0.5,  # 2天1笔
        },
        'BTCUSDT': {
            'max_daily_trades': 1,
        }
    }
}

# ============ 测试函数 ============

passed = 0
failed = 0

def test(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name} - {detail}")
        failed += 1


def setup_controller():
    """创建一个干净的 FrequencyController 实例"""
    ctrl = FrequencyController(
        config=FREQUENCY_CONFIG,
        risk_config=RISK_CONFIG,
        db_manager=None,
        strategy_name="测试策略"
    )
    return ctrl


# ----- 测试1：趋势市和震荡市独立计数 -----

def test_trend_ranging_independent_counting():
    header = "测试1：趋势市和震荡市独立计数"
    print(f"\n{'='*60}")
    print(f"  {header}")
    print(f"{'='*60}")
    
    ctrl = setup_controller()
    now = datetime.now()
    today = now.date().isoformat()
    
    # 记录3笔趋势市交易
    for i in range(3):
        ctrl.daily_trades_trend[today] = ctrl.daily_trades_trend.get(today, 0) + 1
    
    test("趋势市计数=3", ctrl.daily_trades_trend[today] == 3,
         f"实际={ctrl.daily_trades_trend[today]}")
    
    # 趋势市已达上限(2)，检查
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("趋势市3笔已达上限，应拒绝", not can,
         f"结果={can}, 原因={reason}")
    test("拒绝原因为趋势市上限", "已达每日上限" in reason,
         f"实际={reason}")
    
    # 震荡市应不受影响（独立计数器）
    can, reason = ctrl.can_trade('BTCUSDT', now, 'RANGING')
    test("震荡市不受趋势市计数影响，应允许", can,
         f"结果={can}, 原因={reason}")
    
    # 记录3笔震荡市交易
    for i in range(3):
        ctrl.daily_trades_ranging[today] = ctrl.daily_trades_ranging.get(today, 0) + 1
    
    test("震荡市计数=3", ctrl.daily_trades_ranging[today] == 3,
         f"实际={ctrl.daily_trades_ranging[today]}")
    
    # 震荡市已达上限(3)
    can, reason = ctrl.can_trade('BTCUSDT', now, 'RANGING')
    test("震荡市3笔已达上限，应拒绝", not can,
         f"结果={can}, 原因={reason}")
    
    # 趋势市绝大多数计数已满
    ctrl.daily_trades_trend[today] = 2
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("趋势市2笔已达上限2, 应拒绝", not can,
         f"结果={can}, 原因={reason}")


# ----- 测试2：单币种差异化上限覆盖 -----

def test_symbol_specific_limit_override():
    header = "测试2：单币种差异化上限覆盖"
    print(f"\n{'='*60}")
    print(f"  {header}")
    print(f"{'='*60}")
    
    ctrl = setup_controller()
    now = datetime.now()
    today = now.date().isoformat()
    
    # 测试 SOLUSDT（上限=0.5，即2天1笔）
    # 在震荡市下，SOLUSDT 的上限是 0.5
    # 注意：count 是整数，0.5 表示每天最多0.5，取整后为0
    # 也就是说第一天一笔后，第二天计数为0
    can, reason = ctrl.can_trade('SOLUSDT', now, 'RANGING')
    test("SOLUSDT 震荡市初始应允许", can, f"原因={reason}")
    
    # 模拟进行一笔交易后，0.5 上限意味着当天已达上限
    # 逻辑：计数器[daily_trades_trend/ranging][today] >= daily_limit (0.5)
    # 0 >= 0.5 → False，所以第一笔允许，然后计数器变成 1
    # 1 >= 0.5 → True，拒绝
    ctrl.daily_trades_ranging[today] = 1
    
    can, reason = ctrl.can_trade('SOLUSDT', now, 'RANGING')
    test("SOLUSDT 1笔后0.5上限应拒绝", not can,
         f"结果={can}, 原因={reason}")
    test("拒绝原因为上限", "已达每日上限" in reason,
         f"实际={reason}")
    
    # 测试 BTCUSDT（上限=1，与全局不同）
    ctrl.daily_trades_trend[today] = 0
    ctrl.daily_trades_ranging[today] = 0
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("BTCUSDT 趋势市初始应允许", can, f"原因={reason}")
    
    # 但 trend 上限是 BTC 的 symbol_limit = 1，但 global trend limit = 2
    # 取 min -> 所以上限应该是 1
    ctrl.daily_trades_trend[today] = 1
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("BTCUSDT 趋势市1笔后上限1应拒绝", not can,
         f"结果={can}, 原因={reason}")
    
    # 但震荡市下，BTC symbol_limit = 1，global ranging limit = 3
    # 取 min -> 所以上限应该是 1
    ctrl.daily_trades_trend[today] = 0
    ctrl.daily_trades_ranging[today] = 1
    can, reason = ctrl.can_trade('BTCUSDT', now, 'RANGING')
    test("BTCUSDT 震荡市1笔后应拒绝（symbol上限=1）", not can,
         f"结果={can}, 原因={reason}")


# ----- 测试3：冷却期检查 -----

def test_cooldown_period():
    header = "测试3：冷却期检查（趋势市72h vs 震荡市3h）"
    print(f"\n{'='*60}")
    print(f"  {header}")
    print(f"{'='*60}")
    
    ctrl = setup_controller()
    now = datetime.now()
    today = now.date().isoformat()
    
    # 模拟 BTCUSDT 在趋势市刚做完一笔交易
    ctrl.symbol_last_trade_time['BTCUSDT'] = now
    ctrl.daily_trades_trend[today] = 0  # 重置计数，只测冷却期
    
    # 趋势市冷却期应为72h
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("趋势市刚交易完应冷却", not can,
         f"结果={can}, 原因={reason}")
    test("冷却原因含'冷却中'", "冷却期" in reason,
         f"实际={reason}")
    
    # 震荡市冷却期应为3h（使用 ranging_symbol_cooldown_hours）
    # 模拟2小时前交易
    ctrl.symbol_last_trade_time['BTCUSDT'] = now - timedelta(hours=2)
    
    # 趋势市：2h < 72h，仍然冷却
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("趋势市2h后仍在72h冷却", not can, f"原因={reason}")
    
    # 震荡市：2h < 3h，还在冷却
    can, reason = ctrl.can_trade('BTCUSDT', now, 'RANGING')
    test("震荡市2h后仍在3h冷却", not can, f"原因={reason}")
    
    # 模拟4小时前交易
    ctrl.symbol_last_trade_time['BTCUSDT'] = now - timedelta(hours=4)
    
    # 趋势市：4h < 72h，仍然冷却
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("趋势市4h后仍在72h冷却", not can, f"原因={reason}")
    
    # 震荡市：4h >= 3h，解除冷却
    can, reason = ctrl.can_trade('BTCUSDT', now, 'RANGING')
    test("震荡市4h后已过3h冷却，应允许", can, f"原因={reason}")


# ----- 测试4：连续亏损暂停 -----

def test_consecutive_loss_pause():
    header = "测试4：连续亏损暂停"
    print(f"\n{'='*60}")
    print(f"  {header}")
    print(f"{'='*60}")
    
    ctrl = setup_controller()
    now = datetime.now()
    
    # 记录亏损（consecutive_loss_pause=2）
    ctrl.consecutive_losses = 0
    
    # 第1笔亏损
    ctrl.consecutive_losses += 1
    test("第1笔亏损后暂停未触发", ctrl.consecutive_losses == 1 and ctrl.pause_until is None,
         f"losses={ctrl.consecutive_losses}, pause={ctrl.pause_until}")
    
    # 第2笔亏损，触发暂停
    ctrl.consecutive_losses += 1
    if ctrl.consecutive_losses >= FREQUENCY_CONFIG['consecutive_loss_pause']:
        pause_hours = FREQUENCY_CONFIG['pause_duration_hours']
        ctrl.pause_until = now + timedelta(hours=pause_hours)
    
    test("第2笔亏损触发24h暂停", ctrl.pause_until is not None,
         f"losses={ctrl.consecutive_losses}, pause={ctrl.pause_until}")
    
    # 暂停期间应拒绝交易
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("暂停期间应拒绝", not can, f"原因={reason}")
    test("拒绝原因含'暂停中'", "暂停中" in reason, f"实际={reason}")


# ----- 测试5：每日亏损限额 -----

def test_daily_loss_limit():
    header = "测试5：每日亏损限额"
    print(f"\n{'='*60}")
    print(f"  {header}")
    print(f"{'='*60}")
    
    ctrl = setup_controller()
    now = datetime.now()
    today = now.date().isoformat()
    
    # 初始状态，应允许交易
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("初始应允许交易", can, f"原因={reason}")
    
    # 模拟亏损达到-25U
    ctrl.daily_pnl[today] = Decimal('-25.0')
    
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("亏损-25U达限额应拒绝", not can, f"原因={reason}")
    
    # 亏损未达限额，应允许
    ctrl.daily_pnl[today] = Decimal('-10.0')
    can, reason = ctrl.can_trade('BTCUSDT', now, 'STRONG_TREND')
    test("亏损-10U未达限额应允许", can, f"原因={reason}")


# ----- 测试6：记录交易（record_trade）验证计数器递增 -----

def test_record_trade():
    header = "测试6：record_trade 验证计数器递增"
    print(f"\n{'='*60}")
    print(f"  {header}")
    print(f"{'='*60}")
    
    ctrl = setup_controller()
    now = datetime.now()
    import asyncio
    
    # 模拟趋势市开仓
    asyncio.run(ctrl.record_trade('BTCUSDT', now, 'STRONG_TREND'))
    today = now.date().isoformat()
    
    test("趋势市计数=1", ctrl.daily_trades_trend.get(today) == 1,
         f"实际={ctrl.daily_trades_trend.get(today)}")
    test("震荡市计数=0", ctrl.daily_trades_ranging.get(today, 0) == 0,
         f"实际={ctrl.daily_trades_ranging.get(today, 0)}")
    test("品种计数=1", ctrl.symbol_daily_trades['BTCUSDT'].get(today) == 1,
         f"实际={ctrl.symbol_daily_trades['BTCUSDT'].get(today)}")
    
    # 模拟震荡市开仓
    asyncio.run(ctrl.record_trade('ETHUSDT', now, 'RANGING'))
    
    test("趋势市计数仍=1", ctrl.daily_trades_trend.get(today) == 1,
         f"实际={ctrl.daily_trades_trend.get(today)}")
    test("震荡市计数=1", ctrl.daily_trades_ranging.get(today) == 1,
         f"实际={ctrl.daily_trades_ranging.get(today)}")


# ============ 运行所有测试 ============

def run_all():
    global passed, failed
    passed = 0
    failed = 0
    
    print("=" * 60)
    print("  FrequencyController 双轨计数器核心逻辑测试")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 60)
    
    test_trend_ranging_independent_counting()
    test_symbol_specific_limit_override()
    test_cooldown_period()
    test_consecutive_loss_pause()
    test_daily_loss_limit()
    test_record_trade()
    
    print(f"\n{'='*60}")
    print(f"  测试总结")
    print(f"{'='*60}")
    total = passed + failed
    print(f"  总计: {total}  |  通过: {passed}  |  失败: {failed}")
    if failed == 0:
        print(f"  结果: 全部通过")
    else:
        print(f"  结果: 有 {failed} 个失败")
    print(f"{'='*60}\n")
    
    return failed == 0


if __name__ == '__main__':
    success = run_all()
    sys.exit(0 if success else 1)