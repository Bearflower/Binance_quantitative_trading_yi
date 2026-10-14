"""
HRS PNL_SUMMARY 回填工具（dry-run only，不执行）

H6 根因修复后，需要回填历史"只有明细没有汇总"的持仓组。

设计原则：回填锚 income 真值，汇总优先压明细。
  - 金额来源：对账脚本 match_income_to_trades 匹配到的 income 归属真值 SUM
  - 宁缺勿错：某组有 trade_records 明细但 income 无法可靠归属 hrs → 移入未决清单，不回填
  - 幂等双保险：order_id 写 backfill- 批次号前缀 + 存在 PNL_SUMMARY 行兜底

本工具设计为：
  -- 默认 dry-run：读 data/trade_records.csv + data/full_income.json，
     调用 reconcile.match_income_to_trades 跑归属裁决，输出待回填清单 + 建议 SQL
  - 识别 21 组中含 LIMIT/CONDITIONAL_ORDER 开仓错行 pnl 的组 → 金额改 income 真值
  - 有 trade_records 但 income 无法归属 → 移入未决清单

使用：
  python3 ai_tuner/reconciler/hrs_pnl_backfill.py \\
      --csv data/trade_records.csv --income data/full_income.json
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from zoneinfo import ZoneInfo  # Python 3.9+

# 北京时区（UTC+8），实际用于 executed_at 时间语义说明
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


# ──────────────────────────────────────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRecordLite:
    """从 CSV 解析的 trade_records 行（精简版，用于分组）"""
    strategy: str
    symbol: str
    side: str
    order_type: str
    executed_at: Optional[datetime] = None
    realized_pnl: Optional[Decimal] = None
    close_reason: Optional[str] = None

    @classmethod
    def from_csv_row(cls, row: dict) -> Optional["TradeRecordLite"]:
        """从 csv.DictReader 一行构建，空 strategy/空 symbol 返回 None"""
        def _dec(v: str | None) -> Optional[Decimal]:
            if not v or v.strip() == "":
                return None
            try:
                return Decimal(v.strip())
            except InvalidOperation:
                return None

        def _dt(v: str | None) -> Optional[datetime]:
            if not v or v.strip() == "":
                return None
            v = v.strip()
            for fmt in (
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S%z",
            ):
                try:
                    return datetime.strptime(v, fmt)
                except ValueError:
                    continue
            return None

        strategy = (row.get("strategy") or "").strip()
        symbol = (row.get("symbol") or "").strip()
        if not strategy or not symbol:
            return None

        return cls(
            strategy=strategy,
            symbol=symbol,
            side=(row.get("side") or "").strip(),
            order_type=(row.get("order_type") or "").strip(),
            executed_at=_dt(row.get("executed_at")),
            realized_pnl=_dec(row.get("realized_pnl")),
            close_reason=(row.get("close_reason") or "").strip() or None,
        )


@dataclass
class BackfillGroup:
    """一个需要回填 PNL_SUMMARY 的持仓组

    幂等双保险：
      1. 批次号写入 order_id（backfill-YYYYMMDD-NN），重跑时 ON CONFLICT DO NOTHING 跳过
      2. 组内若已存在 PNL_SUMMARY 行 → 识别阶段就跳过（identify_backfill_groups 里已判定）
    """
    strategy: str
    symbol: str
    side: str
    # 该组所有 trade_records 明细行（含 LIMIT/CONDITIONAL_ORDER/STOP_MARKET/TAKE_PROFIT）
    rows: list[TradeRecordLite] = field(default_factory=list)
    # 该组归属 hrs 的 income 真值列表（来自 reconcile.match_income_to_trades）
    income_truths: list[dict] = field(default_factory=list)
    # 未决标记：income 无法归属 hrs → 不回填
    undecided: bool = False
    undecided_reason: str = ""

    @property
    def pnl_from_income(self) -> Decimal:
        """归属 hrs 的 income 真值 SUM（设计 §2.4 锚 income 真值）"""
        if not self.income_truths:
            return Decimal("0")
        total = Decimal("0")
        for inc in self.income_truths:
            total += Decimal(str(inc["income_amount"]))
        return total

    @property
    def pnl_from_trade_records(self) -> Decimal:
        """该组明细行 realized_pnl 的 SUM（**仅平仓类行**，排除 LIMIT/CONDITIONAL_ORDER）"""
        total = Decimal("0")
        for r in self.rows:
            if r.realized_pnl is None:
                continue
            if r.order_type in ("LIMIT", "CONDITIONAL_ORDER"):
                continue  # 开仓类，排除
            total += r.realized_pnl
        return total

    @property
    def last_executed_at(self) -> Optional[datetime]:
        """取 income 真值中最后一笔的时间（来自 income 本身，比 trade_records 明细更准）"""
        if self.income_truths:
            # income_time_ms 是 UTC ms
            max_ms = max(int(t["income_time_ms"]) for t in self.income_truths)
            # 转北京 naive（UTC+8）
            from datetime import timezone as tz
            bj_ts = datetime.fromtimestamp(max_ms / 1000, tz=BEIJING_TZ).replace(tzinfo=None)
            return bj_ts
        # 退化为 trade_records 末笔平仓明细
        closes = [
            r.executed_at for r in self.rows
            if r.order_type not in ("LIMIT", "CONDITIONAL_ORDER")
            and r.executed_at is not None
        ]
        if closes:
            return max(closes)
        return None

    @property
    def has_stop_loss(self) -> bool:
        """组内是否有 close_reason='STOP_LOSS' 的平仓明细"""
        return any(r.close_reason == "STOP_LOSS" for r in self.rows)

    @property
    def source_note(self) -> str:
        """金额来源标注（用于 dry-run 报告）"""
        if self.undecided:
            return f"未决：{self.undecided_reason}"
        diff = abs(self.pnl_from_income - self.pnl_from_trade_records)
        if diff > Decimal("0.01"):
            return (
                f"income真值={self.pnl_from_income}，明细SUM={self.pnl_from_trade_records}，"
                f"差额={diff}（明细含开仓错行，已锚真值）"
            )
        return f"income真值={self.pnl_from_income}，与明细SUM一致"

    def build_sql(self, batch_id: str) -> str:
        """生成单条 INSERT SQL（dry-run 用）"""
        ex_time = self.last_executed_at
        ex_time_str = ex_time.strftime("%Y-%m-%d %H:%M:%S") if ex_time else "NOW()"
        reason = "'STOP_LOSS'" if self.has_stop_loss else "NULL"
        # 金额锚 income 真值；CSV 中 quantity/price 多为 0，填 0 不影响核心价值
        return (
            "INSERT INTO trading.trade_records "
            "(strategy, symbol, order_id, side, order_type, quantity, price, "
            "commission, status, executed_at, realized_pnl, close_reason) "
            f"VALUES ('{self.strategy}', '{self.symbol}', '{batch_id}', '{self.side}', "
            f"'PNL_SUMMARY', 0, 0, "
            f"0, 'FILLED', '{ex_time_str}', {self.pnl_from_income}, {reason}) "
            "ON CONFLICT DO NOTHING;"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 核心逻辑
# ──────────────────────────────────────────────────────────────────────────────


def load_trade_records_lite(csv_path: Path) -> list[TradeRecordLite]:
    """从 CSV 加载 trade_records（精简模型，用于分组）"""
    records: list[TradeRecordLite] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rec = TradeRecordLite.from_csv_row(row)
            if rec is not None:
                records.append(rec)
    return records


def identify_pending_groups(records: list[TradeRecordLite]) -> list[BackfillGroup]:
    """
    扫描 records，识别需要回填 PNL_SUMMARY 的候选组。

    判定逻辑：
    1. 按 (strategy, symbol, side) 分组
    2. 组内有平仓明细（order_type NOT IN ('LIMIT','CONDITIONAL_ORDER','PNL_SUMMARY')）
       且 realized_pnl 不为 None → 该组"已平仓"
    3. 组内没有 PNL_SUMMARY 汇总行
    → 候选组（后续还需 income 归属裁决才能确定是否回填）
    """
    groups: dict[tuple[str, str, str], list[TradeRecordLite]] = defaultdict(list)
    for r in records:
        key = (r.strategy, r.symbol, r.side)
        groups[key].append(r)

    candidates: list[BackfillGroup] = []
    for key, rows in sorted(groups.items()):
        if not key[0] or not key[1]:
            continue

        # 已存在汇总 → 跳过（幂等双保险 #2）
        has_summary = any(r.order_type == "PNL_SUMMARY" for r in rows)
        if has_summary:
            continue

        # 有平仓明细 → 候选
        close_rows = [
            r for r in rows
            if r.order_type not in ("LIMIT", "CONDITIONAL_ORDER", "PNL_SUMMARY")
            and r.realized_pnl is not None
        ]
        if not close_rows:
            continue

        candidates.append(BackfillGroup(
            strategy=key[0], symbol=key[1], side=key[2], rows=rows,
        ))

    return candidates


def run_income_attribution(
    candidates: list[BackfillGroup],
    income_json_path: Path,
    trade_records_csv_path: Path,
) -> tuple[list[BackfillGroup], list[BackfillGroup]]:
    """
    调用 reconcile.match_income_to_trades 跑 income 归属裁决。

    对每个候选组 (hrs, symbol, side)，聚合归属 hrs 的 income 真值。
    组内有 trade_records 但 income 无法归属 hrs → 移入未决清单（宁缺勿错）。

    Returns:
        (回填清单, 未决清单)
    """
    # lazy import，避免 reconcile 不可用时直接崩
    import sys as _sys
    # 确保项目根在 path（从仓库根或 reconciler 子目录运行都能找到 ai_tuner 包）
    _repo_root = Path(__file__).resolve().parent.parent.parent.parent
    if str(_repo_root) not in _sys.path:
        _sys.path.insert(0, str(_repo_root))
    try:
        from ai_tuner.reconciler.hrs_income_reconcile import (
            HRSReconcileConfig,
            load_income_from_json,
            load_trade_records_csv,
            match_income_to_trades,
        )
    except ImportError as e:
        # 退而求其次：直接从 reconciler 目录 import（脚本在该目录下运行时）
        from hrs_income_reconcile import (  # type: ignore[no-redef]
            HRSReconcileConfig,
            load_income_from_json,
            load_trade_records_csv,
            match_income_to_trades,
        )

    # 1. 加载 income + trade_records（reconcile 模块的完整模型）
    income_result = load_income_from_json(str(income_json_path))
    full_trade_records = load_trade_records_csv(str(trade_records_csv_path))

    # 2. 跑匹配（使用 cfg 默认 match_window_ms=7 天，与对账脚本一致）
    cfg = HRSReconcileConfig()
    cfg.match_window_ms = 7 * 24 * 60 * 60 * 1000  # 7 天
    match_results, summary = match_income_to_trades(
        income_result.records, full_trade_records, cfg,
    )

    # 3. 聚合：按 (symbol, side) 分组归属 hrs 的 income
    #    match_result.matched_side 是从 trade_record 匹配来的 side
    by_symbol_side: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for mr in match_results:
        if not mr.matched:
            continue
        if mr.resolved_strategy != "hrs":
            continue
        # side 从匹配到的 trade_record 取
        side = mr.matched_side or "SELL"  # 平仓方向默认 SELL
        by_symbol_side[(mr.income_symbol, side)].append({
            "income_amount": mr.income_amount,
            "income_time_ms": mr.income_time_ms,
        })

    # 4. 把真值注入候选组
    to_fill: list[BackfillGroup] = []
    undecided: list[BackfillGroup] = []

    for g in candidates:
        key = (g.symbol, g.side)
        truths = by_symbol_side.get(key, [])
        g.income_truths = truths

        if truths:
            to_fill.append(g)
        else:
            # 有 trade_records 明细但 income 无法归属 hrs → 未决
            # 检查是否所有 income 都归属 new_coin（明确排除）
            all_income_symbols = set()
            for mr in match_results:
                if mr.income_symbol == g.symbol:
                    all_income_symbols.add(mr.resolved_strategy)
            if "new_coin" in all_income_symbols and "hrs" not in all_income_symbols:
                g.undecided_reason = f"income 全部归属 new_coin（非 hrs）"
            else:
                g.undecided_reason = "income 无法可靠归属 hrs（可能跨策略歧义或金额容差未通过）"
            g.undecided = True
            undecided.append(g)

    return to_fill, undecided


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HRS PNL_SUMMARY 回填工具（dry-run only，锚 income 真值）"
    )
    parser.add_argument(
        "--csv", type=Path,
        default=Path("data/trade_records.csv"),
        help="trade_records CSV 路径",
    )
    parser.add_argument(
        "--income", type=Path,
        default=Path("data/full_income.json"),
        help="income JSON 缓存路径（含 records 列表）",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="（本轮留接口，dry-run 输出 SQL 手动执行）",
    )
    parser.add_argument(
        "--strategy", type=str, default="hrs",
        help="只扫描指定策略（默认 hrs），空则扫全部",
    )
    args = parser.parse_args()

    # --- Step 1: 加载 trade_records CSV ---
    csv_path = args.csv
    if not csv_path.exists():
        print(f"错误：CSV 文件不存在 {csv_path}", file=sys.stderr)
        return 1

    print(f"[回填工具] Step 1: 加载 {csv_path} ...")
    all_records = load_trade_records_lite(csv_path)
    print(f"[回填工具] 共加载 {len(all_records)} 行")

    target_strategy = args.strategy.strip().lower()
    if target_strategy:
        records = [r for r in all_records if r.strategy.lower() == target_strategy]
        print(f"[回填工具] 过滤到 strategy='{target_strategy}' 共 {len(records)} 行")
    else:
        records = all_records

    # --- Step 2: 识别候选组 ---
    candidates = identify_pending_groups(records)
    print(f"[回填工具] Step 2: 识别到 {len(candidates)} 个候选组（有平仓明细但无 PNL_SUMMARY）")

    if not candidates:
        print("[回填工具] 没有需要回填的组。")
        return 0

    # --- Step 3: income 归属裁决 ---
    income_path = args.income
    if not income_path.exists():
        print(
            f"[回填工具] 警告：income JSON 不存在 {income_path}，跳过真值校验，"
            f"直接用明细 SUM 作为金额来源（可能含开仓错行 pnl）",
            file=sys.stderr,
        )
        to_fill = candidates
        undecided: list[BackfillGroup] = []
    else:
        print(f"[回填工具] Step 3: 调 reconcile.match_income_to_trades 跑归属裁决 ...")
        try:
            to_fill, undecided = run_income_attribution(
                candidates, income_path, csv_path,
            )
        except ImportError as e:
            print(
                f"[回填工具] 警告：reconcile 模块不可导入 ({e})，"
                f"退化为明细 SUM（可能含开仓错行）",
                file=sys.stderr,
            )
            to_fill = candidates
            undecided = []

    # --- Step 4: 输出 ---
    today_str = datetime.now().strftime("%Y%m%d")

    if undecided:
        print(f"\n{'='*60}")
        print(f"[回填工具] 未决清单（{len(undecided)} 个，宁缺勿错）")
        print(f"{'='*60}")
        for idx, g in enumerate(undecided, start=1):
            print(f"\n未决 #{idx} {g.strategy} {g.symbol} {g.side}")
            print(f"  原因：{g.undecided_reason}")
            print(f"  明细行数：{len(g.rows)}")
            print(f"  明细SUM={g.pnl_from_trade_records} USDT（有开仓错行嫌疑，不回填）")

    print(f"\n{'='*60}")
    print(f"[回填工具] 待回填清单（{len(to_fill)} 个，锚 income 真值）")
    print(f"{'='*60}")

    for idx, g in enumerate(to_fill, start=1):
        batch_id = f"backfill-{today_str}-{idx:02d}"
        print(f"\n--- #{idx} {g.strategy} {g.symbol} {g.side} ---")
        print(f"  {g.source_note}")
        print(f"  income笔数：{len(g.income_truths)}")
        print(f"  末笔平仓时间：{g.last_executed_at}")
        print(f"  含 STOP_LOSS：{'是' if g.has_stop_loss else '否'}")
        print(f"  建议 INSERT（batch_id={batch_id}，幂等：order_id={batch_id} + 已有汇总跳过）：")
        print(f"  {g.build_sql(batch_id)}")

    # 汇总
    total_from_income = sum((g.pnl_from_income for g in to_fill), Decimal("0"))
    total_from_trade = sum((g.pnl_from_trade_records for g in to_fill), Decimal("0"))
    print(f"\n{'='*60}")
    print(f"[回填工具] 汇总")
    print(f"{'='*60}")
    print(f"  候选组总数：{len(candidates)}")
    print(f"  待回填：{len(to_fill)} 组，income真值合计 {total_from_income} USDT")
    print(f"  未决：{len(undecided)} 组")
    if abs(total_from_income - total_from_trade) > Decimal("0.01"):
        print(f"  明细SUM合计（含开仓错行）：{total_from_trade} USDT，差额 {abs(total_from_income - total_from_trade)} USDT")
        print(f"  → 明细 SUM 含开仓 LIMIT/CONDITIONAL_ORDER 错行 pnl，income 真值已自动过滤")
    print(f"  幂等说明：重跑时 ON CONFLICT DO NOTHING 跳过已回填批次 + 已存在 PNL_SUMMARY 的组自动跳过")

    if args.execute:
        print(
            "\n[回填工具] --execute 留接口，dry-run 模式输出 SQL 需手动执行。\n"
            "  建议：\n"
            "  1. 选 1-2 个组的 SQL 在 staging 环境验证金额正确\n"
            "  2. 全部 SQL 一次性执行（batch_id 天然幂等）\n"
            "  3. 验证：SELECT strategy, symbol, side, SUM(realized_pnl) "
            "FROM trading.trade_records WHERE order_type='PNL_SUMMARY' "
            "AND order_id LIKE 'backfill-%' GROUP BY 1,2,3;",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
