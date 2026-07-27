"""
Token 用量统计
追踪每次 LLM API 调用的 Token 消耗，按月和按策略汇总

成本估算基于 DeepSeek 官方定价（deepseek-v4-pro）：
- 输入（缓存未命中）：$1.74/百万Token
- 输入（缓存命中）：$0.174/百万Token（自动缓存，节省 90%）
- 输出：$3.48/百万Token
（实际定价从 config.yaml 的 deepseek.pricing 配置读取，支持热更新）
"""

from datetime import datetime
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


class CostTracker:
    """
    Token 用量跟踪器

    记录每次 API 调用的 Token 消耗，支持按月和按策略汇总。
    提供成本估算功能。定价从配置文件读取。
    """

    def __init__(self, input_price_per_m: float, output_price_per_m: float, input_cache_hit_price: float = 0.0):
        """
        初始化用量跟踪器

        Args:
            input_price_per_m: 输入价格（美元/百万Token），从配置读取
            output_price_per_m: 输出价格（美元/百万Token），从配置读取
            input_cache_hit_price: 缓存命中输入价格（美元/百万Token），0 表示不使用缓存定价
        """
        self.input_price_per_m = input_price_per_m
        self.output_price_per_m = output_price_per_m
        self.input_cache_hit_price = input_cache_hit_price
        # 每次调用的详细记录
        self._records: List[Dict[str, Any]] = []
        # 按月汇总：{YYYY-MM: {prompt_tokens, completion_tokens, total_tokens, cost}}
        self._monthly: Dict[str, Dict[str, Any]] = {}
        # 按策略汇总：{strategy_id: {prompt_tokens, completion_tokens, total_tokens, cost}}
        self._strategy: Dict[str, Dict[str, Any]] = {}

    def record_usage(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        strategy_id: str = "",
        cache_hit: bool = False,
    ) -> None:
        """
        记录一次 API 调用的 Token 用量

        Args:
            model: 模型名称
            prompt_tokens: 输入 Token 数
            completion_tokens: 输出 Token 数
            total_tokens: 总 Token 数
            strategy_id: 关联的策略ID
            cache_hit: 是否命中上下文缓存（用于缓存定价）
        """
        now = datetime.now()
        month_key = now.strftime("%Y-%m")

        cost = self._calc_cost(prompt_tokens, completion_tokens, cache_hit=cache_hit)

        record = {
            "timestamp": now.isoformat(),
            "model": model,
            "strategy_id": strategy_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(cost, 6),
        }
        self._records.append(record)

        # 更新月度汇总
        if month_key not in self._monthly:
            self._monthly[month_key] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
                "calls": 0,
            }
        monthly = self._monthly[month_key]
        monthly["prompt_tokens"] += prompt_tokens
        monthly["completion_tokens"] += completion_tokens
        monthly["total_tokens"] += total_tokens
        monthly["cost"] += cost
        monthly["calls"] += 1

        # 更新策略汇总
        if strategy_id:
            if strategy_id not in self._strategy:
                self._strategy[strategy_id] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0,
                    "calls": 0,
                }
            strategy = self._strategy[strategy_id]
            strategy["prompt_tokens"] += prompt_tokens
            strategy["completion_tokens"] += completion_tokens
            strategy["total_tokens"] += total_tokens
            strategy["cost"] += cost
            strategy["calls"] += 1

        logger.debug(
            "Token用量已记录",
            model=model,
            strategy_id=strategy_id,
            total_tokens=total_tokens,
            cost_usd=round(cost, 6),
        )

    def get_monthly_cost(self) -> Dict[str, Dict[str, Any]]:
        """
        获取月度成本汇总

        Returns:
            按月汇总的字典 {YYYY-MM: {prompt_tokens, completion_tokens, cost, calls}}
        """
        return self._monthly

    def get_strategy_cost(self, strategy_id: str) -> Dict[str, Any]:
        """
        获取指定策略的成本汇总

        Args:
            strategy_id: 策略唯一标识

        Returns:
            策略成本汇总字典
        """
        return self._strategy.get(strategy_id, {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "calls": 0,
        })

    def get_total_cost(self) -> float:
        """
        获取总成本

        Returns:
            总成本（美元）
        """
        strategy_cost = sum(s["cost"] for s in self._strategy.values())
        # 补充无 strategy_id 的记录成本
        for r in self._records:
            if not r.get("strategy_id"):
                strategy_cost += r.get("cost_usd", 0.0)
        return strategy_cost

    def get_summary(self) -> Dict[str, Any]:
        """
        获取成本汇总报告

        Returns:
            包含总成本、月度明细、策略明细的字典
        """
        # 统计 total_calls：包含所有记录（含无 strategy_id 的）
        all_calls = len(self._records)
        return {
            "total_cost_usd": round(self.get_total_cost(), 4),
            "total_calls": all_calls,
            "monthly": self._monthly,
            "by_strategy": self._strategy,
        }

    def _calc_cost(self, prompt_tokens: int, completion_tokens: int, cache_hit: bool = False) -> float:
        """
        计算单次调用成本

        如果开启了缓存命中定价且本次调用命中缓存，使用缓存命中价格。

        Args:
            prompt_tokens: 输入 Token 数
            completion_tokens: 输出 Token 数
            cache_hit: 是否命中上下文缓存（默认 False）

        Returns:
            成本（美元）
        """
        input_price = self.input_price_per_m
        if cache_hit and self.input_cache_hit_price > 0:
            input_price = self.input_cache_hit_price
        input_cost = (prompt_tokens / 1_000_000) * input_price
        output_cost = (completion_tokens / 1_000_000) * self.output_price_per_m
        return input_cost + output_cost