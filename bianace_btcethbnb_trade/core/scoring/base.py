#!/usr/bin/env python3
"""
评分引擎基类

定义评分引擎的标准接口，所有版本都必须实现这些方法
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class ScoringEngineBase(ABC):
    """
    评分引擎基类

    定义评分引擎的标准接口和公共方法
    所有版本的评分引擎都必须继承此类并实现抽象方法
    """

    def __init__(self, config_file: str = None):
        """
        初始化评分引擎

        Args:
            config_file: 配置文件路径（相对于项目根目录）
        """
        self.config = self._load_config(config_file) if config_file else self._get_default_config()
        self.performance_history = deque(maxlen=20)
        self.score_distribution = defaultdict(int)

    def _load_config(self, config_file: str) -> Dict[str, Any]:
        """
        加载配置文件

        Args:
            config_file: 配置文件路径

        Returns:
            配置字典
        """
        config_path = Path(__file__).parent.parent.parent / config_file
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        else:
            return self._get_default_config()

    @abstractmethod
    def _get_default_config(self) -> Dict[str, Any]:
        """
        获取默认配置

        Returns:
            默认配置字典
        """
        pass

    @abstractmethod
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行评分（主入口）

        Args:
            symbol: 交易对符号
            data: 市场数据字典

        Returns:
            评分结果字典，包含：
            - symbol: 交易对
            - score: 总分
            - grade: 等级（S/A/B/C/None）
            - direction: 方向（多/空）
            - position_ratio: 仓位比例
            - veto_reason: 否决原因（如果有）
            - breakdown: 各维度得分明细
        """
        pass

    @abstractmethod
    def _check_veto(self, data: Dict[str, Any]) -> Optional[str]:
        """
        一票否决检查

        Args:
            data: 市场数据字典

        Returns:
            否决原因，如果通过则返回None
        """
        pass

    @abstractmethod
    def _check_market_state(self, data: Dict[str, Any]) -> str:
        """
        市场状态检查

        Args:
            data: 市场数据字典

        Returns:
            市场状态（trending/ranging/其他）
        """
        pass

    @abstractmethod
    def _score_trend_strength(self, indicators: Dict[str, Any]) -> float:
        """
        趋势强度评分

        Args:
            indicators: 技术指标字典

        Returns:
            趋势强度得分（0-15分）
        """
        pass

    @abstractmethod
    def _score_trend_consistency(self, indicators: Dict[str, Any]) -> float:
        """
        趋势一致性评分

        Args:
            indicators: 技术指标字典

        Returns:
            趋势一致性得分（0-15分）
        """
        pass

    @abstractmethod
    def _score_pattern(self, indicators: Dict[str, Any]) -> float:
        """
        形态质量评分

        Args:
            indicators: 技术指标字典

        Returns:
            形态质量得分（0-30分）
        """
        pass

    @abstractmethod
    def _score_volume(self, indicators: Dict[str, Any]) -> float:
        """
        成交量评分

        Args:
            indicators: 技术指标字典

        Returns:
            成交量得分（0-10分）
        """
        pass

    @abstractmethod
    def _score_momentum(self, indicators: Dict[str, Any]) -> float:
        """
        动量评分

        Args:
            indicators: 技术指标字典

        Returns:
            动量得分（0-20分）
        """
        pass

    @abstractmethod
    def _score_risk(self, symbol: str, data: Dict[str, Any]) -> float:
        """
        风险溢价评分

        Args:
            symbol: 交易对符号
            data: 市场数据字典

        Returns:
            风险溢价得分（0-10分）
        """
        pass

    @abstractmethod
    def _calculate_position_ratio(self, score: float, grade: Optional[str]) -> float:
        """
        计算仓位比例

        Args:
            score: 总分
            grade: 等级

        Returns:
            仓位比例（0.0-1.0）
        """
        pass

    def _check_data_integrity(self, data: Dict[str, Any]) -> Tuple[bool, float]:
        """
        数据完整性检查

        Args:
            data: 市场数据字典

        Returns:
            (是否有效, 置信度)
        """
        indicators = data.get('indicators', {})
        if not indicators:
            return False, 0.0
        return True, 1.0

    def _map_grade(self, score: float, thresholds: Dict[str, int]) -> Tuple[Optional[str], float]:
        """
        等级映射

        Args:
            score: 总分
            thresholds: 等级阈值字典

        Returns:
            (等级, 百分位)
        """
        if score >= thresholds['S']:
            return 'S', 0.95
        elif score >= thresholds['A']:
            return 'A', 0.80
        elif score >= thresholds['B']:
            return 'B', 0.60
        elif score >= thresholds['C']:
            return 'C', 0.40
        else:
            return None, score / 100

    def _determine_direction(self, indicators: Dict[str, Any]) -> str:
        """
        方向判断

        Args:
            indicators: 技术指标字典

        Returns:
            方向（多/空）
        """
        directions = []

        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue

            tf_data = indicators[tf]
            ema21 = tf_data.get('ema21', [])

            if not isinstance(ema21, list):
                ema21 = [ema21]

            if len(ema21) >= 1:
                if len(ema21) > 1 and ema21[-1] > ema21[-2]:
                    directions.append(1)
                else:
                    directions.append(-1)

        if not directions:
            return '多'

        if sum(directions) > 0:
            return '多'
        else:
            return '空'
