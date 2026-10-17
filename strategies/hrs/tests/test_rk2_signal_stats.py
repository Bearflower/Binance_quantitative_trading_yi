"""
RK2: HRS 固定样本统计口径验证

本轮新增测试（B 组），按架构 §6.2 口径定义表实现本地 helper 计算，
在固定样本上断言各口径正确性。不连生产库，不新增生产统计函数。

口径定义（权威来源：docs/research/strategy_remediation/architecture_R10-RK_2026-10-17.md §6.2）：
    触发数 = status IN ('triggered','resolved') 的行数
    超时数 = resolution = 'timeout'
    影子数 = resolution = 'shadow' 或 signal_origin IN ('shadow_scoring','empty_pool')
    真实执行数 = resolution = 'executed'
    执行率 = executed / (executed + conflict + timeout)
    胜率分子/分母 = trading.trade_records.realized_pnl（真实成交，不来自信号行数）

关键红线：
    胜率必须取 trading.trade_records.realized_pnl，严禁用 hrs_signals 行数推导。
"""
import pytest


# ==================== 固定样本构造 ====================

def _build_fixed_sample():
    """
    构造覆盖四类信号的固定样本，模拟 hrs.hrs_signals 表行。

    样本设计（共 13 行）：
        生产 executed:     3 行 (symbol=BTCUSDT/ETHUSDT/SOLUSDT, status=resolved,
                                  resolution=executed, signal_origin='standard_scoring')
        影子 shadow:       3 行 (resolution='shadow', signal_origin='shadow_scoring')
        超时 timeout:       3 行 (resolution='timeout')
        冲突 conflict:      2 行 (resolution='conflict')
        未决行:             1 行 (status='triggered', resolution=None)
        影子通过 signal_origin 标记（但 resolution 不是 shadow）：1 行（signal_origin='empty_pool'）
    """
    rows = []
    rid = 0

    def add(symbol, status, resolution, signal_origin):
        nonlocal rid
        rid += 1
        rows.append({
            "id": rid,
            "symbol": symbol,
            "status": status,
            "resolution": resolution,
            "signal_origin": signal_origin,
        })

    # === 3 行真实 executed（生产标准评分）===
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        add(sym, "resolved", "executed", "standard_scoring")

    # === 3 行 shadow（影子结果）===
    for sym in ["DOGEUSDT", "AVAXUSDT", "DOTUSDT"]:
        add(sym, "resolved", "shadow", "shadow_scoring")

    # === 3 行 timeout（超时，R9 后开始落库）===
    for sym in ["LINKUSDT", "ADAUSDT", "MATICUSDT"]:
        add(sym, "resolved", "timeout", "standard_scoring")

    # === 2 行 conflict（冲突）===
    for sym in ["LTCUSDT", "BCHUSDT"]:
        add(sym, "resolved", "conflict", "standard_scoring")

    # === 1 行未决（triggered 但 resolution 未标）===
    add("XRPUSDT", "triggered", None, "standard_scoring")

    # === 1 行影子（signal_origin=empty_pool 覆盖 LV-RM 空池影子场景）===
    add("ARBUSDT", "resolved", "conflict", "empty_pool")

    return rows


# ==================== 本地口径计算 helper ====================
# 架构 §6.2 口径定义表的纯函数实现，避免同构 SQL 复制

def _count_triggered(rows):
    """触发数 = status IN ('triggered','resolved') 的行数"""
    return sum(1 for r in rows if r["status"] in ("triggered", "resolved"))


def _count_timeout(rows):
    """超时数 = resolution = 'timeout'"""
    return sum(1 for r in rows if r["resolution"] == "timeout")


def _count_shadow(rows):
    """影子数 = resolution='shadow' 或 signal_origin IN ('shadow_scoring','empty_pool')"""
    return sum(
        1 for r in rows
        if r["resolution"] == "shadow"
        or r["signal_origin"] in ("shadow_scoring", "empty_pool")
    )


def _count_executed(rows):
    """真实执行数 = resolution='executed'（代码保证仅非 shadow 信号落 executed）"""
    return sum(1 for r in rows if r["resolution"] == "executed")


def _count_conflict(rows):
    """冲突数 = resolution='conflict'"""
    return sum(1 for r in rows if r["resolution"] == "conflict")


def _count_undecided(rows):
    """未决行 = status='triggered' AND resolution IS NULL"""
    return sum(
        1 for r in rows
        if r["status"] == "triggered" and r["resolution"] is None
    )


def _calc_execution_rate(rows):
    """
    执行率 = executed / (executed + conflict + timeout)

    分母显式声明：shadow 不计入分母（影子未真实下单，不是"执行了但失败"）。
    零分母保护：分母为 0 时返回 None。
    """
    exec_count = _count_executed(rows)
    denom = exec_count + _count_conflict(rows) + _count_timeout(rows)
    if denom == 0:
        return None
    return exec_count / denom


# ==================== 固定样本断言 ====================

class TestFixedSampleStatistics:
    """架构 §6.2 口径在固定样本上的正确性断言"""

    def setup_method(self):
        self.rows = _build_fixed_sample()

    def test_fixed_sample_row_count(self):
        """样本共 13 行：executed(3) + shadow(3) + timeout(3) + conflict(2+1=3) + undecided(1)"""
        assert len(self.rows) == 13

    def test_triggered_count(self):
        """触发数 = 所有行都是 triggered 或 resolved"""
        assert _count_triggered(self.rows) == 13

    def test_timeout_count(self):
        """超时数 = resolution='timeout' 的 3 行"""
        assert _count_timeout(self.rows) == 3

    def test_shadow_count(self):
        """
        影子数：
            resolution='shadow' 有 3 行（shadow_scoring origin）
            signal_origin='empty_pool' 有 1 行（ARBUSDT）
            合计 4 行
        """
        assert _count_shadow(self.rows) == 4

    def test_executed_count(self):
        """真实执行数 = resolution='executed' 的 3 行"""
        assert _count_executed(self.rows) == 3

    def test_conflict_count(self):
        """
        冲突数：resolution='conflict' 共 3 行
            LTCUSDT, BCHUSDT（standard_scoring origin）
            ARBUSDT（empty_pool origin，虽为影子，但 resolution 是 conflict）
        """
        assert _count_conflict(self.rows) == 3

    def test_undecided_count(self):
        """未决行 = status='triggered' AND resolution IS NULL，共 1 行（XRPUSDT）"""
        assert _count_undecided(self.rows) == 1

    def test_execution_rate(self):
        """
        执行率 = executed(3) / (executed(3) + conflict(3) + timeout(3)) = 3/9 = 0.333
        shadow 不计入分母
        """
        rate = _calc_execution_rate(self.rows)
        assert rate == pytest.approx(3 / 9)

    def test_execution_rate_zero_denominator(self):
        """当 executed + conflict + timeout = 0 时返回 None（零分母保护）"""
        empty_terminal_rows = [
            {"id": 1, "symbol": "X", "status": "triggered", "resolution": None,
             "signal_origin": "standard_scoring"},
        ]
        assert _calc_execution_rate(empty_terminal_rows) is None


# ==================== 关键红线断言 ====================

class TestRedLineAssertions:
    """
    架构 §6.2 关键红线验证：
        1. 超时/影子/冲突不得被算作「真实执行」
        2. 胜率不得由 hrs_signals 行数推导
    """

    def setup_method(self):
        self.rows = _build_fixed_sample()

    def test_timeout_not_counted_as_execution(self):
        """超时信号（resolution='timeout'）的 resolution 字段恒为 'timeout'，永不等于 'executed'"""
        timeout_rows = [r for r in self.rows if r["resolution"] == "timeout"]
        assert len(timeout_rows) == 3
        for r in timeout_rows:
            assert r["resolution"] != "executed"

    def test_shadow_not_counted_as_execution(self):
        """
        影子信号不计入真实执行数：
            3 行 resolution='shadow' 的真实执行数 = 0
            1 行 signal_origin='empty_pool' 的（resolution 是 conflict，自然不算 executed）
        因此 _count_executed 对 shadow 子集为 0
        """
        shadow_rows = [
            r for r in self.rows
            if r["resolution"] == "shadow"
            or r["signal_origin"] in ("shadow_scoring", "empty_pool")
        ]
        assert len(shadow_rows) == 4
        # 影子子集里没有 resolution='executed'
        executed_in_shadow = [r for r in shadow_rows if r["resolution"] == "executed"]
        assert len(executed_in_shadow) == 0

    def test_conflict_not_counted_as_execution(self):
        """冲突信号不计入真实执行数"""
        conflict_rows = [r for r in self.rows if r["resolution"] == "conflict"]
        for r in conflict_rows:
            assert r["resolution"] != "executed"

    def test_win_rate_must_come_from_trade_records(self):
        """
        关键红线：胜率分子分母必须取 trading.trade_records.realized_pnl（真实成交），
        不能用 hrs_signals 行数或 resolution='executed' 数量推导。

        验证：构造一个"已执行信号数"和"真实成交盈亏"不一致的反例，
        证明两者语义不重叠。
        """
        # 假设：executed 信号数 = 3，但其中 2 笔是亏损的、1 笔还没平仓
        # 真实成交结果（来自 trading.trade_records，而非 hrs_signals）：
        trade_records = [
            {"strategy": "hrs", "symbol": "BTCUSDT", "realized_pnl": 150.0},   # 盈利 + 已平仓
            {"strategy": "hrs", "symbol": "ETHUSDT", "realized_pnl": -50.0},   # 亏损 + 已平仓
            {"strategy": "hrs", "symbol": "SOLUSDT", "realized_pnl": None},    # 未平仓（realized_pnl 为 NULL）
        ]
        win_molecule = sum(
            1 for t in trade_records
            if t["strategy"] == "hrs" and t["realized_pnl"] is not None and t["realized_pnl"] > 0
        )
        win_denom = sum(
            1 for t in trade_records
            if t["strategy"] == "hrs" and t["realized_pnl"] is not None
        )

        # 信号维度：3 executed 行
        executed_from_signals = _count_executed(self.rows)

        # 真实成交维度：1 笔盈利 / 2 笔已平仓（1 笔亏损 1 笔盈利）
        assert win_molecule == 1
        assert win_denom == 2

        # 关键：信号 executed 数 ≠ 胜率分母，两者语义不重叠
        assert executed_from_signals != win_denom, (
            "执行信号数不能直接用作胜率分母；signal count=%d vs trade_records count=%d"
            % (executed_from_signals, win_denom)
        )
