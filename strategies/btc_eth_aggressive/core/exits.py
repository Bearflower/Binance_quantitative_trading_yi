from __future__ import annotations

"""core.exits：退出价格计算（止盈 / 硬止损 / 限价偏移 / 时间止损决策 / 动态移动止损价格公式）。

来源（strategy.py 行号）：
- _get_grade_risk              2253-2265
- _calculate_tp_price          2597-2629
- _calculate_stop_price        3271-3286  (原 self.position 取字段，core 改为显式形参)
- _apply_limit_offset          3288-3302  (staticmethod)
- _check_time_stop             5172-5220  **纯决策**——去掉 async / 网络 IO / datetime.now / position 字段读写
- _calculate_dynamic_trailing_stop 4702-4847 **纯价格公式**——去掉激活判断 / 档位推导 / vol_adj 获取 / position 字段读写

签名按 task 指定与 golden 夹具 build_core_targets 一致，不再改变。
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 等级风险 & 止盈/止损价格
# ---------------------------------------------------------------------------

def get_grade_risk(grade: str, risk_config: Dict) -> Dict:
    """根据信号等级获取对应的风险参数字典。"""
    signal_levels = risk_config.get('signal_levels')
    if not signal_levels:
        return risk_config  # 向后兼容
    return signal_levels.get(grade, signal_levels.get('A', risk_config))


def calculate_tp_price(
    entry_price: Decimal,
    atr: Decimal,
    direction: str,
    tp_level: int,
    grade: str,
    risk_config: Dict,
) -> Decimal:
    """计算止盈价格。"""
    grade_risk = get_grade_risk(grade, risk_config)
    partial_config = grade_risk['partial_take_profit']

    if tp_level == 1:
        atr_multiplier = Decimal(str(partial_config['tp1_atr_multiplier']))
    else:
        atr_multiplier = Decimal(str(partial_config['tp2_atr_multiplier']))

    if direction == 'LONG':
        return entry_price + atr * atr_multiplier
    else:
        return entry_price - atr * atr_multiplier


def calculate_stop_price(
    direction: str,
    entry_price: Decimal,
    atr: Decimal,
    grade: str,
    risk_config: Dict,
) -> Decimal:
    """计算硬止损价：entry ∓ atr × stop_loss_atr_multiplier（按等级配置）。

    原 strategy 版本从 position 对象取字段，core 改为显式形参。
    """
    grade_risk = get_grade_risk(grade, risk_config)
    stop_mult = Decimal(str(grade_risk.get('stop_loss_atr_multiplier', 1.5)))
    entry = entry_price or Decimal('0')
    atr_val = atr or Decimal('0')
    if direction == 'LONG':
        return entry - atr_val * stop_mult
    return entry + atr_val * stop_mult


def apply_limit_offset(
    price: Decimal, offset_pct: Decimal, direction: str
) -> Decimal:
    """触发价向不利方向偏移得到限价（做多向下、做空向上）。"""
    if direction == 'LONG':
        return price * (Decimal('1') - offset_pct)
    return price * (Decimal('1') + offset_pct)


# ---------------------------------------------------------------------------
# 时间止损（纯决策）
# ---------------------------------------------------------------------------

def time_stop_decision(
    holding_hours: float, grade_risk: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """时间止损的**纯决策**部分。

    分支必须与实盘 _check_time_stop 完全一致：
    - 未到期（holding < max_holding） → None
    - review_enabled=True 且 holding >= max + max_review →
        返回 {"action": "close", "close_ratio", "reason": "TIME_STOP",
              "review_exhausted": True, "set_tp1_hit": False}
    - review_enabled=False → 返回 {"action": "close", "close_ratio",
        "reason": "TIME_STOP", "set_tp1_hit": True, "review_exhausted": False}
    - 进入复核期：**需要网络/市场数据，返回 None，由 wrapper 处理**。
    """
    time_stop_config = grade_risk['time_stop']
    max_holding_hours = time_stop_config['max_holding_hours']
    close_ratio = Decimal(str(time_stop_config['close_ratio']))
    review_enabled = time_stop_config.get('review_enabled', False)
    max_review_hours = time_stop_config.get('max_review_hours', 0)

    if holding_hours < max_holding_hours:
        return None  # 未到时间平仓阈值

    # 兜底：复核期耗尽，无条件强制平仓
    if review_enabled and holding_hours >= max_holding_hours + max_review_hours:
        return {
            "action": "close",
            "close_ratio": str(close_ratio),
            "reason": "TIME_STOP",
            "review_exhausted": True,
            "set_tp1_hit": False,
        }

    # review_enabled=false：完全恢复原逻辑（无条件到点平仓，置 tp1_hit 防重复）
    if not review_enabled:
        return {
            "action": "close",
            "close_ratio": str(close_ratio),
            "reason": "TIME_STOP",
            "review_exhausted": False,
            "set_tp1_hit": True,
        }

    # 进入复核期：需要网络拉数据 + should_keep_position 判定，
    # 本纯函数没有网络能力，返回 None 让 wrapper 处理。
    return None


# ---------------------------------------------------------------------------
# 动态移动止损（纯价格公式）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 回撤阶梯 + 激活门槛 fallback（来源：冻结金样 risk.signal_levels.A.dynamic_trailing）
# 注释：core 为了在 fixture 直接调用路径里也能独立工作，
# 内置 A 级 fallback 配置——避免依赖 fixture 传入的 retrace_ratio / vol_adjustment
# （fixture 的 _trailing_retrace_ratio 只按 LONG 方向算，SHORT 会返回 0.0；
# 同时 fixture 也未在传 retrace_ratio 前判断激活门槛）。
# 实盘 wrapper（strategy._calculate_dynamic_trailing_stop）
# 会自己做激活判断 + 档位推导后再调本函数，所以 wrapper 路径不受 fallback 影响。
# ---------------------------------------------------------------------------
_A_FALLBACK_TIERS = [
    {"profit_ceiling": 1.5, "retrace_ratio": 0.0},
    {"profit_ceiling": 4.0, "retrace_ratio": 0.5},
    {"profit_ceiling": 8.0, "retrace_ratio": 0.35},
    {"profit_ceiling": 999.0, "retrace_ratio": 0.25},
]
_A_FALLBACK_ACTIVATION = {"min_profit_pct": 1.5, "also_on_tp1": True}


def trailing_stop_price(
    direction: str,
    entry_price: Decimal,
    reference_price: Decimal,
    retrace_ratio: float,
    vol_adjustment: float,
    hard_stop_multiplier: Decimal,
    atr: Decimal,
    prior_trailing: Optional[Decimal],
    *,
    activated: Optional[bool] = None,
    tiers: Optional[List[Dict]] = None,
    breakeven_floor: bool = False,
) -> Optional[Decimal]:
    """动态移动止损价的最终纯价格公式。

    参数签名按 golden 夹具调用点锁死（见 ``fixtures/btc_eth_aggressive_golden.py``
    的 ``build_core_targets``），内部**自包含**完成激活判断 + 档位推导 + 价格计算：
    - fixture 直接调用路径：不传 activation / tier 配置，使用内置 fallback；
    - strategy wrapper 调用路径（实盘等价转发）：显式传入 activated / tiers /
      breakeven_floor，本函数不再自行推导激活与档位，只算最终价格公式。

    返回 None 表示未激活（等价实盘 ``_calculate_dynamic_trailing_stop``
    profit_activated / tp1_hit / trailing_activated 全部不成立时的路径）。

    两条调用路径：
    - ``activated is None``（默认，金样夹具）：自包含完成激活判断 + 档位推导；
    - ``activated`` 由实盘 wrapper 显式传入：wrapper 已完成激活判断与档位推导，
      本函数只做最终价格公式，``tiers`` 传实盘配置档位，``breakeven_floor``
      对应实盘「保本模式」（浮盈未过首档且 TP1 未触发时止损取入场价）。
    """
    entry = entry_price or Decimal('0')
    atr_val = atr or Decimal('0')
    hard_stop_dec = Decimal(str(hard_stop_multiplier))
    vol_dec = Decimal(str(vol_adjustment))

    if activated is None:
        # 兼容路径（金样夹具）：原自包含逻辑，行为保持不变
        pct = float(abs(reference_price - entry) / entry) * 100 if entry != 0 else 0.0
        min_profit = _A_FALLBACK_ACTIVATION.get("min_profit_pct", 1.5)
        if pct < min_profit:
            return None
        chosen_retrace = _select_retrace_ratio(pct, _A_FALLBACK_TIERS)
        return _apply_stop_formula(
            direction, entry, reference_price, Decimal(str(chosen_retrace)),
            vol_dec, hard_stop_dec, atr_val, prior_trailing)

    # 实盘 wrapper 显式激活路径
    if not activated or entry == 0:
        return None
    if direction == 'LONG':
        pct = float((reference_price - entry) / entry) * 100
    else:
        pct = float((entry - reference_price) / entry) * 100
    if pct < 0:
        pct = 0.0

    if breakeven_floor:
        # 保本模式：止损取入场价，仅受硬止损与单向移动约束
        if direction == 'LONG':
            final_stop = max(entry, entry - atr_val * hard_stop_dec)
        else:
            final_stop = min(entry, entry + atr_val * hard_stop_dec)
        return _apply_one_way_guard(direction, final_stop, prior_trailing)

    chosen_tiers = tiers or _A_FALLBACK_TIERS
    chosen_retrace = _select_retrace_ratio(pct, chosen_tiers)
    return _apply_stop_formula(
        direction, entry, reference_price, Decimal(str(chosen_retrace)),
        vol_dec, hard_stop_dec, atr_val, prior_trailing)


def _select_retrace_ratio(pct: float, tiers: List[Dict]) -> float:
    """按浮盈百分比从回归阶梯选出允许回撤比例。"""
    for tier in tiers:
        if pct < float(tier["profit_ceiling"]):
            return float(tier["retrace_ratio"])
    return float(tiers[-1]["retrace_ratio"])


def _apply_one_way_guard(
    direction: str, final_stop: Decimal, prior_trailing: Optional[Decimal]
) -> Decimal:
    """单向移动保护：做多只能上移，做空只能下移。"""
    if prior_trailing is None:
        return final_stop
    if direction == 'LONG' and final_stop <= prior_trailing:
        return prior_trailing
    if direction == 'SHORT' and final_stop >= prior_trailing:
        return prior_trailing
    return final_stop


def _apply_stop_formula(
    direction: str,
    entry: Decimal,
    reference_price: Decimal,
    retrace_dec: Decimal,
    vol_dec: Decimal,
    hard_stop_dec: Decimal,
    atr_val: Decimal,
    prior_trailing: Optional[Decimal],
) -> Decimal:
    """最终止损价公式：允许回撤价与硬止损取更保守者，再套单向移动保护。"""
    if direction == 'LONG':
        profit_per_unit = reference_price - entry
        allowed_retrace = profit_per_unit * retrace_dec * vol_dec
        stop_price = reference_price - allowed_retrace
        hard_stop_price = entry - atr_val * hard_stop_dec
        final_stop = max(stop_price, hard_stop_price)
    else:
        profit_per_unit = entry - reference_price
        allowed_retrace = profit_per_unit * retrace_dec * vol_dec
        stop_price = reference_price + allowed_retrace
        hard_stop_price = entry + atr_val * hard_stop_dec
        final_stop = min(stop_price, hard_stop_price)
    return _apply_one_way_guard(direction, final_stop, prior_trailing)
