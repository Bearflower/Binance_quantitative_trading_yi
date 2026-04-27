"""
综合评分引擎

负责：
- 整合 4 个维度的评分
- 计算综合得分
- 应用一票否决机制
- 生成评分报告
"""

from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

from utils.logger import logger


@dataclass
class ScoringResult:
    """评分结果数据类"""
    
    symbol: str
    contract_score: float  # 合约数据评分
    fundamental_score: float  # 基本面评分
    technical_score: float  # 技术面评分
    sentiment_score: float  # 情绪面评分
    total_score: float  # 综合评分
    veto: bool  # 是否被否决
    veto_reason: Optional[str]  # 否决原因
    timestamp: datetime  # 评分时间
    current_price: float = 0.0  # 当前价格
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'contract_score': self.contract_score,
            'fundamental_score': self.fundamental_score,
            'technical_score': self.technical_score,
            'sentiment_score': self.sentiment_score,
            'total_score': self.total_score,
            'veto': self.veto,
            'veto_reason': self.veto_reason,
            'timestamp': self.timestamp.isoformat(),
            'current_price': self.current_price
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScoringResult':
        """从字典创建"""
        return cls(
            symbol=data['symbol'],
            contract_score=data['contract_score'],
            fundamental_score=data['fundamental_score'],
            technical_score=data['technical_score'],
            sentiment_score=data['sentiment_score'],
            total_score=data['total_score'],
            veto=data['veto'],
            veto_reason=data.get('veto_reason'),
            timestamp=datetime.fromisoformat(data['timestamp']),
            current_price=data.get('current_price', 0.0)
        )


class ScoringEngine:
    """综合评分引擎"""
    
    def __init__(self):
        """初始化评分引擎"""
        # 权重配置 - 激进模式（适合新币短线交易）
        self.weights = {
            'contract': 0.35,    # 合约数据 35%
            'fundamental': 0.20, # 基本面 20%（降低权重，适合短线）
            'technical': 0.35,   # 技术面 35%（提高权重，技术为主）
            'sentiment': 0.10    # 情绪面 10%
        }
        
        # 一票否决阈值
        self.veto_thresholds = {
            'oi_ratio': 1.0,     # OI/市值比 > 1.0 否决
            'listing_hours': 168, # 上线 > 168 小时 (7 天) 否决
        }
        
        # 开仓阈值（激进模式降低到 6.0）
        self.entry_threshold = 6.0  # 综合评分 >= 6.0 开仓
        
        logger.info("✅ 综合评分引擎初始化完成（激进模式）")
    
    def calculate_total_score(
        self,
        contract_score: float,
        fundamental_score: float,
        technical_score: float,
        sentiment_score: float
    ) -> float:
        """
        计算综合评分
        
        Args:
            contract_score: 合约数据评分 (0-10)
            fundamental_score: 基本面评分 (0-10)
            technical_score: 技术面评分 (0-10)
            sentiment_score: 情绪面评分 (0-10)
            
        Returns:
            综合评分 (0-10)
        """
        total_score = (
            contract_score * self.weights['contract'] +
            fundamental_score * self.weights['fundamental'] +
            technical_score * self.weights['technical'] +
            sentiment_score * self.weights['sentiment']
        )
        
        # 保留 2 位小数
        total_score = round(total_score, 2)
        
        logger.debug(
            f"📊 综合评分计算："
            f"合约={contract_score:.1f}×{self.weights['contract']:.2f} + "
            f"基本={fundamental_score:.1f}×{self.weights['fundamental']:.2f} + "
            f"技术={technical_score:.1f}×{self.weights['technical']:.2f} + "
            f"情绪={sentiment_score:.1f}×{self.weights['sentiment']:.2f} = "
            f"{total_score:.2f}"
        )
        
        return total_score
    
    def check_veto(
        self,
        oi_ratio: float,
        listing_hours: float
    ) -> Tuple[bool, Optional[str]]:
        """
        检查一票否决条件
        
        Args:
            oi_ratio: OI/市值比
            listing_hours: 上线至今小时数
            
        Returns:
            (veto, reason) 元组:
            - veto: 是否被否决
            - reason: 否决原因
        """
        # 检查 OI/市值比
        if oi_ratio > self.veto_thresholds['oi_ratio']:
            logger.warning(
                f"⚠️ 一票否决：OI/市值比={oi_ratio:.4f} > "
                f"{self.veto_thresholds['oi_ratio']}"
            )
            return True, f"OI/市值比过高 ({oi_ratio:.4f})"
        
        # 检查上线时间
        if listing_hours > self.veto_thresholds['listing_hours']:
            logger.warning(
                f"⚠️ 一票否决：上线时间={listing_hours:.1f}小时 > "
                f"{self.veto_thresholds['listing_hours']}小时"
            )
            return True, f"上线时间过长 ({listing_hours:.1f}小时)"
        
        return False, None
    
    def generate_scoring_report(
        self,
        symbol: str,
        contract_score: float,
        fundamental_score: float,
        technical_score: float,
        sentiment_score: float,
        oi_ratio: float,
        listing_hours: float,
        listing_time: Optional[datetime] = None,
        scoring_attempt: int = 1,
        additional_details: Optional[Dict[str, Any]] = None,
        current_price: float = 0.0
    ) -> ScoringResult:
        """
        生成完整评分报告
        
        Args:
            symbol: 币种符号
            contract_score: 合约数据评分
            fundamental_score: 基本面评分
            technical_score: 技术面评分
            sentiment_score: 情绪面评分
            oi_ratio: OI/市值比
            listing_hours: 上线时间 (小时)
            listing_time: 上线时间
            scoring_attempt: 第几次评分
            additional_details: 额外的详细信息
            current_price: 当前价格
            
        Returns:
            评分结果对象
        """
        # 检查一票否决
        veto, veto_reason = self.check_veto(oi_ratio, listing_hours)
        
        # 计算综合评分
        total_score = self.calculate_total_score(
            contract_score,
            fundamental_score,
            technical_score,
            sentiment_score
        )
        
        # 如果被否决，综合评分置为 0
        if veto:
            total_score = 0.0
            logger.warning(
                f"❌ {symbol} 被一票否决：{veto_reason}, 综合评分置为 0"
            )
        
        # 创建评分结果
        result = ScoringResult(
            symbol=symbol,
            contract_score=contract_score,
            fundamental_score=fundamental_score,
            technical_score=technical_score,
            sentiment_score=sentiment_score,
            total_score=total_score,
            veto=veto,
            veto_reason=veto_reason,
            timestamp=datetime.now(),
            current_price=current_price
        )
        
        # 生成并保存报告
        try:
            from core.report_manager import report_manager
            from utils.scoring_logger import scoring_logger
            
            # 构建详细评分数据
            scores = {
                "contract": {
                    "score": contract_score,
                    "weight": 0.35,
                    "weighted_score": round(contract_score * 0.35, 2),
                    "reason": self._get_contract_reason(contract_score),
                    "details": {
                        "oi_usd": None,
                        "market_cap": None,
                        "oi_ratio": oi_ratio
                    }
                },
                "fundamental": {
                    "score": fundamental_score,
                    "weight": 0.30,
                    "weighted_score": round(fundamental_score * 0.30, 2),
                    "reason": self._get_fundamental_reason(fundamental_score),
                    "details": {
                        "unlock_percentage": 0,
                        "unlock_scale": "unknown"
                    }
                },
                "technical": {
                    "score": technical_score,
                    "weight": 0.25,
                    "weighted_score": round(technical_score * 0.25, 2),
                    "reason": self._get_technical_reason(technical_score),
                    "details": {
                        "trend": "unknown",
                        "rsi": None,
                        "atr_ratio": None,
                        "data_points": 0
                    }
                },
                "sentiment": {
                    "score": sentiment_score,
                    "weight": 0.10,
                    "weighted_score": round(sentiment_score * 0.10, 2),
                    "reason": self._get_sentiment_reason(sentiment_score),
                    "details": {
                        "funding_rate": None,
                        "annual_rate": None
                    }
                }
            }
            
            # 生成传统报告
            report = report_manager.generate_report(
                symbol=symbol,
                listing_time=listing_time or datetime.now(),
                scores=scores,
                total_score=total_score,
                threshold=self.entry_threshold,
                veto=veto,
                veto_reason=veto_reason
            )
            
            # 保存传统报告
            report_manager.save_report(report, symbol)
            
            # 保存评分日志（新功能）
            scoring_data = {
                "listing_time": listing_time.isoformat() if listing_time else None,
                "hours_since_listing": listing_hours,
                "scores": {
                    "contract_score": {
                        "score": contract_score,
                        "reason": self._get_contract_reason(contract_score),
                        "data_available": True
                    },
                    "fundamental_score": {
                        "score": fundamental_score,
                        "reason": self._get_fundamental_reason(fundamental_score),
                        "data_available": True
                    },
                    "technical_score": {
                        "score": technical_score,
                        "reason": self._get_technical_reason(technical_score),
                        "data_available": True
                    },
                    "sentiment_score": {
                        "score": sentiment_score,
                        "reason": self._get_sentiment_reason(sentiment_score),
                        "data_available": True
                    }
                },
                "total_score": total_score,
                "signal_generated": self.should_entry(result),
                "signal_threshold": self.entry_threshold,
                "veto": veto,
                "veto_reason": veto_reason,
                "missing_data": [],
                "additional_details": additional_details or {}
            }
            
            # 保存评分日志
            scoring_logger.log_scoring_report(
                symbol=symbol,
                scoring_data=scoring_data,
                scoring_attempt=scoring_attempt
            )
            
        except Exception as e:
            logger.error(f"❌ 生成报告失败：{e}")
        
        logger.info(
            f"📊 {symbol} 评分报告："
            f"综合={result.total_score:.2f}, "
            f"否决={result.veto}, "
            f"原因={result.veto_reason or '无'}"
        )
        
        return result
    
    def should_entry(self, result: ScoringResult) -> bool:
        """
        判断是否应该开仓
        
        Args:
            result: 评分结果
            
        Returns:
            是否开仓
        """
        # 被否决 → 不开仓
        if result.veto:
            return False
        
        # 综合评分 >= 阈值 → 开仓
        if result.total_score >= self.entry_threshold:
            logger.info(
                f"✅ {result.symbol} 达到开仓条件："
                f"评分={result.total_score:.2f} >= {self.entry_threshold}"
            )
            return True
        
        logger.debug(
            f"ℹ️ {result.symbol} 未达到开仓条件："
            f"评分={result.total_score:.2f} < {self.entry_threshold}"
        )
        return False
    
    def get_recommendation(self, result: ScoringResult) -> str:
        """
        获取操作建议
        
        Args:
            result: 评分结果
            
        Returns:
            操作建议字符串
        """
        if result.veto:
            return "❌ 否决 - 不建议操作"
        
        if result.total_score >= 9.0:
            return "⭐⭐⭐⭐⭐ 强烈推荐 - 满分 10 分"
        elif result.total_score >= 8.0:
            return "⭐⭐⭐⭐ 推荐 - 高确定性机会"
        elif result.total_score >= 7.0:
            return "⭐⭐⭐ 建议关注 - 中等确定性"
        elif result.total_score >= 6.0:
            return "⭐⭐ 观望 - 等待更好时机"
        else:
            return "⭐ 不建议操作 - 评分较低"
    
    def _get_contract_reason(self, score: float) -> str:
        """获取合约数据评分原因"""
        if score >= 8.0:
            return "OI/市值比极高，严重泡沫"
        elif score >= 6.0:
            return "OI/市值比高，泡沫明显"
        elif score >= 4.0:
            return "OI/市值比中等"
        elif score >= 2.0:
            return "OI/市值比低"
        else:
            return "数据不足，使用默认评分"
    
    def _get_fundamental_reason(self, score: float) -> str:
        """获取基本面评分原因"""
        if score >= 8.0:
            return "存在大额解锁"
        elif score >= 6.0:
            return "解锁比例较高"
        elif score >= 4.0:
            return "解锁比例中等"
        elif score >= 2.0:
            return "解锁比例较低"
        else:
            return "无解锁数据"
    
    def _get_technical_reason(self, score: float) -> str:
        """获取技术面评分原因"""
        if score >= 8.0:
            return "下跌趋势明显，技术指标看跌"
        elif score >= 6.0:
            return "偏弱趋势，技术面不利"
        elif score >= 4.0:
            return "震荡趋势，技术面中性"
        elif score >= 2.0:
            return "上涨趋势，技术面不利做空"
        else:
            return "K 线数据不足"
    
    def _get_sentiment_reason(self, score: float) -> str:
        """获取情绪面评分原因"""
        if score >= 8.0:
            return "资金费率极高，市场情绪狂热"
        elif score >= 6.0:
            return "资金费率较高，情绪偏多"
        elif score >= 4.0:
            return "资金费率中等"
        elif score >= 2.0:
            return "资金费率较低"
        else:
            return "默认评分"


# 全局评分引擎实例
scoring_engine = ScoringEngine()
