"""
数据计算工具

负责计算：
- OI/市值比
- 其他衍生指标
"""

from typing import Optional, Tuple
from utils.logger import logger


def calculate_oi_ratio(
    open_interest: float,
    circulating_market_cap: float
) -> Tuple[float, bool]:
    """
    计算 OI/市值比
    
    Args:
        open_interest: 持仓量 (USDT)
        circulating_market_cap: 流通市值 (USDT)
        
    Returns:
        (oi_ratio, is_valid) 元组:
        - oi_ratio: OI/市值比
        - is_valid: 是否有效 (无异常)
        
    异常处理:
        - 市值为 0 或负数 → 返回 (0, False)
        - 持仓量为负数 → 返回 (0, False)
    """
    # 数据验证
    if circulating_market_cap <= 0:
        logger.error(f"❌ 流通市值无效：{circulating_market_cap}")
        return 0.0, False
    
    if open_interest < 0:
        logger.error(f"❌ 持仓量无效：{open_interest}")
        return 0.0, False
    
    # 计算比值
    oi_ratio = open_interest / circulating_market_cap
    
    logger.debug(
        f"📊 OI/市值比计算：持仓量={open_interest:,.2f}, "
        f"市值={circulating_market_cap:,.2f}, 比值={oi_ratio:.4f}"
    )
    
    return oi_ratio, True


def score_oi_ratio(oi_ratio: float) -> Tuple[float, bool]:
    """
    根据 OI/市值比评分
    
    Args:
        oi_ratio: OI/市值比
        
    Returns:
        (score, veto) 元组:
        - score: 评分 (0-10)
        - veto: 是否一票否决
        
    评分规则:
        - > 1.0: 0 分，一票否决
        - 0.8 - 1.0: 0 分
        - 0.5 - 0.8: 3 分
        - 0.3 - 0.5: 7 分
        - < 0.3: 10 分
    """
    # 一票否决
    if oi_ratio > 1.0:
        logger.warning(f"⚠️ OI/市值比 > 1.0 ({oi_ratio:.4f}), 一票否决!")
        return 0.0, True
    
    # 评分
    if oi_ratio < 0.3:
        score = 10.0
    elif oi_ratio < 0.5:
        score = 7.0
    elif oi_ratio < 0.8:
        score = 3.0
    else:
        score = 0.0
    
    logger.debug(f"📊 OI/市值比评分：{oi_ratio:.4f} → {score:.1f}分")
    return score, False


def calculate_annualized_funding_rate(
    funding_rate: float
) -> float:
    """
    计算年化资金费率
    
    Args:
        funding_rate: 8 小时资金费率 (小数形式)
        
    Returns:
        年化资金费率 (小数形式)
        
    计算公式:
        年化费率 = 8 小时费率 × 3 (次/天) × 365 (天)
    """
    annual_rate = funding_rate * 3 * 365
    logger.debug(
        f"📈 年化资金费率计算：8 小时={funding_rate:.4%} → "
        f"年化={annual_rate:.2%}"
    )
    return annual_rate


def score_funding_rate(annual_rate: float) -> float:
    """
    根据年化资金费率评分
    
    Args:
        annual_rate: 年化资金费率 (小数形式)
        
    Returns:
        评分 (0-10)
        
    评分规则:
        - > 100%: 10 分
        - 50% - 100%: 7 分
        - 10% - 50%: 3 分
        - < 10%: 0 分
    """
    if annual_rate > 1.0:  # 100%
        score = 10.0
    elif annual_rate > 0.5:  # 50%
        score = 7.0
    elif annual_rate > 0.1:  # 10%
        score = 3.0
    else:
        score = 0.0
    
    logger.debug(f"📊 资金费率评分：{annual_rate:.2%} → {score:.1f}分")
    return score
