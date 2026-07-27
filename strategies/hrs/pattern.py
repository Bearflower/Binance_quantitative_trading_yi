"""
形态识别模块
检测做空形态（三次冲顶、长上影线、放量滞涨）和做多形态（三次探底、长下影线、放量止跌）
"""
from typing import Dict, List, Tuple, Any
import structlog


logger = structlog.get_logger()


class PatternRecognizer:
    """
    形态识别器

    识别 HRS 策略的关键形态：
    - 做空：三次冲顶、长上影线、放量滞涨
    - 做多：三次探底、长下影线、放量止跌

    形态检测窗口固定为5根K线（根据 V1.6 规范）。
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化形态识别器

        Args:
            config: 配置字典
        """
        self.config = config
        pattern_config = config.get("pattern", {})

        # 做空形态配置
        short_config = pattern_config.get("short", {})
        self.short_three_tops = short_config.get("three_tops", {})
        self.short_long_shadow = short_config.get("long_upper_shadow", {})
        self.short_volume_stag = short_config.get("volume_stagnation", {})
        self.short_double_top = short_config.get("double_top", {})
        self.short_v_reversal = short_config.get("v_reversal", {})

        # 做多形态配置
        long_config = pattern_config.get("long", {})
        self.long_three_bottoms = long_config.get("three_bottoms", {})
        self.long_long_shadow = long_config.get("long_lower_shadow", {})
        self.long_volume_rev = long_config.get("volume_reversal", {})
        self.long_double_bottom = long_config.get("double_bottom", {})
        self.long_v_reversal = long_config.get("v_reversal", {})

        self.window_size = pattern_config.get("window_size", 5)

        logger.info(
            "形态识别器初始化完成",
            window_size=self.window_size
        )

    def detect_short_patterns(self, klines: List[Dict]) -> Dict[str, Any]:
        """
        检测做空形态

        Args:
            klines: 1h K线数据列表（至少5根）

        Returns:
            形态检测结果字典
        """
        result = {
            "three_tops": (False, 0.0),
            "long_upper_shadow": (False, 0.0),
            "volume_stagnation": (False, 0.0),
            "double_top": (False, 0.0),       # 思路2：替代形态
            "v_reversal_short": (False, 0.0),  # 思路2：V型反转
        }

        if not klines or len(klines) < self.window_size:
            return result

        # 三次冲顶检测
        result["three_tops"] = self._detect_three_tops(klines)

        # 长上影线检测
        result["long_upper_shadow"] = self._detect_long_upper_shadow(klines)

        # 放量滞涨检测
        result["volume_stagnation"] = self._detect_volume_stagnation(klines)

        # 思路2：替代形态检测（三次冲顶未触发时才检测）
        if not result["three_tops"][0]:
            result["double_top"] = self._detect_double_top(klines)
            result["v_reversal_short"] = self._detect_v_reversal_short(klines)

        logger.info(
            "做空形态检测完成",
            three_tops=result["three_tops"][0],
            three_tops_score=result["three_tops"][1],
            double_top=result["double_top"][0],
            v_reversal_short=result["v_reversal_short"][0],
            long_upper_shadow=result["long_upper_shadow"][0],
            long_upper_shadow_score=result["long_upper_shadow"][1],
            volume_stagnation=result["volume_stagnation"][0],
            volume_stagnation_score=result["volume_stagnation"][1],
        )

        return result

    def detect_long_patterns(self, klines: List[Dict]) -> Dict[str, Any]:
        """
        检测做多形态

        Args:
            klines: 1h K线数据列表（至少5根）

        Returns:
            形态检测结果字典
        """
        result = {
            "three_bottoms": (False, 0.0),
            "long_lower_shadow": (False, 0.0),
            "volume_reversal": (False, 0.0),
            "double_bottom": (False, 0.0),      # 思路2：替代形态
            "v_reversal_long": (False, 0.0),     # 思路2：V型反转
        }

        if not klines or len(klines) < self.window_size:
            return result

        # 三次探底检测
        result["three_bottoms"] = self._detect_three_bottoms(klines)

        # 长下影线检测
        result["long_lower_shadow"] = self._detect_long_lower_shadow(klines)

        # 放量止跌检测
        result["volume_reversal"] = self._detect_volume_reversal(klines)

        # 思路2：替代形态检测（三次探底未触发时才检测）
        if not result["three_bottoms"][0]:
            result["double_bottom"] = self._detect_double_bottom(klines)
            result["v_reversal_long"] = self._detect_v_reversal_long(klines)

        logger.info(
            "做多形态检测完成",
            three_bottoms=result["three_bottoms"][0],
            three_bottoms_score=result["three_bottoms"][1],
            double_bottom=result["double_bottom"][0],
            v_reversal_long=result["v_reversal_long"][0],
            long_lower_shadow=result["long_lower_shadow"][0],
            long_lower_shadow_score=result["long_lower_shadow"][1],
            volume_reversal=result["volume_reversal"][0],
            volume_reversal_score=result["volume_reversal"][1],
        )

        return result

    def _detect_three_tops(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测三次冲顶形态

        最近5根K线的高点依次降低（每相邻高点差 ≥ 0.2%），
        或同一水平受阻 ≥ 3次。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        highs = [float(k.get("high", 0)) for k in window]

        min_deviation = self.short_three_tops.get("min_deviation", 0.002)
        score_full = self.short_three_tops.get("score_full", 4.0)
        score_partial = self.short_three_tops.get("score_partial", 2.0)

        # 检查是否依次降低
        decreasing = True
        for i in range(1, len(highs)):
            if highs[i] > highs[i - 1]:
                decreasing = False
                break
            # 检查相邻高点差 ≥ 0.2%
            if abs(highs[i] - highs[i - 1]) / highs[i - 1] < min_deviation:
                decreasing = False
                break

        if decreasing:
            return True, score_full

        # 检查是否同一水平受阻（次数阈值从配置读取）
        avg_high = sum(highs) / len(highs)
        resistance_count = sum(
            1 for h in highs
            if abs(h - avg_high) / avg_high < min_deviation
        )
        resistance_threshold = self.short_three_tops.get("resistance_count", 3)
        if resistance_count >= resistance_threshold:
            return True, score_partial

        return False, 0.0

    def _detect_three_bottoms(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测三次探底形态

        最近5根K线的低点依次抬高（每相邻低点差 ≥ 0.2%），
        或同一支撑位被触及 ≥ 3次。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        lows = [float(k.get("low", 0)) for k in window]

        min_deviation = self.long_three_bottoms.get("min_deviation", 0.002)
        score_full = self.long_three_bottoms.get("score_full", 4.0)
        score_partial = self.long_three_bottoms.get("score_partial", 2.0)

        # 检查是否依次抬高
        increasing = True
        for i in range(1, len(lows)):
            if lows[i] < lows[i - 1]:
                increasing = False
                break
            if abs(lows[i] - lows[i - 1]) / lows[i - 1] < min_deviation:
                increasing = False
                break

        if increasing:
            return True, score_full

        # 检查是否同一支撑位被触及（次数阈值从配置读取）
        avg_low = sum(lows) / len(lows)
        support_count = sum(
            1 for l in lows
            if abs(l - avg_low) / avg_low < min_deviation
        )
        support_threshold = self.long_three_bottoms.get("support_count", 3)
        if support_count >= support_threshold:
            return True, score_partial

        return False, 0.0

    def _detect_long_upper_shadow(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测长上影线

        当前K线上影线长度 ≥ 实体长度 × 2，且实体为阴线或十字星。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        current = klines[-1]
        open_price = float(current.get("open", 0))
        close_price = float(current.get("close", 0))
        high_price = float(current.get("high", 0))

        body = abs(close_price - open_price)
        upper_shadow = high_price - max(open_price, close_price)

        ratio_threshold = self.short_long_shadow.get("ratio_threshold", 2.0)
        score_full = self.short_long_shadow.get("score_full", 3.0)
        score_partial = self.short_long_shadow.get("score_partial", 2.0)

        if body == 0:
            # 十字星：上影线 > 0 即满足
            if upper_shadow > 0 and high_price > open_price:
                return True, score_partial
            return False, 0.0

        ratio = upper_shadow / body
        if ratio >= ratio_threshold:
            # 实体为阴线（收盘价 ≤ 开盘价）
            if close_price <= open_price:
                return True, score_full
            return True, score_partial

        return False, 0.0

    def _detect_long_lower_shadow(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测长下影线

        当前K线下影线长度 ≥ 实体长度 × 2，且实体为阳线或十字星。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        current = klines[-1]
        open_price = float(current.get("open", 0))
        close_price = float(current.get("close", 0))
        low_price = float(current.get("low", 0))

        body = abs(close_price - open_price)
        lower_shadow = min(open_price, close_price) - low_price

        ratio_threshold = self.long_long_shadow.get("ratio_threshold", 2.0)
        score_full = self.long_long_shadow.get("score_full", 3.0)
        score_partial = self.long_long_shadow.get("score_partial", 2.0)

        if body == 0:
            if lower_shadow > 0 and low_price < open_price:
                return True, score_partial
            return False, 0.0

        ratio = lower_shadow / body
        if ratio >= ratio_threshold:
            if close_price >= open_price:
                return True, score_full
            return True, score_partial

        return False, 0.0

    def _detect_volume_stagnation(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测放量滞涨

        当前K线成交量 ≥ 前5根均量 × 1.5，且收盘价低于前一根高点。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        current = window[-1]
        prev_klines = window[:-1]

        current_volume = float(current.get("volume", 0))
        current_close = float(current.get("close", 0))
        prev_high = float(prev_klines[-1].get("high", 0)) if prev_klines else 0

        avg_volume = sum(float(k.get("volume", 0)) for k in prev_klines) / len(prev_klines) if prev_klines else 0

        volume_ratio = self.short_volume_stag.get("volume_ratio", 1.5)
        score_full = self.short_volume_stag.get("score_full", 3.0)
        score_partial = self.short_volume_stag.get("score_partial", 2.0)

        if avg_volume == 0:
            return False, 0.0

        if current_volume >= avg_volume * volume_ratio:
            if current_close < prev_high:
                return True, score_full
            return True, score_partial

        return False, 0.0

    def _detect_volume_reversal(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测放量止跌

        当前K线成交量 ≥ 前5根均量 × 1.5，且收盘价高于前一根低点。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        current = window[-1]
        prev_klines = window[:-1]

        current_volume = float(current.get("volume", 0))
        current_close = float(current.get("close", 0))
        prev_low = float(prev_klines[-1].get("low", 0)) if prev_klines else 0

        avg_volume = sum(float(k.get("volume", 0)) for k in prev_klines) / len(prev_klines) if prev_klines else 0

        volume_ratio = self.long_volume_rev.get("volume_ratio", 1.5)
        score_full = self.long_volume_rev.get("score_full", 3.0)
        score_partial = self.long_volume_rev.get("score_partial", 2.0)

        if avg_volume == 0:
            return False, 0.0

        if current_volume >= avg_volume * volume_ratio:
            if current_close > prev_low:
                return True, score_full
            return True, score_partial

        return False, 0.0

    # ================================================================
    # 思路2：替代形态检测方法
    # ================================================================

    def _detect_double_top(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测双顶形态（做空替代形态）

        最近5根K线中出现两个相近的高点（差值<2%），且中间有回落。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        highs = [(i, float(k.get("high", 0))) for i, k in enumerate(window)]

        score = self.short_double_top.get("score", 2.0)
        tolerance = self.short_double_top.get("tolerance", 0.02)

        # 找到两个最高的高点
        sorted_highs = sorted(highs, key=lambda x: x[1], reverse=True)
        if len(sorted_highs) < 2:
            return False, 0.0

        top1_idx, top1_val = sorted_highs[0]
        top2_idx, top2_val = sorted_highs[1]

        # 两个高点必须间隔至少 min_gap 根K线（中间有回落）
        min_gap = self.short_double_top.get("min_gap", 2)
        if abs(top1_idx - top2_idx) < min_gap:
            return False, 0.0

        # 两个高点必须相近（差值 < tolerance）
        if abs(top1_val - top2_val) / top1_val >= tolerance:
            return False, 0.0

        return True, score

    def _detect_double_bottom(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测双底形态（做多替代形态）

        最近5根K线中出现两个相近的低点（差值<2%），且中间有反弹。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        lows = [(i, float(k.get("low", 0))) for i, k in enumerate(window)]

        score = self.long_double_bottom.get("score", 2.0)
        tolerance = self.long_double_bottom.get("tolerance", 0.02)

        # 找到两个最低的低点
        sorted_lows = sorted(lows, key=lambda x: x[1])
        if len(sorted_lows) < 2:
            return False, 0.0

        bottom1_idx, bottom1_val = sorted_lows[0]
        bottom2_idx, bottom2_val = sorted_lows[1]

        # 两个低点必须间隔至少 min_gap 根K线（中间有反弹）
        min_gap = self.long_double_bottom.get("min_gap", 2)
        if abs(bottom1_idx - bottom2_idx) < min_gap:
            return False, 0.0

        # 两个低点必须相近（差值 < tolerance）
        if abs(bottom1_val - bottom2_val) / bottom1_val >= tolerance:
            return False, 0.0

        return True, score

    def _detect_v_reversal_short(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测V型反转做空（做空替代形态）

        最近K线中，至少2根连续阳线后出现一根大阴线（收盘<开盘，实体>前几根均实体）。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        score = self.short_v_reversal.get("score", 2.0)
        consecutive_up = self.short_v_reversal.get("consecutive_up", 2)

        if len(window) < consecutive_up + 1:
            return False, 0.0

        # 检查前 consecutive_up 根是否是连续阳线
        for i in range(len(window) - consecutive_up - 1, len(window) - 1):
            k = window[i]
            if float(k.get("close", 0)) <= float(k.get("open", 0)):
                return False, 0.0

        # 检查最后一根是否是大阴线
        last = window[-1]
        last_open = float(last.get("open", 0))
        last_close = float(last.get("close", 0))
        if last_close >= last_open:
            return False, 0.0

        # 阴线实体 > 前几根平均实体 × 倍数
        body = abs(last_close - last_open)
        body_multiplier = self.short_v_reversal.get("body_multiplier", 1.5)
        prev_bodies = []
        for i in range(len(window) - consecutive_up - 1, len(window) - 1):
            prev_bodies.append(abs(float(window[i].get("close", 0)) - float(window[i].get("open", 0))))
        avg_prev_body = sum(prev_bodies) / len(prev_bodies) if prev_bodies else 0

        if avg_prev_body > 0 and body > avg_prev_body * body_multiplier:
            return True, score

        return False, 0.0

    def _detect_v_reversal_long(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测V型反转做多（做多替代形态）

        最近K线中，至少2根连续阴线后出现一根大阳线（收盘>开盘，实体>前几根均实体）。

        Args:
            klines: K线数据列表

        Returns:
            (是否检测到, 评分)
        """
        window = klines[-self.window_size:]
        score = self.long_v_reversal.get("score", 2.0)
        consecutive_down = self.long_v_reversal.get("consecutive_down", 2)

        if len(window) < consecutive_down + 1:
            return False, 0.0

        # 检查前 consecutive_down 根是否是连续阴线
        for i in range(len(window) - consecutive_down - 1, len(window) - 1):
            k = window[i]
            if float(k.get("close", 0)) >= float(k.get("open", 0)):
                return False, 0.0

        # 检查最后一根是否是大阳线
        last = window[-1]
        last_open = float(last.get("open", 0))
        last_close = float(last.get("close", 0))
        if last_close <= last_open:
            return False, 0.0

        # 阳线实体 > 前几根平均实体 × 倍数
        body = abs(last_close - last_open)
        body_multiplier = self.long_v_reversal.get("body_multiplier", 1.5)
        prev_bodies = []
        for i in range(len(window) - consecutive_down - 1, len(window) - 1):
            prev_bodies.append(abs(float(window[i].get("close", 0)) - float(window[i].get("open", 0))))
        avg_prev_body = sum(prev_bodies) / len(prev_bodies) if prev_bodies else 0

        if avg_prev_body > 0 and body > avg_prev_body * body_multiplier:
            return True, score

        return False, 0.0