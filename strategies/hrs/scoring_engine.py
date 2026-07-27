"""
评分引擎模块
计算 HRS 策略的综合评分，支持做空和做多两个方向
评分维度：合约数据（25%）、技术面（45%）、情绪面（30%）
"""
from typing import Dict, Any, Tuple, Optional, TYPE_CHECKING
from dataclasses import dataclass, field
import structlog

if TYPE_CHECKING:
    from .candidate_pool import DynamicThresholds


logger = structlog.get_logger()


@dataclass
class ScoringResult:
    """评分结果数据类"""

    symbol: str
    direction: str  # 'short' 或 'long'
    total_score: float
    contract_score: float
    technical_score: float
    sentiment_score: float
    veto: bool
    veto_reason: Optional[str]
    extreme_bonus: float = 0.0
    extreme_bonus_applied: bool = False
    extreme_bonus_reason: Optional[str] = None
    entry_mode: str = "standard"          # V2.0-C 新增: "standard" 或 "emm"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "total_score": self.total_score,
            "contract_score": self.contract_score,
            "technical_score": self.technical_score,
            "sentiment_score": self.sentiment_score,
            "contract_weight": self.details.get("weights", {}).get("contract", 0.25),
            "technical_weight": self.details.get("weights", {}).get("technical", 0.45),
            "sentiment_weight": self.details.get("weights", {}).get("sentiment", 0.30),
            "veto": self.veto,
            "veto_reason": self.veto_reason,
            "extreme_bonus": self.extreme_bonus,
            "extreme_bonus_applied": self.extreme_bonus_applied,
            "extreme_bonus_reason": self.extreme_bonus_reason,
            "entry_mode": self.entry_mode,
            "details": self.details,
        }


class ScoringEngine:
    """
    评分引擎

    基于多维度评分系统，综合评估反转机会：
    - 合约数据评分（25%）：OI/市值比
    - 技术面评分（45%）：形态识别（做空/做多）
    - 情绪面评分（30%）：资金费率（年化）
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化评分引擎

        Args:
            config: 配置字典
        """
        self.config = config
        scoring_config = config.get("scoring", {})

        # 权重
        weights = scoring_config.get("weights", {})
        self.contract_weight = weights.get("contract", 0.25)
        self.technical_weight = weights.get("technical", 0.45)
        self.sentiment_weight = weights.get("sentiment", 0.30)

        # 入场阈值
        self.entry_threshold = scoring_config.get("entry_threshold", 6.0)

        # 技术面硬性要求
        tech_config = scoring_config.get("technical", {})
        self.min_technical_score = tech_config.get("min_total_score", 4.0)
        self.min_primary_pattern_score = tech_config.get("min_primary_pattern_score", 1.0)

        # V2.0 新增：极端行情加分配置
        eb_config = scoring_config.get("extreme_bonus", {})
        self.extreme_bonus_enabled = eb_config.get("enabled", True)
        self.extreme_bonus_score = eb_config.get("bonus_score", 1.5)
        self.extreme_bonus_cap = eb_config.get("technical_score_cap", 10)
        self.extreme_bonus_short_threshold = eb_config.get("short", {}).get("threshold", 0.15)
        self.extreme_bonus_long_threshold = eb_config.get("long", {}).get("threshold", -0.15)

        # 年化费率计算参数
        funding_config = config.get("funding_rate", {})
        self.settlements_per_day = funding_config.get("settlements_per_day", 3)
        self.days_per_year = funding_config.get("days_per_year", 365)

        # V2.0-C 新增：极端市场模块（EMM）配置
        emm_config = config.get("emm", {})
        self.emm_enabled = emm_config.get("enabled", False)
        self.emm_long_config = emm_config.get("long", {})
        self.emm_short_config = emm_config.get("short", {})
        self.emm_scoring_config = emm_config.get("scoring", {})
        self.emm_technical_score = self.emm_scoring_config.get("technical_score", 5.0)
        self.emm_entry_threshold = self.emm_scoring_config.get("entry_threshold", 6.0)

        # V2.3 动态阈值引用（由候选池扫描后注入）
        self._dynamic_thresholds: Optional["DynamicThresholds"] = None

        # 思路3：半EMM配置（2/3条件满足时跳过形态门槛）
        semi_emm_config = config.get("semi_emm", {})
        self.semi_emm_enabled = semi_emm_config.get("enabled", False)
        self.semi_emm_min_conditions = semi_emm_config.get("min_conditions", 2)
        # V2.2：半EMM与完整EMM共用技术地板值
        self.emm_tech_floor = semi_emm_config.get("emm_tech_floor", 5.0)
        # F+G组合：半EMM总分阈值和合约分地板
        self.semi_emm_entry_threshold = semi_emm_config.get("entry_threshold", 5.0)
        self.semi_emm_contract_floor = semi_emm_config.get("contract_floor", 3.0)

        logger.info(
            "评分引擎初始化完成",
            weights={
                "contract": self.contract_weight,
                "technical": self.technical_weight,
                "sentiment": self.sentiment_weight,
            },
            entry_threshold=self.entry_threshold,
            emm_entry_threshold=self.emm_entry_threshold,
            semi_emm_entry_threshold=self.semi_emm_entry_threshold,
            emm_tech_floor=self.emm_tech_floor,
            semi_emm_contract_floor=self.semi_emm_contract_floor,
            semi_emm_enabled=self.semi_emm_enabled,
            emm_enabled=self.emm_enabled,
        )

    def set_dynamic_thresholds(self, thresholds: Optional["DynamicThresholds"]) -> None:
        """
        V2.3：设置动态阈值引用

        由 HRSStrategy 在候选池扫描完成后调用，注入全市场分位数阈值。
        传入 None 时清空动态阈值，后续判断回退到固定阈值。

        Args:
            thresholds: DynamicThresholds 实例，或 None 表示禁用动态阈值
        """
        self._dynamic_thresholds = thresholds
        if thresholds is not None:
            logger.info("动态阈值已注入评分引擎", sample_count=thresholds.sample_count)
        else:
            logger.info("动态阈值已清空，评分引擎回退固定阈值")

    def _should_use_dynamic(self) -> bool:
        """
        V2.3：检查是否应使用动态阈值

        条件：
        1. 动态阈值功能已启用（candidate_pool.dynamic_thresholds.enabled）
        2. 动态阈值已注入（非 None）
        3. 动态阈值有效（is_valid() 返回 True）

        Returns:
            是否应使用动态阈值
        """
        dynamic_config = self.config.get("candidate_pool", {}).get("dynamic_thresholds", {})
        if not dynamic_config.get("enabled", False):
            return False
        if self._dynamic_thresholds is None:
            return False
        return self._dynamic_thresholds.is_valid()

    def calculate_contract_score(
        self,
        oi_market_cap_ratio: float,
        direction: str,
        has_market_cap: bool = True
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算合约数据评分（OI/市值比）

        Args:
            oi_market_cap_ratio: OI/市值比
            direction: 方向 ('short' 或 'long')
            has_market_cap: 是否成功获取市值数据

        Returns:
            (评分, 详细信息)
        """
        contract_config = self.config.get("scoring", {}).get("contract", {})
        ratio_config = contract_config.get("oi_market_cap_ratio", {})
        thresholds = ratio_config.get("thresholds", {})
        scores = ratio_config.get("scores", {}).get(direction, {})
        fallback = ratio_config.get("fallback", {})

        if not has_market_cap:
            fb_score = fallback.get(direction, 0)
            return fb_score, {"reason": "市值获取失败", "oi_market_cap_ratio": None, "score": fb_score}

        extreme_high = thresholds.get("extreme_high", 0.25)
        high = thresholds.get("high", 0.20)
        mid_high = thresholds.get("mid_high", 0.15)
        mid = thresholds.get("mid", 0.10)
        low = thresholds.get("low", 0.05)

        if oi_market_cap_ratio > extreme_high:
            score = scores.get("extreme_high", 10)
            reason = f"极度{'拥挤' if direction == 'short' else '冷清'} ({oi_market_cap_ratio:.4f} > {extreme_high})"
        elif oi_market_cap_ratio >= high:
            score = scores.get("high", 8)
            reason = f"{'拥挤' if direction == 'short' else '较冷清'} ({oi_market_cap_ratio:.4f})"
        elif oi_market_cap_ratio >= mid_high:
            score = scores.get("mid_high", 6)
            reason = f"中等偏{'拥挤' if direction == 'short' else '冷清'} ({oi_market_cap_ratio:.4f})"
        elif oi_market_cap_ratio >= mid:
            score = scores.get("mid", 4)
            reason = f"中性 ({oi_market_cap_ratio:.4f})"
        elif oi_market_cap_ratio >= low:
            score = scores.get("low", 2)
            reason = f"{'冷清' if direction == 'short' else '偏拥挤'} ({oi_market_cap_ratio:.4f})"
        else:
            score = scores.get("extreme_low", 0)
            reason = f"极度{'冷清' if direction == 'short' else '拥挤'} ({oi_market_cap_ratio:.4f} < {low})"

        return score, {"reason": reason, "oi_market_cap_ratio": oi_market_cap_ratio, "score": score}

    def calculate_technical_score(
        self,
        patterns: Dict[str, Any],
        direction: str,
        price_change_24h: Optional[float] = None,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算技术面评分

        Args:
            patterns: 形态检测结果
            direction: 方向 ('short' 或 'long')
            price_change_24h: 24小时价格涨跌幅（小数形式），用于极端行情加分判断

        Returns:
            (评分, 详细信息)
        """
        if direction == "short":
            three_tops = patterns.get("three_tops", (False, 0.0))
            long_shadow = patterns.get("long_upper_shadow", (False, 0.0))
            volume_signal = patterns.get("volume_stagnation", (False, 0.0))
            # 思路2：替代形态
            double_top = patterns.get("double_top", (False, 0.0))
            v_reversal = patterns.get("v_reversal_short", (False, 0.0))
        else:
            three_tops = patterns.get("three_bottoms", (False, 0.0))
            long_shadow = patterns.get("long_lower_shadow", (False, 0.0))
            volume_signal = patterns.get("volume_reversal", (False, 0.0))
            # 思路2：替代形态
            double_top = patterns.get("double_bottom", (False, 0.0))
            v_reversal = patterns.get("v_reversal_long", (False, 0.0))

        raw_total = three_tops[1] + long_shadow[1] + volume_signal[1]

        # 思路2：替代形态得分（双底/双顶 或 V型反转）
        alternative_score = double_top[1] + v_reversal[1]

        # V2.0 新增：极端行情加分逻辑
        extreme_bonus = 0.0
        extreme_bonus_applied = False
        extreme_bonus_reason = None

        if self.extreme_bonus_enabled and price_change_24h is not None:
            if direction == "short" and price_change_24h >= self.extreme_bonus_short_threshold:
                extreme_bonus = self.extreme_bonus_score
                extreme_bonus_applied = True
                extreme_bonus_reason = (
                    f"做空极端行情加分：24h涨幅 {price_change_24h:.1%} "
                    f">= 阈值 {self.extreme_bonus_short_threshold:.1%}，+{self.extreme_bonus_score}分"
                )
            elif direction == "long" and price_change_24h <= self.extreme_bonus_long_threshold:
                extreme_bonus = self.extreme_bonus_score
                extreme_bonus_applied = True
                extreme_bonus_reason = (
                    f"做多极端行情加分：24h跌幅 {price_change_24h:.1%} "
                    f"<= 阈值 {self.extreme_bonus_long_threshold:.1%}，+{self.extreme_bonus_score}分"
                )

        # 加分后封顶
        total = min(raw_total + alternative_score + extreme_bonus, self.extreme_bonus_cap)

        details = {
            "primary_pattern": three_tops[0],
            "primary_pattern_score": three_tops[1] + alternative_score,  # 思路2：合并替代形态得分
            "shadow_pattern": long_shadow[0],
            "shadow_pattern_score": long_shadow[1],
            "volume_pattern": volume_signal[0],
            "volume_pattern_score": volume_signal[1],
            "total_score": total,
            "technical_score_raw": raw_total,
            "alternative_score": alternative_score,           # 思路2：替代形态得分
            "alternative_detected": alternative_score > 0,    # 思路2：是否检测到替代形态
            "extreme_bonus": extreme_bonus,
            "extreme_bonus_applied": extreme_bonus_applied,
            "extreme_bonus_reason": extreme_bonus_reason,
        }

        if extreme_bonus_applied:
            logger.info(
                "极端行情加分触发",
                direction=direction,
                price_change_24h=price_change_24h,
                raw_total=raw_total,
                bonus=extreme_bonus,
                capped_total=total,
            )

        return total, details

    def calculate_sentiment_score(
        self,
        funding_rate: float,
        direction: str
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算情绪面评分（资金费率年化）

        Args:
            funding_rate: 资金费率（小数形式）
            direction: 方向 ('short' 或 'long')

        Returns:
            (评分, 详细信息)
        """
        # 年化费率 = 费率 × 每日结算次数 × 年化天数 × 100%
        annualized_rate = funding_rate * self.settlements_per_day * self.days_per_year * 100

        sentiment_config = self.config.get("scoring", {}).get("sentiment", {})
        thresholds = sentiment_config.get("thresholds", {})
        scores = sentiment_config.get("scores", {}).get(direction, {})

        extreme_high = thresholds.get("extreme_high", 150)
        high = thresholds.get("high", 100)
        mid = thresholds.get("mid", 50)
        low = thresholds.get("low", 0)
        low_negative = thresholds.get("low_negative", -20)

        if annualized_rate > extreme_high:
            score = scores.get("extreme_high", 10)
            reason = f"年化费率 {annualized_rate:.1f}% > {extreme_high}%"
        elif annualized_rate >= high:
            score = scores.get("high", 8)
            reason = f"年化费率 {annualized_rate:.1f}% ({high}%~{extreme_high}%)"
        elif annualized_rate >= mid:
            score = scores.get("mid", 6)
            reason = f"年化费率 {annualized_rate:.1f}% ({mid}%~{high}%)"
        elif annualized_rate >= low:
            score = scores.get("low", 3)
            reason = f"年化费率 {annualized_rate:.1f}% ({low}%~{mid}%)"
        elif annualized_rate >= low_negative:
            score = scores.get("negative", 1)
            reason = f"年化费率 {annualized_rate:.1f}% ({low_negative}%~{low}%)"
        else:
            score = scores.get("extreme_negative", 0)
            reason = f"年化费率 {annualized_rate:.1f}% < {low_negative}%"

        return score, {"reason": reason, "annualized_rate": annualized_rate, "score": score}

    def check_emm_conditions(
        self,
        direction: str,
        price_change_24h: Optional[float],
        funding_rate: float,
        oi_market_cap_ratio: float,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        检查是否满足 EMM 触发条件（V2.0-C 新增，V2.3 适配动态阈值）

        Args:
            direction: "short" 或 "long"
            price_change_24h: 24h涨跌幅百分比值（如 -25.0 表示跌25%）
            funding_rate: 当前资金费率（小数形式）
            oi_market_cap_ratio: OI/市值比

        Returns:
            (是否触发, 详情字典)
        """
        # 1. 如果 EMM 未启用，直接返回 False
        if not self.emm_enabled:
            return False, {"reason": "EMM模块未启用"}

        # 2. 如果 price_change_24h 为 None，无法判断
        if price_change_24h is None:
            return False, {"reason": "缺少24h涨跌幅数据"}

        # 3. 计算年化资金费率（百分比值）
        funding_rate_annual = funding_rate * self.settlements_per_day * self.days_per_year * 100

        # 4. V2.3：判断是否使用动态阈值
        use_dynamic = self._should_use_dynamic()

        # 5. 根据方向检查条件（price_change_24h 始终使用固定阈值）
        if direction == "long":
            cfg = self.emm_long_config
            price_ok = price_change_24h <= cfg.get("price_change_24h", -20)
            if use_dynamic:
                dt = self._dynamic_thresholds
                funding_ok = funding_rate_annual <= dt.funding_rate_emm_long
                oi_ok = oi_market_cap_ratio >= dt.oi_market_cap_emm
                funding_threshold = dt.funding_rate_emm_long
                oi_threshold = dt.oi_market_cap_emm
            else:
                funding_ok = funding_rate_annual <= cfg.get("funding_rate_annual", -50)
                oi_ok = oi_market_cap_ratio >= cfg.get("oi_market_cap_ratio", 0.15)
                funding_threshold = cfg.get("funding_rate_annual")
                oi_threshold = cfg.get("oi_market_cap_ratio")
        else:  # short
            cfg = self.emm_short_config
            price_ok = price_change_24h >= cfg.get("price_change_24h", 20)
            if use_dynamic:
                dt = self._dynamic_thresholds
                funding_ok = funding_rate_annual >= dt.funding_rate_emm_short
                oi_ok = oi_market_cap_ratio >= dt.oi_market_cap_emm
                funding_threshold = dt.funding_rate_emm_short
                oi_threshold = dt.oi_market_cap_emm
            else:
                funding_ok = funding_rate_annual >= cfg.get("funding_rate_annual", 150)
                oi_ok = oi_market_cap_ratio >= cfg.get("oi_market_cap_ratio", 0.15)
                funding_threshold = cfg.get("funding_rate_annual")
                oi_threshold = cfg.get("oi_market_cap_ratio")

        # 6. 构建详情
        details = {
            "emm_triggered": price_ok and funding_ok and oi_ok,
            "price_change_24h": price_change_24h,
            "price_change_threshold": cfg.get("price_change_24h"),
            "price_change_ok": price_ok,
            "funding_rate_annual": funding_rate_annual,
            "funding_rate_threshold": funding_threshold,
            "funding_rate_ok": funding_ok,
            "oi_market_cap_ratio": oi_market_cap_ratio,
            "oi_market_cap_threshold": oi_threshold,
            "oi_market_cap_ok": oi_ok,
            "dynamic_thresholds_used": use_dynamic,
        }

        if not details["emm_triggered"]:
            reasons = []
            if not price_ok:
                reasons.append("24h涨跌幅不满足")
            if not funding_ok:
                reasons.append("资金费率不满足")
            if not oi_ok:
                reasons.append("OI/市值比不满足")
            details["reason"] = "；".join(reasons)
        else:
            mode = "动态" if use_dynamic else "固定"
            details["reason"] = f"EMM触发（{direction}，{mode}阈值）"

        # 日志
        if details["emm_triggered"]:
            logger.info(
                "EMM条件触发",
                direction=direction,
                use_dynamic=use_dynamic,
                price_change_24h=price_change_24h,
                funding_rate_annual=funding_rate_annual,
                funding_threshold=funding_threshold,
                oi_market_cap_ratio=oi_market_cap_ratio,
                oi_threshold=oi_threshold,
            )

        return details["emm_triggered"], details

    def check_semi_emm_conditions(
        self,
        direction: str,
        price_change_24h: Optional[float],
        funding_rate: float,
        oi_market_cap_ratio: float,
    ) -> Tuple[bool, int, Dict[str, Any]]:
        """
        检查半EMM条件（思路3：2/3条件满足时跳过形态门槛，V2.3 适配动态阈值）

        Args:
            direction: "short" 或 "long"
            price_change_24h: 24h涨跌幅百分比值
            funding_rate: 当前资金费率（小数形式）
            oi_market_cap_ratio: OI/市值比

        Returns:
            (是否触发半EMM, 满足条件数, 详情字典)
        """
        if not self.semi_emm_enabled:
            return False, 0, {"reason": "半EMM模块未启用"}

        if price_change_24h is None:
            return False, 0, {"reason": "缺少24h涨跌幅数据"}

        # 计算年化资金费率
        funding_rate_annual = funding_rate * self.settlements_per_day * self.days_per_year * 100

        # V2.3：判断是否使用动态阈值
        use_dynamic = self._should_use_dynamic()

        # 根据方向检查条件（price_change_24h 始终使用固定阈值）
        if direction == "long":
            cfg = self.emm_long_config
            price_ok = price_change_24h <= cfg.get("price_change_24h", -20)
            if use_dynamic:
                dt = self._dynamic_thresholds
                funding_ok = funding_rate_annual <= dt.funding_rate_emm_long
                oi_ok = oi_market_cap_ratio >= dt.oi_market_cap_emm
            else:
                funding_ok = funding_rate_annual <= cfg.get("funding_rate_annual", -50)
                oi_ok = oi_market_cap_ratio >= cfg.get("oi_market_cap_ratio", 0.15)
        else:
            cfg = self.emm_short_config
            price_ok = price_change_24h >= cfg.get("price_change_24h", 20)
            if use_dynamic:
                dt = self._dynamic_thresholds
                funding_ok = funding_rate_annual >= dt.funding_rate_emm_short
                oi_ok = oi_market_cap_ratio >= dt.oi_market_cap_emm
            else:
                funding_ok = funding_rate_annual >= cfg.get("funding_rate_annual", 150)
                oi_ok = oi_market_cap_ratio >= cfg.get("oi_market_cap_ratio", 0.15)

        # 计数满足条件
        conditions_met = sum([price_ok, funding_ok, oi_ok])
        is_semi_emm = conditions_met >= self.semi_emm_min_conditions

        details = {
            "semi_emm_triggered": is_semi_emm,
            "conditions_met": conditions_met,
            "conditions_required": self.semi_emm_min_conditions,
            "price_change_24h": price_change_24h,
            "price_change_ok": price_ok,
            "funding_rate_annual": funding_rate_annual,
            "funding_rate_ok": funding_ok,
            "oi_market_cap_ratio": oi_market_cap_ratio,
            "oi_market_cap_ok": oi_ok,
            "dynamic_thresholds_used": use_dynamic,
        }

        if is_semi_emm:
            mode = "动态" if use_dynamic else "固定"
            details["reason"] = f"半EMM触发：{conditions_met}/{3} 条件满足，跳过形态门槛（{mode}阈值）"
        else:
            details["reason"] = f"半EMM未触发：仅{conditions_met}/{3} 条件满足"

        return is_semi_emm, conditions_met, details

    def score(
        self,
        symbol: str,
        direction: str,
        oi_market_cap_ratio: float,
        patterns: Dict[str, Any],
        funding_rate: float,
        has_market_cap: bool = True,
        price_change_24h: Optional[float] = None,
    ) -> ScoringResult:
        """
        执行完整评分（V2.0-C：双轨并行 - 标准模式 + EMM模式）

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
            oi_market_cap_ratio: OI/市值比
            patterns: 形态检测结果（EMM模式下可传空字典）
            funding_rate: 资金费率（小数形式）
            has_market_cap: 是否成功获取市值
            price_change_24h: 24小时价格涨跌幅百分比值（如 -25.0 表示跌25%），
                              用于EMM判断和极端行情加分

        Returns:
            评分结果
        """
        logger.info("开始评分", symbol=symbol, direction=direction)

        # 1. 合约数据评分
        contract_score, contract_details = self.calculate_contract_score(
            oi_market_cap_ratio, direction, has_market_cap
        )

        # 2. V2.0-C：先检查是否触发 EMM 极端市场模式
        is_emm, emm_details = self.check_emm_conditions(
            direction=direction,
            price_change_24h=price_change_24h,
            funding_rate=funding_rate,
            oi_market_cap_ratio=oi_market_cap_ratio,
        )

        if is_emm:
            # EMM模式：使用固定技术分，跳过形态检测
            technical_score = self.emm_technical_score
            technical_details = {
                "emm_mode": True,
                "emm_details": emm_details,
                "total_score": technical_score,
                "primary_pattern_score": 0,
                "shadow_pattern_score": 0,
                "volume_pattern_score": 0,
                "technical_score_raw": 0,
            }
            extreme_bonus = 0.0
            extreme_bonus_applied = False
            extreme_bonus_reason = None
            entry_mode = "emm"
            logger.info(
                "EMM极端市场模式触发",
                symbol=symbol,
                direction=direction,
                emm_details=emm_details,
                technical_score=technical_score,
            )
        else:
            # 标准模式：走原有 V2.0 逻辑，price_change_24h 需转为小数形式
            price_change_decimal = price_change_24h / 100.0 if price_change_24h is not None else None
            technical_score, technical_details = self.calculate_technical_score(
                patterns, direction, price_change_decimal
            )
            extreme_bonus = technical_details.get("extreme_bonus", 0.0)
            extreme_bonus_applied = technical_details.get("extreme_bonus_applied", False)
            extreme_bonus_reason = technical_details.get("extreme_bonus_reason")
            entry_mode = "standard"

            # 思路3：检查半EMM（2/3条件满足则跳过形态门槛）
            is_semi_emm, semi_conditions_met, _ = self.check_semi_emm_conditions(
                direction=direction,
                price_change_24h=price_change_24h,
                funding_rate=funding_rate,
                oi_market_cap_ratio=oi_market_cap_ratio,
            )
            if is_semi_emm:
                entry_mode = "semi_emm"
                technical_details["semi_emm"] = True
                technical_details["semi_emm_conditions_met"] = semi_conditions_met
                # V2.2：半EMM触发时，技术分应用地板值（与完整EMM对齐）
                original_tech = technical_score
                technical_score = max(technical_score, self.emm_tech_floor)
                technical_details["total_score"] = technical_score
                technical_details["semi_emm_floor_applied"] = True
                technical_details["semi_emm_floor_original"] = original_tech
                # F+G组合：半EMM触发时，合约分应用地板值
                original_contract = contract_score
                contract_score = max(contract_score, self.semi_emm_contract_floor)
                contract_details["semi_emm_floor_applied"] = True
                contract_details["semi_emm_floor_original"] = original_contract
                logger.info(
                    "半EMM触发，应用地板值",
                    symbol=symbol,
                    direction=direction,
                    conditions_met=semi_conditions_met,
                    tech_original=original_tech,
                    tech_floored=technical_score,
                    contract_original=original_contract,
                    contract_floored=contract_score,
                )

        # 3. 情绪面评分
        sentiment_score, sentiment_details = self.calculate_sentiment_score(
            funding_rate, direction
        )

        # 4. 计算总分
        total_score = (
            contract_score * self.contract_weight
            + technical_score * self.technical_weight
            + sentiment_score * self.sentiment_weight
        )

        details = {
            "contract": contract_details,
            "technical": technical_details,
            "sentiment": sentiment_details,
            "weights": {
                "contract": self.contract_weight,
                "technical": self.technical_weight,
                "sentiment": self.sentiment_weight,
            },
        }

        result = ScoringResult(
            symbol=symbol,
            direction=direction,
            total_score=round(total_score, 2),
            contract_score=round(contract_score, 2),
            technical_score=round(technical_score, 2),
            sentiment_score=round(sentiment_score, 2),
            veto=False,
            veto_reason=None,
            extreme_bonus=extreme_bonus,
            extreme_bonus_applied=extreme_bonus_applied,
            extreme_bonus_reason=extreme_bonus_reason,
            entry_mode=entry_mode,
            details=details,
        )

        logger.info(
            "评分完成",
            symbol=symbol,
            direction=direction,
            total_score=result.total_score,
            contract_score=result.contract_score,
            technical_score=result.technical_score,
            sentiment_score=result.sentiment_score,
            entry_mode=result.entry_mode,
        )

        return result

    def should_entry(self, score_result: ScoringResult) -> bool:
        """
        判断是否应该入场（V2.0-C：EMM模式跳过技术面硬性门槛）

        入场条件（必须同时满足）：
        1. 总分 ≥ 6.0
        2. 无一票否决
        3. 标准模式：技术总分 ≥ 4.0 且 基础形态分 ≥ 1.0
           EMM模式：跳过技术面硬性门槛

        Args:
            score_result: 评分结果

        Returns:
            是否应该入场
        """
        if score_result.veto:
            logger.info("一票否决，不入场", symbol=score_result.symbol)
            return False

        # V2.2：EMM/半EMM/标准模式使用各自入场阈值
        if score_result.entry_mode == "emm":
            threshold = self.emm_entry_threshold
        elif score_result.entry_mode == "semi_emm":
            threshold = self.semi_emm_entry_threshold
        else:
            threshold = self.entry_threshold
        if score_result.total_score < threshold:
            logger.info(
                "总分未达阈值",
                symbol=score_result.symbol,
                total_score=score_result.total_score,
                threshold=threshold,
            )
            return False

        # V2.0-C：EMM模式跳过技术面硬性门槛；思路3：半EMM也跳过形态门槛
        if score_result.entry_mode == "emm":
            logger.info(
                "EMM模式满足入场条件",
                symbol=score_result.symbol,
                direction=score_result.direction,
                total_score=score_result.total_score,
            )
            return True

        if score_result.technical_score < self.min_technical_score:
            logger.info(
                "技术总分未达要求",
                symbol=score_result.symbol,
                technical_score=score_result.technical_score,
                required=self.min_technical_score,
            )
            return False

        # 思路3：半EMM模式跳过形态门槛，但仍需满足技术总分
        if score_result.entry_mode == "semi_emm":
            if score_result.technical_score >= self.min_technical_score:
                logger.info(
                    "半EMM模式满足入场条件（跳过形态门槛）",
                    symbol=score_result.symbol,
                    direction=score_result.direction,
                    total_score=score_result.total_score,
                    technical_score=score_result.technical_score,
                )
                return True
            return False

        # 检查基础形态评分
        tech_details = score_result.details.get("technical", {})
        primary_pattern_score = tech_details.get("primary_pattern_score", 0)
        if primary_pattern_score < self.min_primary_pattern_score:
            logger.info(
                "基础形态评分不足",
                symbol=score_result.symbol,
                primary_pattern_score=primary_pattern_score,
                required=self.min_primary_pattern_score,
            )
            return False

        logger.info(
            "满足入场条件",
            symbol=score_result.symbol,
            direction=score_result.direction,
            total_score=score_result.total_score,
        )
        return True