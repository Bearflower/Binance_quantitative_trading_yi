"""
V4.0 评分引擎

核心变更：
1. OI/市值比评分逻辑反转（越低越好）
2. 去掉基本面评分
3. 技术面采用纯形态评分（三次冲顶、长上影线、放量滞涨）
4. 情绪面评分简化
5. 增加技术面硬性要求
"""

from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class ScoringResultV4:
    """V4.0 评分结果"""
    symbol: str
    contract_score: float
    technical_score: float
    sentiment_score: float
    total_score: float
    veto: bool
    veto_reason: Optional[str]
    timestamp: datetime
    current_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'contract_score': self.contract_score,
            'technical_score': self.technical_score,
            'sentiment_score': self.sentiment_score,
            'total_score': self.total_score,
            'veto': self.veto,
            'veto_reason': self.veto_reason,
            'timestamp': self.timestamp.isoformat(),
            'current_price': self.current_price
        }


class ScoringEngineV4:
    """V4.0 评分引擎"""

    def __init__(self):
        """初始化评分引擎"""
        self.weights = {
            'contract': 0.45,
            'technical': 0.35,
            'sentiment': 0.20
        }

        self.veto_thresholds = {
            'oi_ratio': 0.8,
            'listing_hours': 48
        }

        self.entry_threshold = 6.5

        print("✅ V4.0 评分引擎初始化完成")

    def calculate_contract_score(self, oi_ratio: float) -> Tuple[float, str]:
        """
        计算合约数据评分（V4.0 - 反转逻辑）

        Args:
            oi_ratio: OI/市值比

        Returns:
            (score, reason) 元组
        """
        if oi_ratio < 0.3:
            score = 10.0
            reason = f"优秀，无主力控盘风险 ({oi_ratio:.4f})"
        elif oi_ratio < 0.5:
            score = 7.0
            reason = f"良好，可接受 ({oi_ratio:.4f})"
        elif oi_ratio < 0.8:
            score = 3.0
            reason = f"偏高，不推荐 ({oi_ratio:.4f})"
        else:
            score = 0.0
            reason = f"极危险 ({oi_ratio:.4f})"

        return score, reason

    def calculate_technical_score(
        self,
        three_tops_detected: bool,
        three_tops_score: float,
        long_upper_shadow: bool,
        long_upper_shadow_score: float,
        volume_divergence: bool,
        volume_divergence_score: float
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算技术面评分（V4.0 - 纯形态评分）

        Args:
            three_tops_detected: 是否检测到三次冲顶
            three_tops_score: 三次冲顶得分（0-4）
            long_upper_shadow: 是否检测到长上影线
            long_upper_shadow_score: 长上影线得分（0-3）
            volume_divergence: 是否检测到放量滞涨
            volume_divergence_score: 放量滞涨得分（0-3）

        Returns:
            (总分, 详情) 元组
        """
        total_score = three_tops_score + long_upper_shadow_score + volume_divergence_score

        details = {
            'three_tops': {
                'detected': three_tops_detected,
                'score': three_tops_score
            },
            'long_upper_shadow': {
                'detected': long_upper_shadow,
                'score': long_upper_shadow_score
            },
            'volume_divergence': {
                'detected': volume_divergence,
                'score': volume_divergence_score
            },
            'total_score': total_score
        }

        return total_score, details

    def calculate_sentiment_score(self, annual_rate: float) -> Tuple[float, str]:
        """
        计算情绪面评分（V4.0 - 简化版）

        Args:
            annual_rate: 年化资金费率（百分比）

        Returns:
            (score, reason) 元组
        """
        if annual_rate > 150:
            score = 10.0
            reason = f"资金费率极高 ({annual_rate:.1f}%)"
        elif annual_rate > 100:
            score = 7.0
            reason = f"资金费率较高 ({annual_rate:.1f}%)"
        elif annual_rate > 50:
            score = 3.0
            reason = f"资金费率中等 ({annual_rate:.1f}%)"
        else:
            score = 0.0
            reason = f"资金费率低或负值 ({annual_rate:.1f}%)"

        return score, reason

    def check_veto(
        self,
        oi_ratio: float,
        listing_hours: float
    ) -> Tuple[bool, Optional[str]]:
        """
        检查一票否决条件

        Args:
            oi_ratio: OI/市值比
            listing_hours: 上线时间（小时）

        Returns:
            (veto, reason) 元组
        """
        if oi_ratio > self.veto_thresholds['oi_ratio']:
            return True, f"OI/市值比过高 ({oi_ratio:.4f} > {self.veto_thresholds['oi_ratio']})"

        if listing_hours > self.veto_thresholds['listing_hours']:
            return True, f"上线时间过长 ({listing_hours:.1f}小时 > {self.veto_thresholds['listing_hours']}小时)"

        return False, None

    def check_technical_requirements(
        self,
        three_tops_score: float,
        technical_score: float
    ) -> Tuple[bool, Optional[str]]:
        """
        检查技术面硬性要求

        Args:
            three_tops_score: 三次冲顶得分
            technical_score: 技术总分

        Returns:
            (满足, 原因) 元组
        """
        if three_tops_score < 2:
            return False, f"三次冲顶得分不足 ({three_tops_score:.1f} < 2)"

        if technical_score < 6:
            return False, f"技术总分不足 ({technical_score:.1f} < 6)"

        return True, None

    def calculate_total_score(
        self,
        contract_score: float,
        technical_score: float,
        sentiment_score: float
    ) -> float:
        """
        计算综合评分

        Args:
            contract_score: 合约数据评分
            technical_score: 技术面评分
            sentiment_score: 情绪面评分

        Returns:
            综合评分
        """
        total_score = (
            contract_score * self.weights['contract'] +
            technical_score * self.weights['technical'] +
            sentiment_score * self.weights['sentiment']
        )

        return round(total_score, 2)

    def should_entry(
        self,
        result: ScoringResultV4,
        three_tops_score: float,
        technical_score: float
    ) -> bool:
        """
        判断是否应该开仓

        Args:
            result: 评分结果
            three_tops_score: 三次冲顶得分
            technical_score: 技术总分

        Returns:
            是否开仓
        """
        if result.veto:
            return False

        technical_ok, _ = self.check_technical_requirements(three_tops_score, technical_score)
        if not technical_ok:
            return False

        if result.total_score >= self.entry_threshold:
            return True

        return False


scoring_engine_v4 = ScoringEngineV4()
