#!/usr/bin/env python3
"""
DeepSeek AI 第二意见模块

提供 AI 分析作为第二意见参考（可选功能）：
1. 对比系统决策和 AI 建议
2. 置信度评估
3. 分歧处理
4. 优化提示词工程

使用方式：
- 系统自动生成交易信号
- 可选调用 DeepSeek 获取第二意见
- 对比两者决策
- 仅在高度一致时执行交易
"""

import logging
import json
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class DeepSeekAdvisor:
    """DeepSeek AI 顾问类"""
    
    def __init__(self, params: StrategyParams = None, api_key: str = None):
        """
        初始化 AI 顾问
        
        Args:
            params: 策略参数
            api_key: DeepSeek API 密钥
        """
        self.params = params or get_params()
        self.api_key = api_key or self.params.get('ai.deepseek_api_key', '')
        
        # AI 分析配置
        self.enable_ai = self.params.get('ai.enable_deepseek', False)
        self.confidence_threshold = self.params.get('ai.confidence_threshold', Decimal('0.7'))
        
        # 提示词模板
        self.prompt_templates = self._load_prompt_templates()
    
    def get_second_opinion(
        self,
        signal: Dict[str, Any],
        market_data: Dict[str, Any],
        account_status: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        获取 AI 第二意见
        
        Args:
            signal: 系统生成的交易信号
            market_data: 市场数据
            account_status: 账户状态
        
        Returns:
            AI 分析结果
        """
        if not self.enable_ai:
            return {
                'enabled': False,
                'reason': 'AI 分析未启用',
                'recommendation': None
            }
        
        try:
            # 构建提示词
            prompt = self._build_analysis_prompt(signal, market_data, account_status)
            
            # 调用 DeepSeek API（简化实现，实际需要调用 API）
            ai_response = self._call_deepseek_api(prompt)
            
            # 解析 AI 响应
            analysis = self._parse_ai_response(ai_response)
            
            # 对比系统决策
            comparison = self._compare_with_system(signal, analysis)
            
            logger.info(f"AI 第二意见获取成功：{signal['币种']}")
            logger.info(f"  AI 建议：{analysis.get('recommendation', 'UNKNOWN')}")
            logger.info(f"  置信度：{analysis.get('confidence', Decimal('0')):.0%}")
            logger.info(f"  一致性：{comparison.get('agreement', 'UNKNOWN')}")
            
            return {
                'enabled': True,
                'timestamp': datetime.now().isoformat(),
                'ai_analysis': analysis,
                'comparison': comparison,
                'recommendation': self._get_final_recommendation(signal, analysis, comparison)
            }
            
        except Exception as e:
            logger.error(f"AI 分析失败：{str(e)}", exc_info=True)
            return {
                'enabled': True,
                'error': str(e),
                'recommendation': None
            }
    
    def compare_decisions(
        self,
        system_signal: Dict[str, Any],
        ai_analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        对比系统决策和 AI 建议
        
        Args:
            system_signal: 系统信号
            ai_analysis: AI 分析
        
        Returns:
            对比结果
        """
        comparison = {
            'direction_match': False,
            'grade_comparison': None,
            'confidence_gap': Decimal('0'),
            'agreement': 'UNKNOWN',
            'action': 'HOLD'
        }
        
        # 检查方向是否一致
        ai_direction = ai_analysis.get('direction')
        system_direction = 'LONG' if system_signal.get('开仓方向') == '多' else 'SHORT'
        
        if ai_direction == system_direction:
            comparison['direction_match'] = True
        
        # 比较置信度
        ai_confidence = ai_analysis.get('confidence', Decimal('0'))
        system_confidence = Decimal(str(system_signal.get('开仓推荐度', 50))) / 100
        
        comparison['confidence_gap'] = abs(ai_confidence - system_confidence)
        
        # 判断一致性
        if comparison['direction_match'] and ai_confidence >= self.confidence_threshold:
            comparison['agreement'] = 'STRONG'
            comparison['action'] = 'EXECUTE'
        elif comparison['direction_match']:
            comparison['agreement'] = 'WEAK'
            comparison['action'] = 'CONSIDER'
        else:
            comparison['agreement'] = 'DISAGREE'
            comparison['action'] = 'REVIEW'
        
        return comparison
    
    def _build_analysis_prompt(
        self,
        signal: Dict[str, Any],
        market_data: Dict[str, Any],
        account_status: Dict[str, Any]
    ) -> str:
        """
        构建分析提示词
        
        Args:
            signal: 系统信号
            market_data: 市场数据
            account_status: 账户状态
        
        Returns:
            提示词字符串
        """
        template = self.prompt_templates.get('second_opinion', '')
        
        # 填充模板
        prompt = template.format(
            symbol=signal.get('币种', 'UNKNOWN'),
            direction=signal.get('开仓方向', 'UNKNOWN'),
            grade=signal.get('信号等级', 'UNKNOWN'),
            entry_price=signal.get('开仓价', '0'),
            stop_loss=signal.get('止损价', '0'),
            recommendation_score=signal.get('开仓推荐度', '0'),
            market_context=json.dumps(market_data, indent=2),
            account_status=json.dumps(account_status, indent=2)
        )
        
        logger.debug(f"AI 提示词长度：{len(prompt)}")
        return prompt
    
    def _call_deepseek_api(self, prompt: str) -> str:
        """
        调用 DeepSeek API
        
        Args:
            prompt: 提示词
        
        Returns:
            API 响应文本
        """
        # TODO: 实际实现需要调用 DeepSeek API
        # 这里返回模拟响应
        
        logger.info("调用 DeepSeek API...")
        
        # 模拟响应
        mock_response = {
            'recommendation': 'BUY',
            'confidence': 0.75,
            'reasoning': '技术面显示多头趋势，EMA21 向上，RSI 未超买',
            'suggestions': [
                '建议设置严格止损',
                '关注 95000 阻力位',
                '分批止盈策略合理'
            ]
        }
        
        return json.dumps(mock_response)
    
    def _parse_ai_response(self, response: str) -> Dict[str, Any]:
        """
        解析 AI 响应
        
        Args:
            response: API 响应
        
        Returns:
            解析后的分析结果
        """
        try:
            data = json.loads(response)
            
            analysis = {
                'direction': data.get('recommendation', 'HOLD'),
                'confidence': Decimal(str(data.get('confidence', 0))),
                'reasoning': data.get('reasoning', ''),
                'suggestions': data.get('suggestions', []),
                'timestamp': datetime.now().isoformat()
            }
            
            return analysis
            
        except json.JSONDecodeError as e:
            logger.error(f"解析 AI 响应失败：{str(e)}")
            return {
                'direction': 'HOLD',
                'confidence': Decimal('0'),
                'error': str(e)
            }
    
    def _get_final_recommendation(
        self,
        signal: Dict[str, Any],
        ai_analysis: Dict[str, Any],
        comparison: Dict[str, Any]
    ) -> str:
        """
        获取最终建议
        
        Args:
            signal: 系统信号
            ai_analysis: AI 分析
            comparison: 对比结果
        
        Returns:
            最终建议（EXECUTE/REVIEW/SKIP）
        """
        if comparison.get('agreement') == 'STRONG':
            return 'EXECUTE'
        elif comparison.get('agreement') == 'DISAGREE':
            return 'SKIP'
        else:
            return 'REVIEW'
    
    def _load_prompt_templates(self) -> Dict[str, str]:
        """加载提示词模板"""
        return {
            'second_opinion': """
你是一个专业的加密货币交易分析师。请分析以下交易信号并提供第二意见：

【系统信号】
- 币种：{symbol}
- 方向：{direction}
- 信号等级：{grade}
- 开仓价：{entry_price}
- 止损价：{stop_loss}
- 推荐度：{recommendation_score}/100

【市场环境】
{market_context}

【账户状态】
{account_status}

请分析：
1. 你是否同意这个交易信号？为什么？
2. 你的置信度是多少？（0-1，1 为最高）
3. 有什么风险需要注意？
4. 对仓位管理有什么建议？

请以 JSON 格式回复：
{{
    "recommendation": "BUY/SELL/HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "分析理由",
    "suggestions": ["建议 1", "建议 2"]
}}
""",
            'market_analysis': """
请分析当前市场环境：

【市场数据】
{market_data}

【技术指标】
{technical_indicators}

请提供：
1. 市场趋势判断
2. 关键支撑/阻力位
3. 交易建议

请以 JSON 格式回复。
"""
        }
    
    def generate_ai_report(
        self,
        signals: List[Dict[str, Any]],
        market_data: Dict[str, Any],
        account_status: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        生成 AI 分析报告
        
        Args:
            signals: 系统信号列表
            market_data: 市场数据
            account_status: 账户状态
        
        Returns:
            AI 分析报告
        """
        ai_opinions = []
        
        for signal in signals:
            opinion = self.get_second_opinion(signal, market_data, account_status)
            ai_opinions.append(opinion)
        
        # 统计一致性
        total = len(ai_opinions)
        strong_agree = sum(1 for o in ai_opinions if o.get('comparison', {}).get('agreement') == 'STRONG')
        disagree = sum(1 for o in ai_opinions if o.get('comparison', {}).get('agreement') == 'DISAGREE')
        
        report = {
            'timestamp': datetime.now().isoformat(),
            'total_signals': total,
            'ai_opinions': ai_opinions,
            'statistics': {
                'strong_agreement': strong_agree,
                'disagreement': disagree,
                'agreement_rate': Decimal(strong_agree) / Decimal(total) if total > 0 else Decimal('0')
            },
            'overall_recommendation': 'EXECUTE' if strong_agree > disagree else 'REVIEW'
        }
        
        return report


# 全局实例
_global_advisor: Optional[DeepSeekAdvisor] = None


def get_deepseek_advisor(params: StrategyParams = None, api_key: str = None) -> DeepSeekAdvisor:
    """获取 DeepSeek 顾问实例（单例模式）"""
    global _global_advisor
    if _global_advisor is None:
        _global_advisor = DeepSeekAdvisor(params=params, api_key=api_key)
    return _global_advisor


# 便捷函数
def get_second_opinion(
    signal: Dict[str, Any],
    market_data: Dict[str, Any],
    account_status: Dict[str, Any]
) -> Dict[str, Any]:
    """获取 AI 第二意见的便捷函数"""
    return get_deepseek_advisor().get_second_opinion(signal, market_data, account_status)


def compare_decisions(
    system_signal: Dict[str, Any],
    ai_analysis: Dict[str, Any]
) -> Dict[str, Any]:
    """对比决策的便捷函数"""
    return get_deepseek_advisor().compare_decisions(system_signal, ai_analysis)
