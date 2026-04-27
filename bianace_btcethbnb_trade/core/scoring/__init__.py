#!/usr/bin/env python3
"""
评分引擎模块

提供统一的评分引擎接口和版本管理
"""

from core.scoring.base import ScoringEngineBase
from core.scoring.v612 import ScoringEngineV612
from core.scoring.factory import (
    get_scoring_engine,
    get_scoring_engine_v612,
    create_scoring_engine,
    list_available_versions,
)

__all__ = [
    'ScoringEngineBase',
    'ScoringEngineV612',
    'get_scoring_engine',
    'get_scoring_engine_v612',
    'create_scoring_engine',
    'list_available_versions',
]
