"""
评分引擎模块
计算新币做空的综合评分，基于合约数据、技术面和情绪面
"""
from typing import Dict, Any, Tuple, List, Optional
from dataclasses import dataclass
import structlog


logger = structlog.get_logger()


@dataclass
class ScoringResult:
    """评分结果数据类"""
    symbol: str
    total_score: float
    contract_score: float
    technical_score: float
    sentiment_score: float
    veto: bool
    veto_reason: Optional[str]
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'total_score': self.total_score,
            'contract_score': self.contract_score,
            'technical_score': self.technical_score,
            'sentiment_score': self.sentiment_score,
            'veto': self.veto,
            'veto_reason': self.veto_reason,
            'details': self.details
        }


class ScoringEngine:
    """评分引擎

    基于多维度评分系统，综合评估新币的做空机会：
    - 合约数据评分（45%）：OI/总交易量比率、OI排名
    - 技术面评分（35%）：三次冲顶、长上影线、放量滞涨
    - 情绪面评分（20%）：资金费率 + OI变化率（双因子，各0~5分）
    """

    # 资金费率年化计算常量
    # 币安每8小时结算一次，一天3次，一年365天
    FUNDING_SETTLEMENTS_PER_DAY = 3   # 每天结算次数
    DAYS_PER_YEAR = 365               # 一年天数

    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化评分引擎

        Args:
            config: 配置字典，包含评分权重和阈值
        """
        self.config = config or {}

        # 评分权重配置
        scoring_config = self.config.get('scoring', {})
        weights = scoring_config.get('weights', {})
        self.weights = {
            'contract': weights.get('contract', 0.45),
            'oi_volume_ratio': weights.get('oi_volume_ratio', 0.30),
            'oi_rank': weights.get('oi_rank', 0.15),
            'technical': weights.get('technical', 0.35),
            'sentiment': weights.get('sentiment', 0.20)
        }

        # 一票否决阈值
        veto_thresholds = scoring_config.get('veto_thresholds', {})
        self.veto_thresholds = {
            'listing_hours': veto_thresholds.get('listing_hours', 48)
        }

        # 入场阈值（V4.1：保持5.0，通过min_total_score和降级模式约束提升质量）
        self.entry_threshold = scoring_config.get('entry_threshold', 5.0)

        logger.info(
            "评分引擎初始化完成",
            weights=self.weights,
            entry_threshold=self.entry_threshold
        )

    def _calc_annualized_rate(self, funding_rate: float) -> float:
        """
        计算资金费率的年化费率（百分比）

        公式：年化费率(%) = 资金费率 × 每天结算次数 × 一年天数 × 100

        Args:
            funding_rate: 资金费率（原始费率，如0.0001表示0.01%）

        Returns:
            年化费率（百分比，如0.0001 → 10.95）
        """
        return funding_rate * self.FUNDING_SETTLEMENTS_PER_DAY * self.DAYS_PER_YEAR * 100

    def calculate_oi_volume_ratio_score(
        self,
        oi_volume_ratio: float
    ) -> Tuple[float, str]:
        """
        计算OI/总交易量比率评分（V4.0）

        评分标准：
        - < 0.2: 10分（优秀，存量杠杆相对增量热钱较小）
        - 0.2 ~ 0.3: 7分（良好）
        - 0.3 ~ 0.4: 3分（偏高，谨慎）
        - 0.4 ~ 0.5: 0分（极危险，需手动复核）
        - > 0.5: 一票否决（直接放弃）

        逻辑：比率越低，代表存量杠杆相对于增量热钱越小，市场越安全，得分越高。

        Args:
            oi_volume_ratio: OI/总交易量比率

        Returns:
            (评分, 原因说明)
        """
        # 从配置文件读取OI/总交易量比率评分阈值和分数
        oi_config = self.config.get('scoring', {}).get('oi_volume_ratio', {})
        thresholds = oi_config.get('thresholds', {})
        scores = oi_config.get('scores', {})

        # 获取阈值配置（带默认值）
        veto_threshold = thresholds.get('veto', 0.5)
        danger_threshold = thresholds.get('danger', 0.4)
        caution_threshold = thresholds.get('caution', 0.3)
        good_threshold = thresholds.get('good', 0.2)

        # 获取分数配置（带默认值）
        veto_score = scores.get('veto', -1.0)
        danger_score = scores.get('danger', 0.0)
        caution_score = scores.get('caution', 3.0)
        good_score = scores.get('good', 7.0)
        excellent_score = scores.get('excellent', 10.0)

        # 根据阈值判断评分
        if oi_volume_ratio > veto_threshold:
            # 一票否决情况，返回-1标记
            score = veto_score
            reason = f"一票否决，比率过高 ({oi_volume_ratio:.4f} > {veto_threshold})"
        elif oi_volume_ratio >= danger_threshold:
            score = danger_score
            reason = f"极危险，需手动复核 ({oi_volume_ratio:.4f})"
        elif oi_volume_ratio >= caution_threshold:
            score = caution_score
            reason = f"偏高，谨慎 ({oi_volume_ratio:.4f})"
        elif oi_volume_ratio >= good_threshold:
            score = good_score
            reason = f"良好 ({oi_volume_ratio:.4f})"
        else:
            score = excellent_score
            reason = f"优秀 ({oi_volume_ratio:.4f})"

        logger.debug(
            "OI/总交易量比率评分",
            ratio=oi_volume_ratio,
            score=score,
            reason=reason
        )

        return score, reason

    def calculate_oi_rank_score(
        self,
        oi_usd: float,
        recent_coins_oi: List[float]
    ) -> Tuple[float, str]:
        """
        计算OI绝对值排名评分

        评分标准：
        - 排名前10%: 10分
        - 排名前30%: 7分
        - 排名前50%: 5分
        - 排名后50%: 3分

        Args:
            oi_usd: 当前OI（美元）
            recent_coins_oi: 最近新币的OI列表

        Returns:
            (评分, 原因说明)
        """
        if not recent_coins_oi or len(recent_coins_oi) == 0:
            return 5.0, "无历史数据对比"

        # 计算排名
        sorted_oi = sorted(recent_coins_oi, reverse=True)
        rank = 0
        for i, oi in enumerate(sorted_oi):
            if oi_usd >= oi:
                rank = i + 1
                break
            rank = len(sorted_oi)

        percentile = rank / len(sorted_oi)

        if percentile <= 0.1:
            score = 10.0
            reason = f"排名前10% (${oi_usd:,.0f})"
        elif percentile <= 0.3:
            score = 7.0
            reason = f"排名前30% (${oi_usd:,.0f})"
        elif percentile <= 0.5:
            score = 5.0
            reason = f"排名前50% (${oi_usd:,.0f})"
        else:
            score = 3.0
            reason = f"排名后50% (${oi_usd:,.0f})"

        logger.debug(
            "OI排名评分",
            oi_usd=oi_usd,
            rank=rank,
            percentile=percentile,
            score=score
        )

        return score, reason

    def calculate_contract_score(
        self,
        oi_volume_ratio: float,
        oi_usd: float,
        recent_coins_oi: List[float] = None
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算合约数据综合评分（权重45%）

        包含：
        - OI/总交易量比率（权重30%）
        - OI排名（权重15%）

        Args:
            oi_volume_ratio: OI/总交易量比率
            oi_usd: OI金额（美元）
            recent_coins_oi: 最近新币的OI列表

        Returns:
            (综合评分, 详细信息)
        """
        # 计算OI/总交易量比率评分
        ratio_score, ratio_reason = self.calculate_oi_volume_ratio_score(oi_volume_ratio)

        # 计算OI排名评分
        rank_score, rank_reason = self.calculate_oi_rank_score(oi_usd, recent_coins_oi or [])

        # 加权计算
        total_score = (
            ratio_score * self.weights['oi_volume_ratio'] / self.weights['contract'] +
            rank_score * self.weights['oi_rank'] / self.weights['contract']
        )

        details = {
            'oi_volume_ratio_score': ratio_score,
            'oi_volume_ratio_reason': ratio_reason,
            'oi_rank_score': rank_score,
            'oi_rank_reason': rank_reason,
            'weighted_score': total_score
        }

        logger.info(
            "合约数据评分完成",
            total_score=total_score,
            ratio_score=ratio_score,
            rank_score=rank_score
        )

        return total_score, details

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

        评分标准：
        - 三次冲顶：最高4分
        - 长上影线：最高3分
        - 放量滞涨：最高3分
        - 总分最高10分

        Args:
            three_tops_detected: 是否检测到三次冲顶
            three_tops_score: 三次冲顶评分
            long_upper_shadow: 是否检测到长上影线
            long_upper_shadow_score: 长上影线评分
            volume_divergence: 是否检测到放量滞涨
            volume_divergence_score: 放量滞涨评分

        Returns:
            (技术面评分, 详细信息)
        """
        # 累加形态评分
        total_score = three_tops_score + long_upper_shadow_score + volume_divergence_score

        details = {
            'three_tops_detected': three_tops_detected,
            'three_tops_score': three_tops_score,
            'long_upper_shadow': long_upper_shadow,
            'long_upper_shadow_score': long_upper_shadow_score,
            'volume_divergence': volume_divergence,
            'volume_divergence_score': volume_divergence_score,
            'total_score': total_score
        }

        logger.info(
            "技术面评分完成",
            total_score=total_score,
            three_tops=three_tops_score,
            long_upper_shadow=long_upper_shadow_score,
            volume_divergence=volume_divergence_score
        )

        return total_score, details

    def calculate_sentiment_score(
        self,
        funding_rate: float,
        oi_change_rate: float,
        sentiment_degraded: bool = False
    ) -> Tuple[float, str]:
        """
        计算情绪面评分（V4.1 动态模式版）

        完整模式（上线>=3h且OI数据可用）：资金费率（0~5分）+ OI变化率（0~5分），总分10分
        降级模式（上线<3h或OI数据缺失）：仅资金费率，映射到10分制（乘以配置的乘数）

        Args:
            funding_rate: 资金费率（原始费率，如0.0001表示0.01%）
            oi_change_rate: OI变化率（小数，如0.5表示50%增长）
            sentiment_degraded: 是否使用降级模式

        Returns:
            (评分, 原因说明)
        """
        # 计算年化费率（使用私有方法，避免硬编码结算次数和天数常量）
        annualized_rate = self._calc_annualized_rate(funding_rate)

        # 1. 资金费率子因子评分（0~5分）
        fr_config = self.config.get('scoring', {}).get('sentiment', {}).get('funding_rate', {})
        fr_thresholds = fr_config.get('thresholds', {})
        fr_scores = fr_config.get('scores', {})

        # 获取资金费率阈值配置（带默认值），用于评分判断和原因说明
        # V4.1：删除mild分支，年化费率<50%直接计0分，避免中等费率虚高情绪分
        extreme_threshold = fr_thresholds.get('extreme', 100)
        greed_threshold = fr_thresholds.get('greed', 50)

        if annualized_rate > extreme_threshold:
            fr_score = fr_scores.get('extreme', 5.0)
            fr_reason = f"年化费率 {annualized_rate:.1f}% > {extreme_threshold}%"
        elif annualized_rate >= greed_threshold:
            fr_score = fr_scores.get('greed', 3.0)
            fr_reason = f"年化费率 {annualized_rate:.1f}% ({greed_threshold}%~{extreme_threshold}%)"
        else:
            fr_score = fr_scores.get('neutral', 0.0)
            fr_reason = f"年化费率 {annualized_rate:.1f}% < {greed_threshold}%"

        # C+D方案：根据降级标志切换评分模式
        if sentiment_degraded:
            # 降级模式：仅资金费率，映射到10分制
            degraded_config = self.config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {})
            fr_multiplier = degraded_config.get('funding_rate_multiplier', 2)
            total_score = fr_score * fr_multiplier

            # V4.1新增：降级模式情绪分上限截断
            # 避免资金费率中等时情绪分虚高，最高不超过 max_sentiment_score（默认6.0）
            max_sentiment_score = degraded_config.get('max_sentiment_score', 6.0)
            if total_score > max_sentiment_score:
                logger.info(
                    "降级模式情绪分触发上限截断",
                    original_score=total_score,
                    max_sentiment_score=max_sentiment_score
                )
                total_score = max_sentiment_score

            reason = f"[降级] 费率 {fr_score}/5*{fr_multiplier} = {total_score}/10 ({fr_reason})"

            logger.info("情绪面评分(降级模式)",
                        annualized_rate=annualized_rate, fr_score=fr_score,
                        fr_multiplier=fr_multiplier, total=total_score,
                        max_sentiment_score=max_sentiment_score)
        else:
            # 完整模式：资金费率 + OI变化率
            oi_config = self.config.get('scoring', {}).get('sentiment', {}).get('oi_change', {})
            oi_thresholds = oi_config.get('thresholds', {})
            oi_scores = oi_config.get('scores', {})

            oi_change_pct = oi_change_rate * 100  # 转为百分比

            # 获取OI变化率阈值配置（带默认值），用于评分判断和原因说明
            oi_extreme_threshold = oi_thresholds.get('extreme', 50)
            oi_greed_threshold = oi_thresholds.get('greed', 30)
            oi_mild_threshold = oi_thresholds.get('mild', 10)

            if oi_change_pct >= oi_extreme_threshold:
                oi_score = oi_scores.get('extreme', 5.0)
                oi_reason = f"OI增长 {oi_change_pct:.1f}% >= {oi_extreme_threshold}%"
            elif oi_change_pct >= oi_greed_threshold:
                oi_score = oi_scores.get('greed', 3.0)
                oi_reason = f"OI增长 {oi_change_pct:.1f}% ({oi_greed_threshold}%~{oi_extreme_threshold}%)"
            elif oi_change_pct >= oi_mild_threshold:
                oi_score = oi_scores.get('mild', 1.0)
                oi_reason = f"OI增长 {oi_change_pct:.1f}% ({oi_mild_threshold}%~{oi_greed_threshold}%)"
            else:
                oi_score = oi_scores.get('neutral', 0.0)
                oi_reason = f"OI增长 {oi_change_pct:.1f}% < {oi_mild_threshold}%"

            total_score = fr_score + oi_score
            reason = f"[完整] 费率 {fr_score}/5 ({fr_reason}) + OI变化 {oi_score}/5 ({oi_reason})"

            logger.debug("情绪面评分(完整模式)",
                         annualized_rate=annualized_rate, fr_score=fr_score,
                         oi_change_pct=oi_change_pct, oi_score=oi_score,
                         total=total_score)

        return total_score, reason

    def check_veto(
        self,
        listing_hours: float,
        oi_volume_ratio: float = None
    ) -> Tuple[bool, Optional[str]]:
        """
        检查一票否决条件（V4.0）

        一票否决条件：
        1. 上线时间 > 48小时
        2. OI/总交易量比率 > 0.5

        Args:
            listing_hours: 上线时长（小时）
            oi_volume_ratio: OI/总交易量比率（可选）

        Returns:
            (是否否决, 否决原因)
        """
        # 检查上线时间
        if listing_hours > self.veto_thresholds['listing_hours']:
            reason = f"上线时间过长 ({listing_hours:.1f}小时)"
            logger.warning(f"一票否决: {reason}")
            return True, reason

        # 检查OI/总交易量比率（从配置读取一票否决阈值，避免硬编码）
        oi_veto_threshold = self.config.get('scoring', {}).get('oi_volume_ratio', {}).get('thresholds', {}).get('veto', 0.5)
        if oi_volume_ratio is not None and oi_volume_ratio > oi_veto_threshold:
            reason = f"OI/总交易量比率过高 ({oi_volume_ratio:.4f} > {oi_veto_threshold})"
            logger.warning(f"一票否决: {reason}")
            return True, reason

        return False, None

    def score(
        self,
        symbol: str,
        oi_usd: float,
        total_volume_usd: float,
        funding_rate: float,
        oi_change_rate: float,
        three_tops_detected: bool,
        three_tops_score: float,
        long_upper_shadow: bool,
        long_upper_shadow_score: float,
        volume_divergence: bool,
        volume_divergence_score: float,
        listing_hours: float,
        current_price: float,
        recent_coins_oi: List[float] = None,
        sentiment_degraded: bool = False
    ) -> ScoringResult:
        """
        执行完整评分

        Args:
            symbol: 交易对
            oi_usd: OI金额（美元）
            total_volume_usd: 总交易量（美元）
            funding_rate: 资金费率
            oi_change_rate: OI变化率（小数，如0.5表示50%增长）
            three_tops_detected: 是否检测到三次冲顶
            three_tops_score: 三次冲顶评分
            long_upper_shadow: 是否检测到长上影线
            long_upper_shadow_score: 长上影线评分
            volume_divergence: 是否检测到放量滞涨
            volume_divergence_score: 放量滞涨评分
            listing_hours: 上线时长（小时）
            current_price: 当前价格
            recent_coins_oi: 最近新币的OI列表
            sentiment_degraded: 是否使用情绪面降级模式（上线<3h或OI数据缺失）

        Returns:
            评分结果
        """
        logger.info(f"开始评分: {symbol}")

        # 1. 计算OI/总交易量比率
        oi_volume_ratio = oi_usd / total_volume_usd if total_volume_usd > 0 else 0

        # 2. 检查一票否决
        veto, veto_reason = self.check_veto(listing_hours, oi_volume_ratio)

        if veto:
            return ScoringResult(
                symbol=symbol,
                total_score=0.0,
                contract_score=0.0,
                technical_score=0.0,
                sentiment_score=0.0,
                veto=True,
                veto_reason=veto_reason,
                details={'reason': '一票否决', 'oi_volume_ratio': oi_volume_ratio}
            )

        # 3. 计算合约数据评分（45%）
        contract_score, contract_details = self.calculate_contract_score(
            oi_volume_ratio=oi_volume_ratio,
            oi_usd=oi_usd,
            recent_coins_oi=recent_coins_oi
        )

        # 4. 计算技术面评分（35%）
        technical_score, technical_details = self.calculate_technical_score(
            three_tops_detected=three_tops_detected,
            three_tops_score=three_tops_score,
            long_upper_shadow=long_upper_shadow,
            long_upper_shadow_score=long_upper_shadow_score,
            volume_divergence=volume_divergence,
            volume_divergence_score=volume_divergence_score
        )

        # 5. 计算情绪面评分（C+D方案：根据降级标志切换模式）
        sentiment_score, sentiment_reason = self.calculate_sentiment_score(
            funding_rate, oi_change_rate, sentiment_degraded
        )

        # 6. 计算总分
        total_score = (
            contract_score * self.weights['contract'] +
            technical_score * self.weights['technical'] +
            sentiment_score * self.weights['sentiment']
        )

        # 7. 构建详细信息
        details = {
            'listing_hours': listing_hours,
            'current_price': current_price,
            'oi_usd': oi_usd,
            'total_volume_usd': total_volume_usd,
            'oi_volume_ratio': oi_volume_ratio,
            'funding_rate': funding_rate,
            'contract': contract_details,
            'technical': technical_details,
            'sentiment': {
                'score': sentiment_score,
                'reason': sentiment_reason,
                'degraded': sentiment_degraded,
                'annualized_rate': self._calc_annualized_rate(funding_rate),
                'oi_change_rate': oi_change_rate,
                'oi_change_pct': oi_change_rate * 100
            }
        }

        result = ScoringResult(
            symbol=symbol,
            total_score=total_score,
            contract_score=contract_score,
            technical_score=technical_score,
            sentiment_score=sentiment_score,
            veto=False,
            veto_reason=None,
            details=details
        )

        logger.info(
            f"评分完成: {symbol}",
            total_score=total_score,
            contract_score=contract_score,
            technical_score=technical_score,
            sentiment_score=sentiment_score
        )

        return result

    def should_entry(
        self,
        score_result: ScoringResult,
        three_tops_score: float,
        total_technical_score: float,
        sentiment_degraded: bool = False
    ) -> bool:
        """
        判断是否应该入场（V4.1 - 增加降级模式额外约束）

        入场条件（必须同时满足）：
        1. 无一票否决
        2. 总分 >= 入场阈值（配置项 entry_threshold）
        3. 技术总分 >= 技术面最低要求（配置项 min_total_score）
        4. 三次冲顶评分 >= 三次冲顶最低要求（配置项 min_three_tops_score）
        5. V4.1新增：降级模式下，技术总分必须 >= 降级模式配置的 min_technical_score

        Args:
            score_result: 评分结果
            three_tops_score: 三次冲顶评分
            total_technical_score: 技术面总分
            sentiment_degraded: 是否使用情绪面降级模式（V4.1新增，默认False保持向后兼容）

        Returns:
            是否应该入场
        """
        # 检查一票否决
        if score_result.veto:
            logger.info("一票否决，不入场")
            return False

        # 检查总分阈值
        if score_result.total_score < self.entry_threshold:
            logger.info(
                f"评分未达阈值，不入场",
                total_score=score_result.total_score,
                threshold=self.entry_threshold
            )
            return False

        # 检查技术面硬性要求（必须同时满足）
        # 从配置文件读取技术面评分阈值（V4.1：默认值对齐配置项 min_total_score=7.0、min_three_tops_score=3.0）
        technical_config = self.config.get('scoring', {}).get('technical', {})
        min_total_score = technical_config.get('min_total_score', 7.0)
        min_three_tops_score = technical_config.get('min_three_tops_score', 3.0)

        if not (total_technical_score >= min_total_score and three_tops_score >= min_three_tops_score):
            logger.info(
                "技术面硬性要求不满足，不入场",
                technical_score=total_technical_score,
                three_tops_score=three_tops_score,
                requirement=f"技术总分≥{min_total_score} 且 三次冲顶≥{min_three_tops_score}"
            )
            return False

        # V4.1新增：降级模式额外约束
        # 降级模式下，技术分必须达到更严格的标准（min_technical_score，默认7.0）
        # 因为降级模式缺乏OI变化率数据，需要用更严格的技术面要求补偿信号质量
        if sentiment_degraded:
            degraded_config = self.config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {})
            degraded_min_technical = degraded_config.get('min_technical_score', 7.0)
            if total_technical_score < degraded_min_technical:
                logger.info(
                    "降级模式技术分约束不满足，不入场",
                    technical_score=total_technical_score,
                    degraded_min_technical=degraded_min_technical
                )
                return False

        logger.info(
            "满足入场条件",
            total_score=score_result.total_score,
            technical_score=total_technical_score,
            three_tops_score=three_tops_score,
            sentiment_degraded=sentiment_degraded
        )
        return True
