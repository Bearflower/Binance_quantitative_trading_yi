"""
形态识别模块
检测三次冲顶、长上影线、放量滞涨等做空信号形态

形态检测自适应策略：
- 长上影线：仅需1根K线，不受K线数量限制
- 三次冲顶：自适应窗口大小，K线越多检测越精确
- 放量滞涨：自适应基线计算，K线越多基线越稳定

所有自适应阈值从 config.yaml 的 pattern.adaptive 段读取。
"""
from typing import Dict, List, Tuple, Any
import structlog


logger = structlog.get_logger()


class PatternRecognizer:
    """形态识别器

    识别新币做空的关键形态：
    - 三次冲顶：检测三个高点是否接近（自适应窗口）
    - 长上影线：上影线/实体 > 2
    - 放量滞涨：成交量放大但价格涨幅小（自适应基线）
    """

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        pattern_config = self.config.get('pattern', {})

        three_tops = pattern_config.get('three_tops', {})
        self.three_tops_max_deviation = three_tops.get('max_deviation', 0.02)
        self.three_tops_score_high = three_tops.get('score_high', 4.0)
        self.three_tops_score_medium = three_tops.get('score_medium', 3.0)

        long_shadow = pattern_config.get('long_upper_shadow', {})
        self.shadow_ratio_threshold = long_shadow.get('ratio_threshold', 2.0)
        self.shadow_score_high = long_shadow.get('score_high', 3.0)
        self.shadow_score_medium = long_shadow.get('score_medium', 2.0)

        vol_div = pattern_config.get('volume_divergence', {})
        self.volume_ratio_threshold = vol_div.get('volume_ratio_threshold', 1.5)
        self.price_change_threshold = vol_div.get('price_change_threshold', 0.02)
        self.vol_div_score_high = vol_div.get('score_high', 3.0)
        self.vol_div_score_medium = vol_div.get('score_medium', 2.0)

        adaptive = pattern_config.get('adaptive', {})
        self.min_klines_for_detection = adaptive.get('min_klines_for_detection', 2)

        peak_window = adaptive.get('peak_window', {})
        self.pw_threshold_high = peak_window.get('threshold_high', 30)
        self.pw_window_high = peak_window.get('window_high', 5)
        self.pw_threshold_mid = peak_window.get('threshold_mid', 20)
        self.pw_window_mid = peak_window.get('window_mid', 3)
        self.pw_window_low = peak_window.get('window_low', 2)

        self.peak_extra_margin = adaptive.get('peak_extra_margin', 3)

        vol_div_adaptive = adaptive.get('volume_divergence', {})
        self.vd_tier_high_min = vol_div_adaptive.get('tier_high_min', 25)
        self.vd_tier_high_recent_n = vol_div_adaptive.get('tier_high_recent_n', 5)
        self.vd_tier_mid_min = vol_div_adaptive.get('tier_mid_min', 18)
        self.vd_tier_mid_recent_n = vol_div_adaptive.get('tier_mid_recent_n', 3)
        self.vd_tier_low_min = vol_div_adaptive.get('tier_low_min', 10)
        self.vd_tier_low_recent_n = vol_div_adaptive.get('tier_low_recent_n', 2)

        score_tier = adaptive.get('score_tier', {})
        self.three_tops_high_deviation = score_tier.get('three_tops_high_deviation', 0.01)
        self.long_shadow_high_ratio = score_tier.get('long_shadow_high_ratio', 3.0)
        self.volume_divergence_high_ratio = score_tier.get('volume_divergence_high_ratio', 2.0)

        logger.info("形态识别器初始化完成（自适应模式）")

    def _adaptive_peak_window(self, kline_count: int) -> int:
        if kline_count >= self.pw_threshold_high:
            return self.pw_window_high
        elif kline_count >= self.pw_threshold_mid:
            return self.pw_window_mid
        else:
            return self.pw_window_low

    def detect(self, klines: List[Dict]) -> Dict[str, Any]:
        if not klines or len(klines) < self.min_klines_for_detection:
            logger.warning("K线数据不足，无法检测形态", kline_count=len(klines) if klines else 0)
            return {
                'three_tops': (False, 0.0),
                'long_upper_shadow': (False, 0.0),
                'volume_divergence': (False, 0.0)
            }

        kline_count = len(klines)
        three_tops, three_tops_score = self.detect_three_tops(klines)
        long_shadow, long_shadow_score = self.detect_long_upper_shadow(klines)
        vol_div, vol_div_score = self.detect_volume_divergence(klines)

        result = {
            'three_tops': (three_tops, three_tops_score),
            'long_upper_shadow': (long_shadow, long_shadow_score),
            'volume_divergence': (vol_div, vol_div_score)
        }

        logger.info(
            "形态检测完成",
            kline_count=kline_count,
            three_tops=three_tops,
            three_tops_score=three_tops_score,
            long_upper_shadow=long_shadow,
            long_upper_shadow_score=long_shadow_score,
            volume_divergence=vol_div,
            volume_divergence_score=vol_div_score
        )

        return result

    def detect_three_tops(self, klines: List[Dict]) -> Tuple[bool, float]:
        kline_count = len(klines)
        window = self._adaptive_peak_window(kline_count)

        min_required = 2 * window + self.peak_extra_margin
        if kline_count < min_required:
            return False, 0.0

        peaks = self._find_peaks(klines, window)

        if len(peaks) < 3:
            logger.debug("未找到足够的高点", kline_count=kline_count, window=window, peaks_found=len(peaks))
            return False, 0.0

        last_three_peaks = peaks[-3:]
        avg_peak = sum(p['high'] for p in last_three_peaks) / 3

        max_deviation = max(
            abs(p['high'] - avg_peak) / avg_peak
            for p in last_three_peaks
        )

        if max_deviation < self.three_tops_max_deviation:
            if max_deviation < self.three_tops_high_deviation:
                score = self.three_tops_score_high
            else:
                score = self.three_tops_score_medium

            logger.info(
                "检测到三次冲顶形态",
                kline_count=kline_count,
                window=window,
                max_deviation=max_deviation,
                score=score,
                peaks=[p['high'] for p in last_three_peaks]
            )
            return True, score

        return False, 0.0

    def detect_long_upper_shadow(self, klines: List[Dict]) -> Tuple[bool, float]:
        """
        检测最近N根K线中是否存在长上影线

        长上影线特征：上影线长度 / 实体长度 > ratio_threshold
        表示价格冲高后回落，多头力量衰竭
        """
        if not klines:
            return False, 0.0

        # 检查最近3根K线
        check_count = min(3, len(klines))

        best_score = 0.0
        for i in range(-check_count, 0):
            kline = klines[i]
            open_price = float(kline.get('open', 0))
            close_price = float(kline.get('close', 0))
            high_price = float(kline.get('high', 0))

            body = abs(close_price - open_price)
            upper_shadow = high_price - max(open_price, close_price)

            if body == 0:
                continue

            ratio = upper_shadow / body

            if ratio > self.shadow_ratio_threshold:
                if ratio > self.long_shadow_high_ratio:
                    score = self.shadow_score_high
                else:
                    score = self.shadow_score_medium

                # 取最高分
                if score > best_score:
                    best_score = score

        if best_score > 0:
            logger.info(
                "检测到长上影线",
                check_range=f"最近{check_count}根",
                score=best_score
            )
            return True, best_score

        return False, 0.0

    def detect_volume_divergence(self, klines: List[Dict]) -> Tuple[bool, float]:
        kline_count = len(klines)

        if kline_count >= self.vd_tier_high_min:
            recent_n = self.vd_tier_high_recent_n
            baseline_start = -20
            baseline_end = -5
        elif kline_count >= self.vd_tier_mid_min:
            recent_n = self.vd_tier_mid_recent_n
            baseline_start = -kline_count
            baseline_end = -3
        elif kline_count >= self.vd_tier_low_min:
            recent_n = self.vd_tier_low_recent_n
            baseline_start = -kline_count
            baseline_end = -2
        else:
            return False, 0.0

        recent_klines = klines[-recent_n:]
        baseline_klines = klines[baseline_start:baseline_end]

        if len(baseline_klines) < 2:
            return False, 0.0

        first_open = float(recent_klines[0].get('open', 0))
        last_close = float(recent_klines[-1].get('close', 0))

        if first_open == 0:
            return False, 0.0

        price_change = (last_close - first_open) / first_open

        baseline_count = len(baseline_klines)
        avg_volume = sum(float(k.get('volume', 0)) for k in baseline_klines) / baseline_count
        recent_volume = sum(float(k.get('volume', 0)) for k in recent_klines) / recent_n

        if avg_volume == 0:
            return False, 0.0

        volume_ratio = recent_volume / avg_volume

        if volume_ratio > self.volume_ratio_threshold and abs(price_change) < self.price_change_threshold:
            if volume_ratio > self.volume_divergence_high_ratio:
                score = self.vol_div_score_high
            else:
                score = self.vol_div_score_medium

            logger.info(
                "检测到放量滞涨",
                kline_count=kline_count,
                recent_n=recent_n,
                baseline_count=baseline_count,
                volume_ratio=volume_ratio,
                price_change=price_change,
                score=score
            )
            return True, score

        return False, 0.0

    def _find_peaks(self, klines: List[Dict], window: int = 5) -> List[Dict]:
        peaks = []
        n = len(klines)

        for i in range(window, n):
            current_high = float(klines[i].get('high', 0))

            is_peak = True
            # 左侧检查完整窗口
            for j in range(i - window, i):
                if j >= 0:
                    neighbor_high = float(klines[j].get('high', 0))
                    if neighbor_high > current_high:
                        is_peak = False
                        break
            # 右侧只检查存在的邻居
            if is_peak:
                for j in range(i + 1, min(i + window + 1, n)):
                    neighbor_high = float(klines[j].get('high', 0))
                    if neighbor_high > current_high:
                        is_peak = False
                        break

            if is_peak:
                peak_data = {
                    'index': i,
                    'high': current_high
                }
                if 'open_time' in klines[i]:
                    peak_data['time'] = klines[i]['open_time']

                peaks.append(peak_data)

        logger.debug("找到高点", peak_count=len(peaks), window=window, kline_count=n)
        return peaks