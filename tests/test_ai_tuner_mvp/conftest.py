# -*- coding: utf-8 -*-
"""
AI Tuner MVP 阶段一测试公共设施

提供：
- 项目根 / manifest 目录常量与真实配置 fixture
- 新契约（8 字段信封）/ 旧契约报文构造 helper
- auditor 测试用的 FakeDB（按 SQL 子串路由返回值，支持异常注入）
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

# 仓库根：tests/test_ai_tuner_mvp/conftest.py → 上两级
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = PROJECT_ROOT / "ai_tuner" / "parameters" / "manifests"

# 新契约唯一优化目标（与 response_parser 常量同源，测试侧独立声明以便校验契约值本身）
NEW_OBJECTIVE = "maximize_risk_adjusted_net_return_under_drawdown_cap"


def make_envelope(**overrides):
    """构造新契约 8 字段合法信封，可用关键字参数覆盖任意字段"""
    envelope = {
        "decision": "PROPOSE",
        "objective": NEW_OBJECTIVE,
        "diagnosis": ["上周开仓次数偏少，入场门槛偏高"],
        "target_metrics": {"trade_count": ">= 5/周"},
        "adjustments": {"strategy.scoring.min_score": {"from": 60, "to": 62}},
        "applicable_market_regimes": ["trending", "ranging"],
        "expected_impact": {"trade_count": "+10%"},
        "confidence": 0.7,
    }
    envelope.update(overrides)
    return envelope


def dump_envelope(**overrides) -> str:
    """信封转 JSON 字符串"""
    return json.dumps(make_envelope(**overrides), ensure_ascii=False)


def make_legacy_payload(**overrides):
    """构造旧契约报文（btc_eth / new_coin / grid 等使用的宽松格式）"""
    payload = {
        "reasons": ["胜率下降", "回撤扩大"],
        "summary": "建议下调入场门槛",
        "adjustments": {"scoring.min_score": {"from": 60, "to": 62}},
        "expected_impact": "交易次数提升",
        "confidence": 0.6,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 真实配置 / manifest fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def aggressive_manifest():
    """从磁盘加载 btc_eth_aggressive 真实 manifest"""
    from ai_tuner.parameters.manifest import ManifestRegistry

    return ManifestRegistry(MANIFEST_DIR).load("btc_eth_aggressive")


@pytest.fixture
def hrs_manifest():
    """从磁盘加载 hrs 真实 manifest"""
    from ai_tuner.parameters.manifest import ManifestRegistry

    return ManifestRegistry(MANIFEST_DIR).load("hrs")


@pytest.fixture
def aggressive_config():
    """btc_eth_aggressive 策略当前生效配置"""
    with open(PROJECT_ROOT / "strategies" / "btc_eth_aggressive" / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def hrs_config():
    """hrs 策略当前生效配置"""
    with open(PROJECT_ROOT / "strategies" / "hrs" / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def tuner_config():
    """ai_tuner 系统配置（真实 config.yaml）"""
    with open(PROJECT_ROOT / "ai_tuner" / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def as_of():
    """固定审计截止时间（UTC），保证窗口与 K 线计数可预计算"""
    return datetime(2026, 10, 17, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# auditor 测试桩：FakeDB
# ---------------------------------------------------------------------------

# 全达标基线数据（对照 ai_tuner/config.yaml 的 readiness 阈值）：
# closed=100(>=20) signals=200(>=100) complete=1.0(>=0.95)
# identity=0.90(>=0.90) kline=2160/2160(>=0.98) snapshot=0.0(>=0.0) regime=5(>=3)
BASELINE_RESPONSES = {
    # trade_signals 列校验（information_schema.columns）
    "column_name = 'strategy_id'": {"?column?": 1},
    # trade_signals schema 探测（information_schema.tables, fetch_all）
    "table_name = 'trade_signals'": [{"table_schema": "btc_eth"}],
    # trade_records 完整度查询
    "AS closed_trades": {"closed_trades": 100, "complete_ratio": 1.0},
    # regime proxy（COUNT DISTINCT symbol）
    "AS syms": {"syms": 5},
    # 信号计数
    "FROM btc_eth.trade_signals": {"cnt": 200},
    # K 线 schema 探测（ORDER BY 区分于表名探测）
    "ORDER BY table_schema": {"table_schema": "btc_eth"},
    # K 线表名探测
    "table_name ILIKE": {"table_name": "klines_1h"},
    # K 线时间戳列探测
    "column_name IN": {"column_name": "timestamp"},
    # K 线计数（90 天 * 24 bar = 2160）
    "FROM btc_eth.klines_1h": {"cnt": 2160},
}


class FakeDB:
    """
    异步数据库桩：按 SQL 子串路由返回值

    - responses: {SQL子串: 返回值}，按插入顺序匹配第一个命中的键
    - errors:    {SQL子串: 异常实例}，优先级高于 responses，用于覆盖异常分支
    - sql_log:   记录全部执行的 SQL，供断言（如确认未发起覆盖层写入）
    """

    def __init__(self, responses=None, errors=None):
        # 注意：不能写 `responses or BASELINE`——空字典是"全部探测失败"的有效用例
        self.responses = BASELINE_RESPONSES if responses is None else dict(responses)
        self.errors = errors or {}
        self.sql_log = []

    def _route(self, sql):
        for key, exc in self.errors.items():
            if key in sql:
                raise exc
        for key, value in self.responses.items():
            if key in sql:
                return value
        return None

    async def fetch_one(self, sql, *args):
        self.sql_log.append(sql)
        return self._route(sql)

    async def fetch_all(self, sql, *args):
        self.sql_log.append(sql)
        value = self._route(sql)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


@pytest.fixture
def fake_db():
    """全达标基线 FakeDB"""
    return FakeDB()
