"""
震荡市量价确认逻辑测试（v6.28 半区校验）

覆盖：
- LONG 在布林带下半区（close < middle）+ 缩量 → 通过
- LONG 在布林带上半区（close > middle）→ 拒绝
- SHORT 在布林带上半区（close > middle）+ 缩量 → 通过
- SHORT 在布林带下半区（close < middle）→ 拒绝
- 放量（volume >= shrink_ratio × vol_ma）→ 拒绝
- 数据缺失（close/volume/BB_Middle/Volume_MA 任一缺失）→ 保守拒绝
- volume_confirm.enabled=false → 直接通过
"""
import sys
import os
import pytest
import pandas as pd

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from strategies.btc_eth.strategy import BTCEthStrategy


# ============================================================================
# 辅助函数：构造 _check_volume_confirm 所需的入参
# ============================================================================

def make_klines(close, volume):
    """
    构造 4h K 线字典（含最后一行 close/volume）

    Args:
        close: 最新收盘价（可为 None 表示缺失该字段）
        volume: 最新成交量（可为 None 表示缺失该字段）

    Returns:
        klines 字典，形如 {'4h': [{...}]}
    """
    row = {}
    if close is not None:
        row['close'] = close
    if volume is not None:
        row['volume'] = volume
    return {'4h': [row]}


def make_indicators(bb_middle=None, vol_ma=None):
    """
    构造指标字典（含 4h 的 BB_Middle / Volume_MA）

    Args:
        bb_middle: 布林中轨值（None 表示缺失字段）
        vol_ma: 成交量均线值（None 表示缺失字段）

    Returns:
        indicators 字典，形如 {'4h': {field: pd.Series}}
    """
    fields = {}
    if bb_middle is not None:
        fields['BB_Middle'] = pd.Series([bb_middle])
    if vol_ma is not None:
        fields['Volume_MA'] = pd.Series([vol_ma])
    return {'4h': fields}


def make_config(enabled=True, shrink_ratio=1.0):
    """
    构造量价确认配置节

    Args:
        enabled: 是否启用量价确认
        shrink_ratio: 缩量倍率

    Returns:
        ranging_config 字典
    """
    return {
        'volume_confirm': {
            'enabled': enabled,
            'shrink_ratio': shrink_ratio,
        }
    }


def call_volume_confirm(direction, klines, indicators, config):
    """
    静态调用 _check_volume_confirm 并返回 (通过与否, 拒绝原因)

    Args:
        direction: 信号方向（'LONG' / 'SHORT'）
        klines: K 线字典
        indicators: 指标字典
        config: 震荡市配置字典

    Returns:
        (bool, str) 元组
    """
    return BTCEthStrategy._check_volume_confirm(direction, indicators, klines, config)


# ============================================================================
# 正常场景：半区校验 + 缩量
# ============================================================================

class TestLongInLowerHalf:
    """LONG 在布林带下半区（close < middle）+ 缩量 → 通过"""

    def test_long_lower_half_shrink_pass(self):
        """做多：收盘价处于下半区且缩量，应通过"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=95.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is True
        assert reason == ""

    def test_long_middle_touch_reject(self):
        """做多：收盘价恰好等于中轨（不属于下半区）→ 拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=100.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "下半区" in reason


class TestLongInUpperHalf:
    """LONG 在布林带上半区（close > middle）→ 拒绝"""

    def test_long_upper_half_reject(self):
        """做多：收盘价处于上半区（接近上轨），应拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=105.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "下半区" in reason


class TestShortInUpperHalf:
    """SHORT 在布林带上半区（close > middle）+ 缩量 → 通过"""

    def test_short_upper_half_shrink_pass(self):
        """做空：收盘价处于上半区且缩量，应通过"""
        ok, reason = call_volume_confirm(
            'SHORT',
            make_klines(close=105.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is True
        assert reason == ""

    def test_short_middle_touch_reject(self):
        """做空：收盘价恰好等于中轨（不属于上半区）→ 拒绝"""
        ok, reason = call_volume_confirm(
            'SHORT',
            make_klines(close=100.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "上半区" in reason


class TestShortInLowerHalf:
    """SHORT 在布林带下半区（close < middle）→ 拒绝"""

    def test_short_lower_half_reject(self):
        """做空：收盘价处于下半区（接近下轨），应拒绝"""
        ok, reason = call_volume_confirm(
            'SHORT',
            make_klines(close=95.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "上半区" in reason


# ============================================================================
# 缩量校验：放量拒绝
# ============================================================================

class TestVolumeShrink:
    """放量（volume >= shrink_ratio × vol_ma）→ 拒绝"""

    def test_volume_equal_threshold_reject(self):
        """成交量等于阈值（shrink_ratio × vol_ma）→ 拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=95.0, volume=100.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "未缩量" in reason

    def test_volume_over_threshold_reject(self):
        """成交量超过阈值 → 拒绝"""
        ok, reason = call_volume_confirm(
            'SHORT',
            make_klines(close=105.0, volume=120.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "未缩量" in reason

    def test_shrink_ratio_11_tolerance(self):
        """shrink_ratio=1.1 时略微放量（volume=105 < 1.1×100）→ 通过"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=95.0, volume=105.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(shrink_ratio=1.1),
        )
        assert ok is True
        assert reason == ""


# ============================================================================
# 数据缺失：保守拒绝
# ============================================================================

class TestMissingData:
    """数据缺失（close/volume/BB_Middle/Volume_MA 任一缺失）→ 保守拒绝"""

    def test_close_field_missing_reject(self):
        """close 字段缺失 → 拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=None, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "量价数据缺失" in reason

    def test_volume_field_missing_reject(self):
        """volume 字段缺失 → 拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=95.0, volume=None),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "量价数据缺失" in reason

    def test_close_nan_reject(self):
        """close 为 NaN → 拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=float('nan'), volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "量价数据缺失" in reason

    def test_bb_middle_missing_reject(self):
        """BB_Middle 指标缺失 → 拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=95.0, volume=50.0),
            make_indicators(bb_middle=None, vol_ma=100.0),
            make_config(),
        )
        assert ok is False
        assert "量价数据缺失" in reason

    def test_vol_ma_missing_reject(self):
        """Volume_MA 指标缺失 → 拒绝"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=95.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=None),
            make_config(),
        )
        assert ok is False
        assert "量价数据缺失" in reason


# ============================================================================
# 开关控制：enabled=false 直接通过
# ============================================================================

class TestDisabled:
    """volume_confirm.enabled=false → 直接通过（不做任何校验）"""

    def test_disabled_pass_even_missing_data(self):
        """关闭量价确认后，即使数据缺失也直接通过"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=None, volume=None),
            make_indicators(bb_middle=None, vol_ma=None),
            make_config(enabled=False),
        )
        assert ok is True
        assert reason == ""

    def test_disabled_pass_even_wrong_half(self):
        """关闭量价确认后，即使方向与半区不匹配也直接通过"""
        ok, reason = call_volume_confirm(
            'LONG',
            make_klines(close=105.0, volume=50.0),
            make_indicators(bb_middle=100.0, vol_ma=100.0),
            make_config(enabled=False),
        )
        assert ok is True
        assert reason == ""


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
