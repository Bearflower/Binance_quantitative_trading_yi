"""
90 天准备度审计结果数据模型

定义候选策略的各项准备度指标和最终审计结果。
"""

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class CandidateReadiness:
    """单个候选策略的准备度指标"""
    strategy_id: str

    # --- 基础数据量 ---
    closed_trades: int          # 窗口内已平仓交易数
    effective_signals: int      # 窗口内有效信号数（来自该策略 schema 下的信号表，按 ILIKE '%signal%' 探测）
    regime_count: int           # 窗口内覆盖的市场状态数

    # --- 字段完整度 ---
    complete_trade_ratio: float         # trade_records 必填字段完整度
    config_identity_ratio: float        # 配置身份可追溯度（版本覆盖层 ↔ 交易记录）
    # 按策略 universe 逐 symbol 加权的覆盖率（sum(actual)/sum(expected)），
    # expected/actual 均按闭合 bar 计数、去重 open_time，缺表按 0%。
    kline_coverage: float
    external_snapshot_coverage: float   # 外部时点快照完整度（OI/市值/资金费率/情绪）

    # --- 外部快照门禁策略级元数据 ---
    # 取值："required"（缺失即 ineligible）或 "not_required_for_current_manifest"（报告项）
    external_snapshot_requirement: str = "required"
    # 失败原因代码：required_historical_external_snapshots_missing / not_required_for_current_manifest
    external_snapshot_reason: str = ""
    external_snapshot_expected: int = 0
    external_snapshot_complete: int = 0
    external_snapshot_evidence_start: Optional[str] = None
    external_snapshot_sources: List[str] = field(default_factory=list)

    # --- 失败清单（每个条目对应一个未过的 gate，含实际值和阈值）---
    failures: List[str] = field(default_factory=list)

    # --- 数据缺口（无法采集的指标来源说明，用于报告）---
    gaps: List[str] = field(default_factory=list)

    # --- K 线覆盖逐 symbol/interval 细目（阶段二 Task 3）---
    # 由 kline_coverage.compute_kline_coverage 填充；内容为 JSON 安全的字典
    # （datetime 一律 isoformat 字符串），ReadinessResult.to_dict → asdict 后可直接 json.dumps。
    # 键：symbol / interval / expected / actual / coverage / missing_ranges（ISO 字符串对）/ gap
    kline_symbols: List[Dict] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        """无失败项即为合格"""
        return not self.failures


@dataclass(frozen=True)
class ReadinessResult:
    """全部候选策略的审计结果"""
    as_of: str                  # 审计截止时间（ISO 8601）
    window_days: int            # 审计窗口长度（天）
    decision: str               # SELECT / STOP
    selected_strategy: Optional[str]
    candidates: Dict[str, CandidateReadiness]

    def to_dict(self) -> Dict:
        """可 JSON 序列化的字典（嵌套 CandidateReadiness 也展开）"""
        return {
            "as_of": self.as_of,
            "window_days": self.window_days,
            "decision": self.decision,
            "selected_strategy": self.selected_strategy,
            "candidates": {
                sid: asdict(cr) for sid, cr in self.candidates.items()
            },
        }
