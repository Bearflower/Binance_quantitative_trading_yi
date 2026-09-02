"""
市场状态分段器

将K线序列按价格走势分为上涨/下跌/横盘段，
用于回测引擎按市况分别统计收益表现。

分段算法：
1. 扫描K线序列，计算累计价格变化率
2. 当累计变化率超过阈值时，切分为一段
3. 合并长度不足的短段到相邻段
4. 重新判定每段的 regime
"""

from dataclasses import dataclass
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


@dataclass
class MarketSegment:
    """市场状态分段"""
    regime: str = "横盘"                    # 市况："上涨" / "下跌" / "横盘"
    start_index: int = 0                    # 起始K线序号
    end_index: int = 0                      # 结束K线序号
    price_change_pct: float = 0.0           # 价格变化率


class MarketSegmenter:
    """市场状态分段器

    将K线序列按价格走势分为上涨/下跌/横盘段。
    纯Python实现，不依赖外部库。
    """

    def __init__(self, trend_threshold: float = 0.02, min_segment_length: int = 8):
        """
        初始化分段器

        Args:
            trend_threshold: 趋势判定阈值（如 0.02 表示 2%）
            min_segment_length: 每段最少K线数（不足则合并到相邻段）
        """
        self._trend_threshold = trend_threshold
        self._min_segment_length = min_segment_length

    def segment(self, klines: List[Dict[str, Any]]) -> List[MarketSegment]:
        """
        将K线序列分段

        算法：
        1. 计算每根K线的价格变化率
        2. 累计变化率，到达阈值时切分
        3. 合并短段
        4. 重新判定 regime

        Args:
            klines: K线数据列表，每项包含 close 字段

        Returns:
            MarketSegment 列表，覆盖所有K线，无重叠无遗漏
        """
        if not klines or len(klines) < self._min_segment_length:
            # 数据不足，全部归为横盘
            if klines:
                return [MarketSegment(
                    regime="横盘",
                    start_index=0,
                    end_index=len(klines) - 1,
                    price_change_pct=0.0,
                )]
            return []

        # 1. 计算价格变化率序列
        price_changes = self._calc_price_changes(klines)

        # 2. 初始分段
        segments = self._initial_segmentation(klines, price_changes)

        # 3. 合并短段
        segments = self._merge_short_segments(segments)

        # 4. 重新判定 regime
        for seg in segments:
            seg.regime = self._classify_regime(seg.price_change_pct)
            seg.price_change_pct = self._recalc_price_change(klines, seg.start_index, seg.end_index)

        return segments

    # ============================================================
    # 私有方法
    # ============================================================

    def _calc_price_changes(self, klines: List[Dict[str, Any]]) -> List[float]:
        """
        计算每根K线的价格变化率

        Args:
            klines: K线数据列表

        Returns:
            价格变化率列表，长度 = len(klines) - 1
        """
        changes = []
        for i in range(1, len(klines)):
            prev_close = float(klines[i - 1]["close"])
            curr_close = float(klines[i]["close"])
            if prev_close > 0:
                change = (curr_close - prev_close) / prev_close
            else:
                change = 0.0
            changes.append(change)
        return changes

    def _initial_segmentation(
        self,
        klines: List[Dict[str, Any]],
        price_changes: List[float],
    ) -> List[MarketSegment]:
        """
        基于累计价格变化率进行初始分段

        Args:
            klines: K线数据列表
            price_changes: 价格变化率列表

        Returns:
            初始分段列表
        """
        segments = []
        current_start = 0
        cumulative_change = 0.0

        for i, change in enumerate(price_changes):
            cumulative_change += change

            # 检查是否到达转折点
            if abs(cumulative_change) >= self._trend_threshold:
                segment = MarketSegment(
                    regime="",  # 后续重新判定
                    start_index=current_start,
                    end_index=i + 1,  # 包含当前K线
                    price_change_pct=cumulative_change,
                )
                segments.append(segment)
                current_start = i + 2  # 下一段从下一根K线开始
                cumulative_change = 0.0

        # 处理剩余部分
        if current_start < len(klines):
            # 重新计算剩余部分的价格变化率
            if current_start < len(klines) - 1 and klines[current_start].get("close", 0) > 0:
                first_close = float(klines[current_start]["close"])
                last_close = float(klines[-1]["close"])
                remaining_change = (last_close - first_close) / first_close if first_close > 0 else 0.0
            else:
                remaining_change = cumulative_change

            segment = MarketSegment(
                regime="",
                start_index=current_start,
                end_index=len(klines) - 1,
                price_change_pct=remaining_change,
            )
            segments.append(segment)

        return segments

    def _merge_short_segments(
        self, segments: List[MarketSegment]
    ) -> List[MarketSegment]:
        """
        合并长度不足的短段到相邻段

        Args:
            segments: 分段列表

        Returns:
            合并后的分段列表
        """
        if not segments:
            return segments

        # 反复合并直到所有段长度 >= min_segment_length
        changed = True
        max_iterations = len(segments)  # 防止无限循环
        iteration = 0

        while changed and iteration < max_iterations:
            changed = False
            iteration += 1
            merged = []

            for i, seg in enumerate(segments):
                seg_length = seg.end_index - seg.start_index + 1

                if seg_length >= self._min_segment_length:
                    merged.append(seg)
                    continue

                # 短段：尝试合并
                if merged:
                    # 合并到前一个段
                    prev = merged[-1]
                    merged[-1] = MarketSegment(
                        regime="",
                        start_index=prev.start_index,
                        end_index=seg.end_index,
                        price_change_pct=prev.price_change_pct + seg.price_change_pct,
                    )
                    changed = True
                elif i + 1 < len(segments):
                    # 合并到后一个段
                    next_seg = segments[i + 1]
                    segments[i + 1] = MarketSegment(
                        regime="",
                        start_index=seg.start_index,
                        end_index=next_seg.end_index,
                        price_change_pct=seg.price_change_pct + next_seg.price_change_pct,
                    )
                    changed = True
                    # 跳过下一个段，因为已经被合并
                    merged.append(None)  # 占位符
                else:
                    # 没有可以合并的段，保留原样
                    merged.append(seg)

            # 过滤掉 None 占位符
            segments = [s for s in merged if s is not None]

        return segments

    def _classify_regime(self, price_change_pct: float) -> str:
        """
        根据价格变化率判定市况

        Args:
            price_change_pct: 价格变化率

        Returns:
            "上涨" / "下跌" / "横盘"
        """
        if abs(price_change_pct) >= self._trend_threshold:
            return "上涨" if price_change_pct > 0 else "下跌"
        return "横盘"

    @staticmethod
    def _recalc_price_change(
        klines: List[Dict[str, Any]], start_index: int, end_index: int
    ) -> float:
        """
        重新计算指定区间的价格变化率

        Args:
            klines: K线数据列表
            start_index: 起始序号
            end_index: 结束序号

        Returns:
            价格变化率
        """
        if start_index >= len(klines) or end_index >= len(klines):
            return 0.0

        first_close = float(klines[start_index]["close"])
        last_close = float(klines[end_index]["close"])
        if first_close > 0:
            return (last_close - first_close) / first_close
        return 0.0