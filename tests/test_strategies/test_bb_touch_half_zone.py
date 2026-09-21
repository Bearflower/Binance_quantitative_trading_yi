"""
BB 半区投票测试（v6.29 由触轨判定改为半区判定）

覆盖：
- 下半区（close < middle）→ 多票 1、空票 0
- 上半区（close > middle）→ 多票 0、空票 1
- 恰好等于中轨 → 平票 (0, 0)
- bb_touch.enabled=false → (0, 0, [], 1.0, 1.0)
- BB_Middle 缺失 → 0 票
- BB_Upper / BB_Lower 缺失 → 0 票
- df 为空 → 0 票
- close 为 NaN → 0 票
- dist_to_lower / dist_to_upper 诊断值正确
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
# 辅助函数：构造 _bb_touch_votes 所需的入参
# ============================================================================

def make_df(close):
    """
    构造 4h K 线 DataFrame（含最后一行 close）

    Args:
        close: 最新收盘价（可为 float 或 NaN）

    Returns:
        pd.DataFrame，形如 {'close': [...]}
    """
    return pd.DataFrame({'close': [close]})


def make_indicators(bb_upper=110.0, bb_middle=100.0, bb_lower=90.0):
    """
    构造指标字典（含 4h 的 BB_Upper / BB_Middle / BB_Lower）

    Args:
        bb_upper: 布林上轨值（None 表示缺失字段）
        bb_middle: 布林中轨值（None 表示缺失字段）
        bb_lower: 布林下轨值（None 表示缺失字段）

    Returns:
        indicators 字典，形如 {'4h': {field: pd.Series}}
    """
    fields = {}
    if bb_upper is not None:
        fields['BB_Upper'] = pd.Series([bb_upper])
    if bb_middle is not None:
        fields['BB_Middle'] = pd.Series([bb_middle])
    if bb_lower is not None:
        fields['BB_Lower'] = pd.Series([bb_lower])
    return {'4h': fields}


def make_config(enabled=True):
    """
    构造 bb_touch 入场条件配置节

    Args:
        enabled: 是否启用 bb_touch 判定

    Returns:
        entry_conditions 字典
    """
    return {'bb_touch': enabled}


# ============================================================================
# BB 半区投票测试
# ============================================================================

class TestBBTouchHalfZone:
    """测试 _bb_touch_votes 的半区判定逻辑。"""

    def test_lower_half_zone_gives_long_vote(self):
        """下半区（close < middle）投多票 1、空票 0。"""
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            make_df(95.0), make_indicators(), make_config())
        assert ld == 1
        assert sd == 0
        assert any('下半区' in c for c in conds)

    def test_upper_half_zone_gives_short_vote(self):
        """上半区（close > middle）投多票 0、空票 1。"""
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            make_df(105.0), make_indicators(), make_config())
        assert ld == 0
        assert sd == 1
        assert any('上半区' in c for c in conds)

    def test_exact_middle_returns_no_votes(self):
        """恰好等于中轨 → 平票 (0, 0)。"""
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            make_df(100.0), make_indicators(), make_config())
        assert ld == 0
        assert sd == 0
        assert conds == []

    def test_disabled_returns_no_votes(self):
        """bb_touch.enabled=false → (0, 0, [], 1.0, 1.0)。"""
        result = BTCEthStrategy._bb_touch_votes(
            make_df(95.0), make_indicators(), make_config(enabled=False))
        assert result == (0, 0, [], 1.0, 1.0)

    def test_missing_middle_returns_no_votes(self):
        """BB_Middle 缺失 → 0 票。"""
        indicators = make_indicators()
        del indicators['4h']['BB_Middle']
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            make_df(95.0), indicators, make_config())
        assert (ld, sd, conds) == (0, 0, [])

    def test_missing_upper_lower_returns_no_votes(self):
        """BB_Upper / BB_Lower 缺失 → 0 票。"""
        indicators = make_indicators(bb_upper=None, bb_lower=None)
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            make_df(95.0), indicators, make_config())
        assert (ld, sd, conds) == (0, 0, [])

    def test_empty_df_returns_no_votes(self):
        """df 为空 → 0 票。"""
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            pd.DataFrame(), make_indicators(), make_config())
        assert (ld, sd, conds) == (0, 0, [])

    def test_nan_close_returns_no_votes(self):
        """close 为 NaN → 0 票。"""
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            make_df(float('nan')), make_indicators(), make_config())
        assert (ld, sd, conds) == (0, 0, [])

    def test_dist_diagnostics_values(self):
        """dist_to_lower / dist_to_upper 诊断值正确。

        构造 close=95、lower=90、upper=110、middle=100：
        bb_range=20，dist_to_lower=(95-90)/20=0.25，dist_to_upper=(110-95)/20=0.75。
        """
        _, _, _, dist_to_lower, dist_to_upper = BTCEthStrategy._bb_touch_votes(
            make_df(95.0), make_indicators(), make_config())
        assert dist_to_lower == pytest.approx(0.25)
        assert dist_to_upper == pytest.approx(0.75)
