"""
回归测试：_add_position 入场风控拒绝必须回写 skip_reason。

背景（2026-11-05 生产事故）：
  SOLUSDT 已有浮盈空单，analyze() 生成加仓信号，_add_position() 在
  第一道风控闸门 entry_allowed(..., adding=True) 返回 False 时直接
  return False，且未回写 skip_reason，导致 main.py 无法区分"预期内拒绝"
  与"真实异常"，兜底按 error 记录。

覆盖范围：
  1. entry_allowed 返回 False → _add_position 返回 False，signal['skip_reason']
     被写入非空字符串（修复点验证）。
  2. 拒绝分支在调用任何交易所接口前返回（哨兵对象防穿透）。
  3. 拒绝分支不访问 position.rebuild_pending 之后的逻辑（第二道闸门保护）。

所有外部依赖（entry_risk_policy、DB、交易所、通知）全部 mock，绝不连真实服务。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# 被测模块
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
import strategies.btc_eth_aggressive.strategy as strategy_mod


# ── 假件构造 ───────────────────────────────────────────────────


def _make_strategy() -> BTCEthStrategy:
    """绕过 __init__ 直接构造策略实例。"""
    return object.__new__(BTCEthStrategy)


class _ForbiddenAccess:
    """哨兵：任何属性访问都会抛错，用于证明代码没有穿透到交易所层。"""

    def __getattr__(self, name):
        raise AssertionError(
            f"_add_position 拒绝分支不应访问 binance.{name}（应在 entry_allowed 处提前返回）"
        )

    def __await__(self):
        raise AssertionError("_add_position 拒绝分支不应 await 任何交易所调用")


def _make_position() -> SimpleNamespace:
    """构造含 rebuild_pending=False 的最小持仓桩。"""
    return SimpleNamespace(rebuild_pending=False)


def _make_signal() -> dict:
    """构造最小加仓信号 dict。"""
    return {
        "direction": "SHORT",
        "grade": "A",
        "quantity": 1.0,
    }


# ── fixture ────────────────────────────────────────────────────


@pytest.fixture
def mock_entry_allowed(monkeypatch):
    """把模块级 entry_allowed 替换为 AsyncMock；返回 mock 供断言。"""
    mock = AsyncMock()
    monkeypatch.setattr(strategy_mod, "entry_allowed", mock)
    return mock


@pytest.fixture
def forbidden_binance():
    """拒绝分支不应访问的 binance 哨兵对象。"""
    return _ForbiddenAccess()


# ══════════════════════════════════════════════════════════════════
# 核心修复点：entry_allowed 返回 False 必须回写 skip_reason
# ══════════════════════════════════════════════════════════════════


async def test_entry_allowed_false_writes_skip_reason(mock_entry_allowed):
    """entry_allowed 返回 False → _add_position 返回 False，signal['skip_reason'] 非空。"""
    mock_entry_allowed.return_value = False

    strat = _make_strategy()
    strat.binance = AsyncMock()  # 这里即使给真 mock，也会在第一道闸门就返回
    position = _make_position()
    signal = _make_signal()

    result = await strat._add_position("SOLUSDT", position, signal)

    assert result is False, "entry_allowed=False 时应返回 False"
    assert "skip_reason" in signal, "必须回写 skip_reason"
    assert isinstance(signal["skip_reason"], str) and len(signal["skip_reason"]) > 0, (
        f"skip_reason 必须是非空字符串，实际：{signal.get('skip_reason')!r}"
    )
    assert "入场风控" in signal["skip_reason"], (
        f"skip_reason 应包含风控语义，实际：{signal.get('skip_reason')!r}"
    )


async def test_entry_allowed_called_with_adding_true(mock_entry_allowed):
    """entry_allowed 必须以 adding=True 调用，不能遗漏该关键字参数。"""
    mock_entry_allowed.return_value = False

    strat = _make_strategy()
    strat.binance = AsyncMock()
    position = _make_position()
    signal = _make_signal()

    await strat._add_position("SOLUSDT", position, signal)

    mock_entry_allowed.assert_awaited_once()
    args, kwargs = mock_entry_allowed.await_args
    # 第一个位置参数是 strategy 实例（self），第二个是策略名
    assert args[1] == "btc_eth_aggressive", "策略名必须是 btc_eth_aggressive"
    assert kwargs.get("adding") is True or (len(args) >= 3 and args[2] is True), (
        "必须传入 adding=True"
    )


# ══════════════════════════════════════════════════════════════════
# 拒绝分支必须在访问任何交易所接口前返回
# ══════════════════════════════════════════════════════════════════


async def test_reject_branch_does_not_touch_exchange(mock_entry_allowed, forbidden_binance):
    """entry_allowed=False 时不应访问 self.binance 任何属性；哨兵抛错即测试失败。"""
    mock_entry_allowed.return_value = False

    strat = _make_strategy()
    strat.binance = forbidden_binance  # 哨兵：任何访问都会 AssertionError
    position = _make_position()
    signal = _make_signal()

    result = await strat._add_position("SOLUSDT", position, signal)

    assert result is False
    assert signal.get("skip_reason"), "正常返回且已回写 skip_reason → 说明哨兵未被触发"


async def test_reject_branch_does_not_check_rebuild_pending(mock_entry_allowed, forbidden_binance):
    """entry_allowed=False 是第一道闸门，不应走到 position.rebuild_pending 之后的逻辑。"""
    mock_entry_allowed.return_value = False

    # 关键：故意让 position.rebuild_pending 抛错，如果代码穿透到第二道闸门就会炸
    class _ExplodingPosition(SimpleNamespace):
        @property
        def rebuild_pending(self):
            raise AssertionError(
                "_add_position 拒绝分支不应访问 rebuild_pending"
                "（entry_allowed 是第一道闸门，应提前返回）"
            )

    strat = _make_strategy()
    strat.binance = forbidden_binance
    position = _ExplodingPosition(rebuild_pending=False)  # 即使值是 False，property 也会先被求值
    signal = _make_signal()

    result = await strat._add_position("SOLUSDT", position, signal)

    assert result is False
    assert signal.get("skip_reason"), "正常返回 → 证明第一道闸门就截断了"
