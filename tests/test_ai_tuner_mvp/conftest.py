# -*- coding: utf-8 -*-
"""
AI Tuner MVP 阶段一测试公共设施

提供：
- 项目根 / manifest 目录常量与真实配置 fixture
- 新契约（8 字段信封）/ 旧契约报文构造 helper
- auditor 测试用的 FakeDB（按 SQL 子串路由返回值，支持异常注入 + 调用参数记录）
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
#
# FakeDB 按 SQL 子串路由返回值。
# 由于新 auditor.py 每个策略的信号/SQL 字符串本身不同
# （schema/表名/时间列名都字面嵌入），所以两个策略不会路由错。
#
# BASELINE_RESPONSES 覆盖所有正常路径下 auditor 会发起的查询。
# 键设计：用 SQL 中足够独特的子串定位唯一路由，避免误命中。

# 全达标基线数据（对照 ai_tuner/config.yaml 的 readiness 阈值）：
# closed=100(>=20) signals=200(>=100) complete=1.0(>=0.95)
# identity=0.90(>=0.90) kline=2160/2160(>=0.98) snapshot=0.0(>=0.0) regime=5(>=3)
# 主力合约: BTCUSDT；K线表: btc_eth.kline_btcusdt_1h（90 天 * 24 bar = 2160）
#
# 路由键说明：
# - str 类型: 仅 SQL 子串匹配（整个 SQL 字符串含该子串即命中）
# - (str, tuple) 类型: SQL 子串匹配 + args 前缀匹配
#   用于同一 SQL 模板但不同参数需返回不同值的场景（如 aggressive/hrs 信号表探测）
#
# 注意：FakeDB 内部 SQL 含真实换行符，路由键必须包含换行才能精确匹配。
# 为简化，这里用 SQL 模板中足够独特的、无换行的子串定位。
_BASELINE_INFORMATION_SCHEMA_COLUMNS = (
    "FROM information_schema.columns\n        WHERE table_schema = $1 "
    "AND table_name = $2 AND column_name = $3"
)

BASELINE_RESPONSES = {
    # ============ 时间列类型探测（args-aware，精确到列名）============
    # trading.trade_records.executed_at → naive（被 _bind_window 多处调用）
    (_BASELINE_INFORMATION_SCHEMA_COLUMNS,
     ("trading", "trade_records", "executed_at")): {
        "data_type": "timestamp without time zone"
    },
    # aggressive 信号表时间列探测（循环候选列，created_at 第一个命中）→ naive
    # 精确 3 参数前缀：只有 ('btc_eth_aggressive', 'trade_signals', 'created_at') 命中
    (_BASELINE_INFORMATION_SCHEMA_COLUMNS,
     ("btc_eth_aggressive", "trade_signals", "created_at")): {
        "data_type": "timestamp without time zone"
    },
    # hrs 信号表时间列探测（created_at/signal_time/executed_at 不存在，triggered_at 命中）→ aware
    # 精确 3 参数前缀：只有 ('hrs', 'hrs_signals', 'triggered_at') 命中返回 aware；
    # 其他候选列的调用遍历所有键都不命中 → FakeDB 返回 None → auditor 继续下一个候选
    (_BASELINE_INFORMATION_SCHEMA_COLUMNS,
     ("hrs", "hrs_signals", "triggered_at")): {
        "data_type": "timestamp with time zone"
    },
    # K 线时间列探测
    (_BASELINE_INFORMATION_SCHEMA_COLUMNS,
     ("btc_eth", "kline_btcusdt_1h", "open_time")): {
        "data_type": "timestamp without time zone"
    },

    # ============ schema / 表存在性探测 ============
    # information_schema.schemata 验证 schema 存在（每个策略各查一次，返回存在即可）
    "FROM information_schema.schemata WHERE schema_name = $1": {"?column?": 1},

    # aggressive 信号表探测（f-string SQL 含字面 'btc_eth_aggressive'）
    "WHERE table_schema = 'btc_eth_aggressive' AND table_name ILIKE": [
        {"table_name": "trade_signals"},
    ],
    # hrs 信号表探测（f-string SQL 含字面 'hrs'）
    "WHERE table_schema = 'hrs' AND table_name ILIKE": [
        {"table_name": "hrs_signals"},
    ],

    # K 线 schema 探测（仍保留，返回 btc_eth）
    "ORDER BY table_schema": {"table_schema": "btc_eth"},

    # K 线表名探测（按 symbol 过滤）
    # auditor 的 _detect_kline_table_name 用参数化 SQL：
    #   WHERE table_schema = $1 AND table_name ILIKE $2 ORDER BY table_name LIMIT 1
    # 优先 1h（pattern kline_<symbol>_%h）、退而任意周期（kline_<symbol>_%），
    # 两个调用共享同一 SQL 模板但不同 args 前缀。
    # 纯子串 WHERE table_schema = $1 AND table_name ILIKE $2 即可唯一命中
    # （_find_signal_table 只有 ILIKE $1 不会误命中）。
    "WHERE table_schema = $1 AND table_name ILIKE $2": {"table_name": "kline_btcusdt_1h"},

    # K 线时间戳列探测（候选列集合）
    "column_name IN ('timestamp', 'open_time'": {"column_name": "open_time"},

    # ============ 主业务查询（SQL 模板唯一，纯子串匹配）============
    # trade_records 完整度查询
    "AS closed_trades": {"closed_trades": 100, "complete_ratio": 1.0},

    # regime proxy（COUNT DISTINCT symbol）
    "COUNT(DISTINCT symbol) AS syms": {"syms": 5},

    # 主力合约查询（GROUP BY symbol + COUNT(*) DESC LIMIT 1）
    # 用于 K 线表定位；所有策略共享同一模板 → 返回同一 BTCUSDT
    "GROUP BY symbol ORDER BY COUNT(*) DESC": {"symbol": "BTCUSDT"},

    # aggressive 信号计数（FROM 子句含字面 schema/table/ts_col）
    "FROM btc_eth_aggressive.trade_signals": {"cnt": 200},

    # hrs 信号计数
    "FROM hrs.hrs_signals": {"cnt": 200},

    # K 线计数（schema/table/ts_col 字面在 FROM/WHERE 里）
    "FROM btc_eth.kline_btcusdt_1h": {"cnt": 2160},
}


class FakeDB:
    """
    异步数据库桩：按 SQL 子串 + args 前缀联合路由返回值

    responses 键支持两种形式：
    - str: 仅 SQL 子串匹配（原有语义）
    - (str, tuple): SQL 子串匹配 + args 前缀匹配（args[:len(prefix)] == prefix）
      用于同一 SQL 模板但不同参数需返回不同值的场景
    errors 同理。

    - sql_log:         记录全部执行的 SQL
    - call_args_log:   记录每次调用的 (method, sql, args)
    """

    def __init__(self, responses=None, errors=None):
        self.responses = BASELINE_RESPONSES if responses is None else dict(responses)
        self.errors = errors or {}
        self.sql_log = []
        self.call_args_log = []

    def _route(self, sql, args=()):
        for key, exc in self.errors.items():
            if self._match(key, sql, args):
                raise exc
        for key, value in self.responses.items():
            if self._match(key, sql, args):
                return value
        return None

    @staticmethod
    def _match(key, sql, args):
        """路由键匹配：支持 str（仅 SQL 子串）或 (str, tuple)（SQL + args 前缀）"""
        if isinstance(key, str):
            return key in sql
        # tuple: (SQL子串, args前缀)
        sql_key, args_prefix = key
        if sql_key not in sql:
            return False
        if args_prefix is None:
            return True
        n = len(args_prefix)
        return args[:n] == args_prefix

    async def fetch_one(self, sql, *args):
        self.sql_log.append(sql)
        self.call_args_log.append(("fetch_one", sql, args))
        value = self._route(sql, args)
        if value is None:
            return None
        # 如果路由返回 list（为 _find_signal_table 等 fetch_one 调用返回单条结果），
        # 取第一个元素；正常情况下 fetch_one 路由返回的是 dict
        if isinstance(value, list):
            return value[0] if value else None
        return value

    async def fetch_all(self, sql, *args):
        self.sql_log.append(sql)
        self.call_args_log.append(("fetch_all", sql, args))
        value = self._route(sql, args)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


@pytest.fixture
def fake_db():
    """全达标基线 FakeDB"""
    return FakeDB()
