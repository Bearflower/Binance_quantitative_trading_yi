"""
合约数据评分模块

负责：
- 获取合约持仓量（OI）数据
- 获取市值数据（从 CoinGecko）
- 计算 OI/市值比率
- 计算合约数据评分（0-10 分）
"""

from typing import Dict, Any, Optional, Tuple
from utils.logger import logger
from core.binance_client import binance_client
from core.coingecko_client import coingecko_client


class ContractScorer:
    """合约数据评分器"""
    
    def __init__(self):
        """初始化评分器"""
        logger.info("✅ 合约数据评分器初始化完成")
    
    def get_market_cap(
        self,
        symbol: str
    ) -> Optional[float]:
        """
        获取市值数据
        
        Args:
            symbol: 币种符号（如 LOBSTERUSDT）
            
        Returns:
            市值（USD），失败返回 None
        """
        try:
            # 从 CoinGecko 获取市值
            market_cap = coingecko_client.get_market_cap_by_symbol(symbol)
            
            if market_cap:
                logger.debug(f"📊 {symbol} 市值：${market_cap:,.2f}")
                return market_cap
            else:
                logger.warning(f"⚠️ 无法获取 {symbol} 的市值数据")
                return None
                
        except Exception as e:
            logger.error(f"❌ 获取市值失败：{e}")
            return None
    
    def calculate_oi_ratio(
        self,
        symbol: str
    ) -> Tuple[Optional[float], bool]:
        """
        计算 OI/市值比率
        
        Args:
            symbol: 币种符号
            
        Returns:
            (oi_ratio, valid) 元组：
            - oi_ratio: OI/市值比率
            - valid: 是否有效（两者都获取成功）
        """
        try:
            # 获取 OI 数据
            logger.debug(f"📊 开始获取 {symbol} 的 OI 数据...")
            oi_usd = binance_client.get_current_open_interest(symbol)
            
            if not oi_usd:
                logger.warning(
                    f"⚠️ 无法获取 {symbol} 的 OI 数据，"
                    f"可能原因：1) 网络问题 2) API 限流 3) 该币种数据不存在（新币常见）"
                )
                return None, False
            
            # 获取市值数据
            logger.debug(f"📊 开始获取 {symbol} 的市值数据...")
            market_cap = self.get_market_cap(symbol)
            
            if not market_cap:
                logger.warning(
                    f"⚠️ 无法获取 {symbol} 的市值数据，"
                    f"可能原因：1) CoinGecko 无该币种数据 2) 网络问题"
                )
                return None, False
            
            # 计算比率
            oi_ratio = oi_usd / market_cap
            
            logger.info(
                f"📊 {symbol} OI/市值比率：{oi_ratio:.4f} "
                f"(OI=${oi_usd:,.2f}, 市值=${market_cap:,.2f})"
            )
            
            return oi_ratio, True
            
        except Exception as e:
            logger.error(
                f"❌ 计算 {symbol} OI/市值比率失败："
                f"OI=${oi_usd if 'oi_usd' in locals() else 'N/A'}, "
                f"市值=${market_cap if 'market_cap' in locals() else 'N/A'}, "
                f"错误：{e}"
            )
            return None, False
    
    def score_oi_ratio(
        self,
        oi_ratio: float
    ) -> Tuple[float, str]:
        """
        根据 OI/市值比率评分
        
        Args:
            oi_ratio: OI/市值比率
            
        Returns:
            (score, reason) 元组：
            - score: 评分（0-10 分）
            - reason: 评分原因
            
        评分规则（针对做空）：
            - oi_ratio > 1.0: 10 分（严重泡沫，强烈做空信号）
            - 0.5-1.0: 8 分（高泡沫）
            - 0.3-0.5: 6 分（中等泡沫）
            - 0.1-0.3: 4 分（低泡沫）
            - <0.1: 2 分（泡沫很小）
        """
        if oi_ratio > 1.0:
            score = 10.0
            reason = f"OI/市值比极高 ({oi_ratio:.4f})，严重泡沫"
        elif oi_ratio > 0.5:
            score = 8.0
            reason = f"OI/市值比高 ({oi_ratio:.4f})，泡沫明显"
        elif oi_ratio > 0.3:
            score = 6.0
            reason = f"OI/市值比中等 ({oi_ratio:.4f})"
        elif oi_ratio > 0.1:
            score = 4.0
            reason = f"OI/市值比低 ({oi_ratio:.4f})"
        else:
            score = 2.0
            reason = f"OI/市值比极低 ({oi_ratio:.4f})，泡沫很小"
        
        logger.debug(f"📊 OI/市值比评分：{score}/10.0 ({reason})")
        
        return score, reason
    
    def calculate_contract_score(
        self,
        symbol: str
    ) -> Tuple[float, Optional[str]]:
        """
        计算合约数据评分
        
        Args:
            symbol: 币种符号
            
        Returns:
            (score, reason) 元组：
            - score: 评分（0-10 分）
            - reason: 评分原因（失败时为 None）
        """
        try:
            # 计算 OI/市值比率
            oi_ratio, valid = self.calculate_oi_ratio(symbol)
            
            if not valid or oi_ratio is None:
                logger.warning(f"⚠️ {symbol} OI/市值比率计算失败，使用默认评分 5.0")
                return 5.0, "数据不足，使用默认评分"
            
            # 根据 OI/市值比率评分
            score, reason = self.score_oi_ratio(oi_ratio)
            
            logger.info(f"📊 {symbol} 合约数据评分：{score}/10.0 ({reason})")
            
            return score, reason
            
        except Exception as e:
            logger.error(f"❌ 计算合约数据评分失败：{e}")
            return 5.0, f"计算失败：{e}"


# 全局评分器实例
contract_scorer = ContractScorer()
