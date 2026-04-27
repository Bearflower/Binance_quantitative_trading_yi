"""
评分引擎

核心特性：
1. 使用 OI/上线以来总交易量 替代 OI/市值比
2. 合约数据评分分为两个子项：
   - OI/总交易量比率（权重30%）
   - OI绝对值排名（权重15%）
3. 技术面采用纯形态评分（三次冲顶、长上影线、放量滞涨）
4. 情绪面评分使用资金费率（权重20%）
"""

from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class ScoringResult:
    """评分结果"""
    symbol: str
    contract_score: float
    oi_volume_ratio_score: float
    oi_rank_score: float
    technical_score: float
    sentiment_score: float
    total_score: float
    veto: bool
    veto_reason: Optional[str]
    timestamp: datetime
    current_price: float = 0.0
    oi_volume_ratio: float = 0.0
    oi_usd: float = 0.0
    total_volume_usd: float = 0.0
    funding_rate: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'contract_score': self.contract_score,
            'oi_volume_ratio_score': self.oi_volume_ratio_score,
            'oi_rank_score': self.oi_rank_score,
            'technical_score': self.technical_score,
            'sentiment_score': self.sentiment_score,
            'total_score': self.total_score,
            'veto': self.veto,
            'veto_reason': self.veto_reason,
            'timestamp': self.timestamp.isoformat(),
            'current_price': self.current_price,
            'oi_volume_ratio': self.oi_volume_ratio,
            'oi_usd': self.oi_usd,
            'total_volume_usd': self.total_volume_usd,
            'funding_rate': self.funding_rate
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScoringResult':
        """从字典创建评分结果"""
        return cls(
            symbol=data['symbol'],
            contract_score=data.get('contract_score', 0.0),
            oi_volume_ratio_score=data.get('oi_volume_ratio_score', 0.0),
            oi_rank_score=data.get('oi_rank_score', 0.0),
            technical_score=data.get('technical_score', 0.0),
            sentiment_score=data.get('sentiment_score', 0.0),
            total_score=data.get('total_score', 0.0),
            veto=data.get('veto', False),
            veto_reason=data.get('veto_reason'),
            timestamp=datetime.fromisoformat(data['timestamp']) if isinstance(data['timestamp'], str) else data['timestamp'],
            current_price=data.get('current_price', 0.0),
            oi_volume_ratio=data.get('oi_volume_ratio', 0.0),
            oi_usd=data.get('oi_usd', 0.0),
            total_volume_usd=data.get('total_volume_usd', 0.0),
            funding_rate=data.get('funding_rate', 0.0)
        )


class ScoringEngine:
    """评分引擎"""

    def __init__(self):
        """初始化评分引擎"""
        self.weights = {
            'contract': 0.45,
            'oi_volume_ratio': 0.30,
            'oi_rank': 0.15,
            'technical': 0.35,
            'sentiment': 0.20
        }

        self.veto_thresholds = {
            'listing_hours': 48
        }

        self.entry_threshold = 6.5

        print("✅ 评分引擎初始化完成")

    def calculate_oi_volume_ratio_score(self, oi_volume_ratio: float) -> Tuple[float, str]:
        """
        计算OI/总交易量比率评分
        
        逻辑说明：
        - OI代表存量杠杆
        - 总交易量代表增量热钱的累积
        - 高比率意味着市场活跃度下降但存量杠杆高企，是理想的做空时机
        
        评分标准（基于实际数据分布调整）：
        - 比率 > 0.06 (95%分位): 极高，存量杠杆远超增量热钱，最佳做空时机 (10分)
        - 比率 > 0.045 (90%分位): 高，市场脆弱 (7分)
        - 比率 > 0.025 (75%分位): 中等 (4分)
        - 比率 <= 0.025: 低，市场活跃度高 (1分)

        Args:
            oi_volume_ratio: OI/总交易量比率

        Returns:
            (score, reason) 元组
        """
        if oi_volume_ratio > 0.06:
            score = 10.0
            reason = f"极高，存量杠杆远超增量热钱 ({oi_volume_ratio:.4f})"
        elif oi_volume_ratio > 0.045:
            score = 7.0
            reason = f"高，市场脆弱 ({oi_volume_ratio:.4f})"
        elif oi_volume_ratio > 0.025:
            score = 4.0
            reason = f"中等 ({oi_volume_ratio:.4f})"
        else:
            score = 1.0
            reason = f"低，市场活跃度高 ({oi_volume_ratio:.4f})"

        return score, reason

    def calculate_oi_rank_score(
        self, 
        oi_usd: float, 
        recent_coins_oi: List[float]
    ) -> Tuple[float, str]:
        """
        计算OI绝对值排名评分
        
        与前10个新币的OI对比，OI越大说明市场关注度越高，做空价值越大

        Args:
            oi_usd: 当前币种的OI（USD）
            recent_coins_oi: 最近10个新币的OI列表

        Returns:
            (score, reason) 元组
        """
        if not recent_coins_oi or len(recent_coins_oi) == 0:
            return 5.0, "无对比数据，默认中等评分"

        sorted_oi = sorted(recent_coins_oi, reverse=True)
        rank = 1
        for i, oi in enumerate(sorted_oi):
            if oi_usd >= oi:
                rank = i + 1
                break
            rank = len(sorted_oi) + 1

        total = len(sorted_oi)
        percentile = (total - rank + 1) / total

        if percentile >= 0.8:
            score = 10.0
            reason = f"OI排名前20% (排名{rank}/{total}, OI=${oi_usd:,.0f})"
        elif percentile >= 0.6:
            score = 7.0
            reason = f"OI排名前40% (排名{rank}/{total}, OI=${oi_usd:,.0f})"
        elif percentile >= 0.4:
            score = 4.0
            reason = f"OI排名中等 (排名{rank}/{total}, OI=${oi_usd:,.0f})"
        else:
            score = 1.0
            reason = f"OI排名较低 (排名{rank}/{total}, OI=${oi_usd:,.0f})"

        return score, reason

    def calculate_contract_score(
        self,
        oi_volume_ratio: float,
        oi_usd: float,
        recent_coins_oi: List[float] = None
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算合约数据综合评分（权重45%）

        Args:
            oi_volume_ratio: OI/总交易量比率
            oi_usd: OI（USD）
            recent_coins_oi: 最近新币的OI列表（用于排名对比）

        Returns:
            (总分, 详情) 元组
        """
        oi_ratio_score, oi_ratio_reason = self.calculate_oi_volume_ratio_score(oi_volume_ratio)
        
        if recent_coins_oi:
            oi_rank_score, oi_rank_reason = self.calculate_oi_rank_score(oi_usd, recent_coins_oi)
        else:
            oi_rank_score, oi_rank_reason = 5.0, "无对比数据"

        weighted_score = (
            oi_ratio_score * self.weights['oi_volume_ratio'] / self.weights['contract'] +
            oi_rank_score * self.weights['oi_rank'] / self.weights['contract']
        )

        details = {
            'oi_volume_ratio_score': oi_ratio_score,
            'oi_volume_ratio_reason': oi_ratio_reason,
            'oi_rank_score': oi_rank_score,
            'oi_rank_reason': oi_rank_reason,
            'weighted_score': weighted_score
        }

        return weighted_score, details

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
        计算技术面评分（V4.1 - 纯形态评分）

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

    def calculate_sentiment_score(self, funding_rate: float) -> Tuple[float, str]:
        """
        计算情绪面评分（V4.1 - 基于资金费率）

        Args:
            funding_rate: 资金费率（小数形式，如0.0001）

        Returns:
            (score, reason) 元组
        """
        annual_rate = funding_rate * 3 * 365 * 100

        if annual_rate > 150:
            score = 10.0
            reason = f"资金费率极高 ({annual_rate:.1f}%/年, {funding_rate:.6f}/8h)"
        elif annual_rate > 100:
            score = 7.0
            reason = f"资金费率较高 ({annual_rate:.1f}%/年, {funding_rate:.6f}/8h)"
        elif annual_rate > 50:
            score = 4.0
            reason = f"资金费率中等 ({annual_rate:.1f}%/年, {funding_rate:.6f}/8h)"
        elif annual_rate > 0:
            score = 1.0
            reason = f"资金费率较低 ({annual_rate:.1f}%/年, {funding_rate:.6f}/8h)"
        else:
            score = 0.0
            reason = f"资金费率为负 ({annual_rate:.1f}%/年, {funding_rate:.6f}/8h)"

        return score, reason

    def check_veto(self, listing_hours: float) -> Tuple[bool, Optional[str]]:
        """
        检查一票否决条件

        Args:
            listing_hours: 上线时间（小时）

        Returns:
            (veto, reason) 元组
        """
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

        if technical_score < 4:
            return False, f"技术总分不足 ({technical_score:.1f} < 4)"

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
        result: ScoringResult,
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

    def score(
        self,
        symbol: str,
        oi_usd: float,
        total_volume_usd: float,
        funding_rate: float,
        three_tops_detected: bool,
        three_tops_score: float,
        long_upper_shadow: bool,
        long_upper_shadow_score: float,
        volume_divergence: bool,
        volume_divergence_score: float,
        listing_hours: float,
        current_price: float,
        recent_coins_oi: List[float] = None
    ) -> ScoringResult:
        """
        执行完整评分

        Args:
            symbol: 交易对
            oi_usd: OI（USD）
            total_volume_usd: 上线以来总交易量（USD）
            funding_rate: 资金费率
            three_tops_detected: 是否检测到三次冲顶
            three_tops_score: 三次冲顶得分
            long_upper_shadow: 是否检测到长上影线
            long_upper_shadow_score: 长上影线得分
            volume_divergence: 是否检测到放量滞涨
            volume_divergence_score: 放量滞涨得分
            listing_hours: 上线时间（小时）
            current_price: 当前价格
            recent_coins_oi: 最近新币的OI列表

        Returns:
            评分结果
        """
        oi_volume_ratio = oi_usd / total_volume_usd if total_volume_usd > 0 else 0

        contract_score, contract_details = self.calculate_contract_score(
            oi_volume_ratio, oi_usd, recent_coins_oi
        )

        technical_score, technical_details = self.calculate_technical_score(
            three_tops_detected, three_tops_score,
            long_upper_shadow, long_upper_shadow_score,
            volume_divergence, volume_divergence_score
        )

        sentiment_score, sentiment_reason = self.calculate_sentiment_score(funding_rate)

        veto, veto_reason = self.check_veto(listing_hours)

        total_score = self.calculate_total_score(
            contract_score, technical_score, sentiment_score
        )

        return ScoringResult(
            symbol=symbol,
            contract_score=contract_score,
            oi_volume_ratio_score=contract_details['oi_volume_ratio_score'],
            oi_rank_score=contract_details['oi_rank_score'],
            technical_score=technical_score,
            sentiment_score=sentiment_score,
            total_score=total_score,
            veto=veto,
            veto_reason=veto_reason,
            timestamp=datetime.now(),
            current_price=current_price,
            oi_volume_ratio=oi_volume_ratio,
            oi_usd=oi_usd,
            total_volume_usd=total_volume_usd,
            funding_rate=funding_rate
        )


scoring_engine = ScoringEngine()
