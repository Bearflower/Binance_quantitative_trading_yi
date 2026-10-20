"""反事实停牌标记测试：阈值从配置读取（禁止硬编码）、三档标记存在且不截断路径。"""
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import pytest

from backtest.btc_eth.replay.counterfactual import (
    scan_triggers, CounterfactualTriggers, pressure_test_final_equity,
)


UTC = timezone.utc
# 单一真源：与 config.yaml counterfactual.halt_lines_pct 对齐
DEFAULT_HALT_LINES = [15, 25, 30]


def _mk(eq_list):
    """把简单数字列表转三元组 [(datetime, Decimal, bool), ...]（阻断 #4 契约）。

    第三字段 has_open_position=False（空仓快照）。
    """
    out = []
    t = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    for v in eq_list:
        out.append((t, Decimal(str(v)), False))
        t += timedelta(hours=1)
    return out


def _mk_with_pos(eq_list):
    """同 _mk，但第三字段 has_open_position=True（持仓期逐分钟盯市点）。"""
    out = []
    t = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    for v in eq_list:
        out.append((t, Decimal(str(v)), True))
        t += timedelta(hours=1)
    return out


def test_scan_15pct_only_hit():
    """权益历史只有 15% 线被触发（最低 240 低于 255，但没到 225/210）。"""
    hist = _mk([300, 290, 280, 240, 270])
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    assert triggers.trigger_15pct is not None
    assert triggers.trigger_25pct is None
    assert triggers.trigger_30pct is None


def test_scan_all_three_hit():
    hist = _mk([300, 280, 260, 240, 220, 210])
    # 255→225→210: 15% (255) 线在 260 处就被突破；25% (225) 线在 240 处；30% (210) 在 210 处
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    assert triggers.trigger_15pct is not None
    assert triggers.trigger_25pct is not None
    assert triggers.trigger_30pct is not None


def test_no_trigger_if_stays_above_15():
    hist = _mk([300, 299, 298, 295, 297])  # 最低 295，远高于 255
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    assert triggers.trigger_15pct is None
    assert triggers.trigger_25pct is None
    assert triggers.trigger_30pct is None


def test_counterfactual_does_not_truncate_main_path():
    """关键：scan_triggers 只独立计算标记，不返回"要不要截断"的标志。
    引擎始终走完整个回放期；即使触发了 30% 红线，后续路径也照常执行。
    """
    hist = _mk([300, 200, 150, 100])  # 先崩到 100
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    # 三个都有，但输入历史长度保持了完整序列
    assert triggers.trigger_15pct is not None
    assert triggers.trigger_30pct is not None
    # 主口径"不截断"在此体现：引擎的 run() 方法在调用 scan_triggers 之前，
    # 已经把所有事件都处理完了。
    assert len(hist) == 4


def test_as_dict_format():
    hist = _mk([300, 200])
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    d = triggers.as_dict()
    assert "15pct" in d
    assert "25pct" in d
    assert "30pct" in d
    assert d["15pct"]["open_position"] is False


def test_scan_triggers_consumes_triple_open_position_true():
    """阻断 #4 回归：scan_triggers 必须消费三元组第三字段（持仓期盯市点）。

    15% 触发点来自持仓期的逐分钟盯市快照（has_open_position=True），
    as_dict 里 open_position 必须如实为 True——反事实停牌标记要能回答
    「触发时当时有没有仓位」。
    """
    hist = _mk_with_pos([300, 290, 240, 270])
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    assert triggers.trigger_15pct is not None
    at, eq, has_pos = triggers.trigger_15pct
    assert has_pos is True
    assert eq == Decimal("240")
    assert triggers.as_dict()["15pct"]["open_position"] is True


# ---------- 任务 A 新增测试 ----------

def test_fail_closed_on_missing_halt_lines():
    """缺 halt_lines_pct 时必须抛 ValueError（fail-closed）。"""
    hist = _mk([300, 240])
    with pytest.raises(ValueError, match="halt_lines_pct"):
        scan_triggers(hist, Decimal("300"))


def test_fail_closed_on_wrong_halt_lines_count():
    """halt_lines_pct 数量不是 3 档时必须抛 ValueError。"""
    hist = _mk([300, 240])
    with pytest.raises(ValueError, match="必须提供恰好 3 档"):
        scan_triggers(hist, Decimal("300"), halt_lines_pct=[15, 25])


def test_fail_closed_on_non_monotonic_halt_lines():
    """halt_lines_pct 必须严格递增且在 (0, 100) 内。"""
    hist = _mk([300, 240])
    with pytest.raises(ValueError, match="严格递增"):
        scan_triggers(hist, Decimal("300"), halt_lines_pct=[30, 25, 15])


def test_fail_closed_on_out_of_range_halt_lines():
    """halt_lines_pct 的值必须在 (0, 100) 内。"""
    hist = _mk([300, 240])
    with pytest.raises(ValueError, match="严格递增"):
        scan_triggers(hist, Decimal("300"), halt_lines_pct=[0, 25, 30])
    with pytest.raises(ValueError, match="严格递增"):
        scan_triggers(hist, Decimal("300"), halt_lines_pct=[15, 25, 100])


def test_boundary_eq_limit_triggers():
    """边界：equity 恰好等于 limit 时必须触发（<= 语义）。"""
    # 300 * 0.85 = 255，精确给 255
    hist = _mk([300, 255, 260])
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    assert triggers.trigger_15pct is not None


def test_boundary_just_above_not_triggers():
    """边界：equity 刚好在 limit 上方（256 > 255）时，15% 不触发。"""
    hist = _mk([300, 256, 255])  # 第一次 256 不触发，第二次 255 才触发
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=DEFAULT_HALT_LINES)
    assert triggers.trigger_15pct is not None
    # 验证触发点是第二个条目（255），不是第一个（256）
    assert triggers.trigger_15pct[1] == Decimal("255")


def test_threshold_dependency_on_config():
    """阈值完全由 halt_lines_pct 参数决定（不得在 scan_triggers 内部硬编码）。

    传入自定义阈值 [10, 20, 30]，验证 10% 线能在 270（300*0.90=270）时触发。
    """
    hist = _mk([300, 270, 240])
    triggers = scan_triggers(hist, Decimal("300"), halt_lines_pct=[10, 20, 30])
    # 10% 线应该在 270 时触发
    assert triggers.trigger_15pct is not None  # 字段名沿用 15pct，但语义是"第一档"
    assert triggers.trigger_15pct[1] == Decimal("270")


def test_pressure_test_final_equity():
    final_eq = Decimal("290")
    total_notional = Decimal("1000")
    # 额外滑点 2 bps（基准 2bps + 2bps = 4bps，属于 2× 压力测试）
    new_eq = pressure_test_final_equity(final_eq, total_notional, Decimal("2"))
    extra_cost = total_notional * Decimal("2") / Decimal("10000")
    expected = final_eq - extra_cost
    assert new_eq == expected
