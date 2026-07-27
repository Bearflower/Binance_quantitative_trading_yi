"""
测试 calculate_dynamic_grid_params() 方法的震荡市场三层防线修复

测试目标：验证震荡市场下上移/下移停止价与止盈止损价不再 fallback 到相同值，
而是显式独立计算，形成三层防线结构。
"""
import sys
import os
from decimal import Decimal
import yaml

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from strategies.grid.grid_calculator import GridCalculator, GridMode


def load_config():
    """加载网格策略配置"""
    config_path = os.path.join(
        os.path.dirname(__file__), '..', 'strategies', 'grid', 'config.yaml'
    )
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def run_test(test_name, condition, detail=""):
    """运行单个测试并报告结果"""
    if condition:
        print(f"  [通过] {test_name}")
        if detail:
            print(f"         详情: {detail}")
        return True
    else:
        print(f"  [失败] {test_name}")
        if detail:
            print(f"         详情: {detail}")
        return False


def main():
    config = load_config()
    calculator = GridCalculator(config)

    current_price = Decimal('3000')
    atr_smooth = Decimal('80')
    atr_baseline = Decimal('75')
    stop_loss_buffer = config['grid']['stop_loss_buffer']  # 2
    move_divisor = Decimal(str(config['grid']['oscillation_move_buffer_divisor']))  # 2

    all_passed = True
    total = 0
    passed = 0

    # ============================================================
    # 场景 1：震荡市场 - 验证 stop_move 与 stop_loss 独立
    # ============================================================
    print("\n" + "=" * 72)
    print("测试场景 1：震荡市场 - 验证三层防线价格独立")
    print("=" * 72)

    params = calculator.calculate_dynamic_grid_params(
        current_price=current_price,
        atr_smooth=atr_smooth,
        atr_baseline=atr_baseline,
        market_state='震荡市场',
        trend_strength=Decimal('0.3')
    )

    # 1a: 上移停止价不应等于止盈止损高价
    t = run_test(
        "1a: stop_move_up_price != stop_loss_high",
        params.stop_move_up_price != params.stop_loss_high,
        f"stop_move_up_price={params.stop_move_up_price}, stop_loss_high={params.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 1b: 下移停止价不应等于止盈止损低价
    t = run_test(
        "1b: stop_move_down_price != stop_loss_low",
        params.stop_move_down_price != params.stop_loss_low,
        f"stop_move_down_price={params.stop_move_down_price}, stop_loss_low={params.stop_loss_low}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 1c: 上移停止价应小于止盈止损高价（在边界和止盈止损价之间）
    t = run_test(
        "1c: upper_boundary < stop_move_up_price < stop_loss_high",
        params.upper_boundary < params.stop_move_up_price < params.stop_loss_high,
        f"upper_boundary={params.upper_boundary}, "
        f"stop_move_up_price={params.stop_move_up_price}, "
        f"stop_loss_high={params.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 1d: 下移停止价应大于止盈止损低价（在边界和止盈止损价之间）
    t = run_test(
        "1d: stop_loss_low < stop_move_down_price < lower_boundary",
        params.stop_loss_low < params.stop_move_down_price < params.lower_boundary,
        f"stop_loss_low={params.stop_loss_low}, "
        f"stop_move_down_price={params.stop_move_down_price}, "
        f"lower_boundary={params.lower_boundary}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # ============================================================
    # 场景 2：震荡市场 - 三层防线顺序验证（上下移价在边界和止盈止损价之间）
    # ============================================================
    print("\n" + "=" * 72)
    print("测试场景 2：震荡市场 - 三层防线顺序与数值验证")
    print("=" * 72)

    expected_move_buffer = Decimal(str(stop_loss_buffer)) / move_divisor
    expected_stop_move_up = params.upper_boundary + expected_move_buffer * atr_smooth
    expected_stop_move_down = params.lower_boundary - expected_move_buffer * atr_smooth
    expected_stop_loss_high = params.upper_boundary + Decimal(str(stop_loss_buffer)) * atr_smooth
    expected_stop_loss_low = params.lower_boundary - Decimal(str(stop_loss_buffer)) * atr_smooth

    # 2a: 止盈止损高价公式验证
    t = run_test(
        "2a: stop_loss_high = upper_boundary + stop_loss_buffer * ATR",
        abs(params.stop_loss_high - expected_stop_loss_high) < Decimal('0.01'),
        f"实际={params.stop_loss_high}, 期望={expected_stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 2b: 止盈止损低价公式验证
    t = run_test(
        "2b: stop_loss_low = lower_boundary - stop_loss_buffer * ATR",
        abs(params.stop_loss_low - expected_stop_loss_low) < Decimal('0.01'),
        f"实际={params.stop_loss_low}, 期望={expected_stop_loss_low}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 2c: 上移停止价公式验证
    t = run_test(
        "2c: stop_move_up_price = upper_boundary + (buffer/divisor) * ATR",
        abs(params.stop_move_up_price - expected_stop_move_up) < Decimal('0.01'),
        f"move_buffer={expected_move_buffer}, "
        f"实际={params.stop_move_up_price}, 期望={expected_stop_move_up}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 2d: 下移停止价公式验证
    t = run_test(
        "2d: stop_move_down_price = lower_boundary - (buffer/divisor) * ATR",
        abs(params.stop_move_down_price - expected_stop_move_down) < Decimal('0.01'),
        f"move_buffer={expected_move_buffer}, "
        f"实际={params.stop_move_down_price}, 期望={expected_stop_move_down}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 2e: 三层防线顺序（下方）
    t = run_test(
        "2e: stop_loss_low < stop_move_down_price < lower_boundary (三层防线下方)",
        params.stop_loss_low < params.stop_move_down_price < params.lower_boundary,
        f"stop_loss_low={params.stop_loss_low} < "
        f"stop_move_down={params.stop_move_down_price} < "
        f"lower_boundary={params.lower_boundary}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 2f: 三层防线顺序（上方）
    t = run_test(
        "2f: upper_boundary < stop_move_up_price < stop_loss_high (三层防线上方)",
        params.upper_boundary < params.stop_move_up_price < params.stop_loss_high,
        f"upper_boundary={params.upper_boundary} < "
        f"stop_move_up={params.stop_move_up_price} < "
        f"stop_loss_high={params.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # ============================================================
    # 场景 3：上升趋势 - stop_move_up_price 正常计算，stop_move_down_price fallback
    # ============================================================
    print("\n" + "=" * 72)
    print("测试场景 3：上升趋势 - 上移价正常计算，下移价 fallback")
    print("=" * 72)

    params_uptrend = calculator.calculate_dynamic_grid_params(
        current_price=current_price,
        atr_smooth=atr_smooth,
        atr_baseline=atr_baseline,
        market_state='上升趋势',
        trend_strength=Decimal('0.3')
    )

    # 3a: 上移停止价应由趋势强度公式计算
    expected_uptrend_move_up = params_uptrend.upper_boundary + Decimal('0.3') * atr_smooth
    t = run_test(
        "3a: stop_move_up_price = upper_boundary + trend_strength * ATR",
        abs(params_uptrend.stop_move_up_price - expected_uptrend_move_up) < Decimal('0.01'),
        f"实际={params_uptrend.stop_move_up_price}, 期望={expected_uptrend_move_up}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 3b: 下移停止价应 fallback 到止盈止损低价
    t = run_test(
        "3b: stop_move_down_price 应 fallback 到 stop_loss_low",
        params_uptrend.stop_move_down_price == params_uptrend.stop_loss_low,
        f"stop_move_down_price={params_uptrend.stop_move_down_price}, "
        f"stop_loss_low={params_uptrend.stop_loss_low}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 3c: 上移价与止盈止损高价的关系（上升趋势中上移价可能更接近）
    t = run_test(
        "3c: stop_move_up_price 应小于 stop_loss_high（上移价在边界和止损价之间）",
        params_uptrend.upper_boundary < params_uptrend.stop_move_up_price < params_uptrend.stop_loss_high,
        f"upper_boundary={params_uptrend.upper_boundary}, "
        f"stop_move_up_price={params_uptrend.stop_move_up_price}, "
        f"stop_loss_high={params_uptrend.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # ============================================================
    # 场景 4：下降趋势 - stop_move_down_price 正常计算，stop_move_up_price fallback
    # ============================================================
    print("\n" + "=" * 72)
    print("测试场景 4：下降趋势 - 下移价正常计算，上移价 fallback")
    print("=" * 72)

    params_downtrend = calculator.calculate_dynamic_grid_params(
        current_price=current_price,
        atr_smooth=atr_smooth,
        atr_baseline=atr_baseline,
        market_state='下降趋势',
        trend_strength=Decimal('0.3')
    )

    # 4a: 下移停止价应由趋势强度公式计算
    expected_downtrend_move_down = params_downtrend.lower_boundary - Decimal('0.3') * atr_smooth
    t = run_test(
        "4a: stop_move_down_price = lower_boundary - trend_strength * ATR",
        abs(params_downtrend.stop_move_down_price - expected_downtrend_move_down) < Decimal('0.01'),
        f"实际={params_downtrend.stop_move_down_price}, 期望={expected_downtrend_move_down}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 4b: 上移停止价应 fallback 到止盈止损高价
    t = run_test(
        "4b: stop_move_up_price 应 fallback 到 stop_loss_high",
        params_downtrend.stop_move_up_price == params_downtrend.stop_loss_high,
        f"stop_move_up_price={params_downtrend.stop_move_up_price}, "
        f"stop_loss_high={params_downtrend.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 4c: 下移价在边界和止损价之间
    t = run_test(
        "4c: stop_loss_low < stop_move_down_price < lower_boundary",
        params_downtrend.stop_loss_low < params_downtrend.stop_move_down_price < params_downtrend.lower_boundary,
        f"stop_loss_low={params_downtrend.stop_loss_low}, "
        f"stop_move_down_price={params_downtrend.stop_move_down_price}, "
        f"lower_boundary={params_downtrend.lower_boundary}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # ============================================================
    # 场景 5：边界情况 - trend_strength=0 时各状态行为
    # ============================================================
    print("\n" + "=" * 72)
    print("测试场景 5：趋势强度 trend_strength=0 时的行为")
    print("=" * 72)

    # 5a: 上升趋势 trend_strength=0
    params_up_zero = calculator.calculate_dynamic_grid_params(
        current_price=current_price,
        atr_smooth=atr_smooth,
        atr_baseline=atr_baseline,
        market_state='上升趋势',
        trend_strength=Decimal('0')
    )
    t = run_test(
        "5a: 上升趋势 trend_strength=0 时 stop_move_up_price 应 fallback 到 stop_loss_high",
        params_up_zero.stop_move_up_price == params_up_zero.stop_loss_high,
        f"stop_move_up_price={params_up_zero.stop_move_up_price}, "
        f"stop_loss_high={params_up_zero.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 5b: 上升趋势 trend_strength=0 时 stop_move_down_price 应 fallback
    t = run_test(
        "5b: 上升趋势 trend_strength=0 时 stop_move_down_price 应 fallback 到 stop_loss_low",
        params_up_zero.stop_move_down_price == params_up_zero.stop_loss_low,
        f"stop_move_down_price={params_up_zero.stop_move_down_price}, "
        f"stop_loss_low={params_up_zero.stop_loss_low}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 5c: 下降趋势 trend_strength=0
    params_down_zero = calculator.calculate_dynamic_grid_params(
        current_price=current_price,
        atr_smooth=atr_smooth,
        atr_baseline=atr_baseline,
        market_state='下降趋势',
        trend_strength=Decimal('0')
    )
    t = run_test(
        "5c: 下降趋势 trend_strength=0 时 stop_move_down_price 应 fallback 到 stop_loss_low",
        params_down_zero.stop_move_down_price == params_down_zero.stop_loss_low,
        f"stop_move_down_price={params_down_zero.stop_move_down_price}, "
        f"stop_loss_low={params_down_zero.stop_loss_low}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 5d: 下降趋势 trend_strength=0 时 stop_move_up_price 应 fallback
    t = run_test(
        "5d: 下降趋势 trend_strength=0 时 stop_move_up_price 应 fallback 到 stop_loss_high",
        params_down_zero.stop_move_up_price == params_down_zero.stop_loss_high,
        f"stop_move_up_price={params_down_zero.stop_move_up_price}, "
        f"stop_loss_high={params_down_zero.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # 5e: 震荡市场 trend_strength=0 仍应正常计算三层防线
    params_osc_zero = calculator.calculate_dynamic_grid_params(
        current_price=current_price,
        atr_smooth=atr_smooth,
        atr_baseline=atr_baseline,
        market_state='震荡市场',
        trend_strength=Decimal('0')
    )
    t = run_test(
        "5e: 震荡市场 trend_strength=0 时仍应独立计算三层防线 (stop_move_up != stop_loss_high)",
        params_osc_zero.stop_move_up_price != params_osc_zero.stop_loss_high,
        f"stop_move_up={params_osc_zero.stop_move_up_price}, stop_loss_high={params_osc_zero.stop_loss_high}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    t = run_test(
        "5f: 震荡市场 trend_strength=0 时仍应独立计算三层防线 (stop_move_down != stop_loss_low)",
        params_osc_zero.stop_move_down_price != params_osc_zero.stop_loss_low,
        f"stop_move_down={params_osc_zero.stop_move_down_price}, stop_loss_low={params_osc_zero.stop_loss_low}"
    )
    total += 1; passed += int(t); all_passed = all_passed and t

    # ============================================================
    # 汇总结果
    # ============================================================
    print("\n" + "=" * 72)
    print("测试结果汇总")
    print("=" * 72)
    print(f"总计: {total} 个测试")
    print(f"通过: {passed} 个")
    print(f"失败: {total - passed} 个")
    print(f"通过率: {passed / total * 100:.2f}%")
    print(f"整体结果: {'全部通过' if all_passed else '存在失败用例'}")

    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())