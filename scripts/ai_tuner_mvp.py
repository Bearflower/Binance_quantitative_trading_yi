#!/usr/bin/env python3
"""
AI Tuner MVP：准备度审计 + K 线覆盖审计 CLI

用法：
  python3 scripts/ai_tuner_mvp.py audit \
      --as-of 2026-10-17T00:00:00Z --output var/readiness.json
  python3 scripts/ai_tuner_mvp.py kline-audit \
      --strategy btc_eth_aggressive --as-of 2026-10-21T00:00:00Z \
      [--output /tmp/kline-audit.json]

退出码：
  audit：
    0  = 选出一个候选策略（SELECT）
    2  = 所有候选都不合格（STOP），报告里列失败原因
    1  = 运行时错误
  kline-audit：
    0  = 成功（写报告或打印摘要）
    1  = 运行时错误（配置缺失 / 数据库连接失败 / 运行异常）

输出：UTF-8 JSON 报告（原子写入 .tmp + os.replace）
"""

import argparse
import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# K 线采集脚本路径（只读铁律：只在 recommended_commands 里作为文本建议出现，
# 永不启动外部进程、永不 import 该脚本）
KLINE_FETCH_SCRIPT = "services/kline_service/scripts/fetch_history_bulk.py"

# 关键：以文件方式运行脚本时（python3 scripts/ai_tuner_mvp.py），
# sys.path[0] = scripts/ 目录，仓库根不在搜索路径 → shared/ai_tuner 导入失败。
# pytest 能过是因为 pytest 自动把 rootdir 加进 sys.path。
# 这里显式注入仓库根到 sys.path[0]，确保 CLI 能直接执行。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_resolved_config() -> dict:
    """加载 ai_tuner/config.yaml 并解析 ${VAR} 环境变量占位符"""
    config_path = PROJECT_ROOT / "ai_tuner" / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return _resolve_env(raw)


def _resolve_env(obj):
    """递归替换字典/字符串中的 ${VAR} 和 ${VAR:default}"""
    if isinstance(obj, str):
        pattern = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")

        def repl(match):
            name = match.group(1).strip()
            default = match.group(2)
            value = os.getenv(name, default if default is not None else "")
            # 全部是数字时自动转 int
            if value and value.lstrip("-").isdigit():
                return value  # 保持字符串，由 yaml 解析时处理
            return value

        return pattern.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env(item) for item in obj]
    return obj


# ===========================================================================
# 辅助函数
# ===========================================================================


def _parse_as_of(raw: str) -> datetime:
    """解析 --as-of 时间字符串（ISO 8601，支持 Z 后缀）

    naive 自动补 UTC；格式非法抛 ValueError（由调用方转 exit 1）。
    供 audit / kline-audit 两个子命令共用，避免复制解析逻辑。
    """
    normalized = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_utc_z(dt: datetime) -> str:
    """datetime → UTC ISO 8601 Z 结尾字符串（JSON 安全）

    naive 视为 UTC；aware 先转 UTC 再格式化。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic(report: dict, output_path: Path) -> None:
    """原子写入 JSON：mkdir 父目录 → 写 .tmp → os.replace

    audit / kline-audit 共用，避免重复 12 行样板。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, output_path)


@asynccontextmanager
async def _open_db(db_cfg: dict):
    """打开数据库连接的上下文管理器（复用于 run_audit / run_kline_audit）

    Args:
        db_cfg: 已展开 ${VAR} 的 database 段字典（调用方各自 load_config 后传入，
                避免本函数二次 load_resolved_config + 保持幽灵变量消失）。

    Yields:
        db — 连接成功的 DatabaseManager；连接失败则 yield None（已打印 stderr）。
        disconnect 在 finally 自动调用。
    """
    from shared.database import DatabaseManager

    db = DatabaseManager(
        host=db_cfg.get("host", "localhost"),
        port=int(db_cfg.get("port", 5432)),
        database=db_cfg.get("database", ""),
        user=db_cfg.get("user", ""),
        password=db_cfg.get("password", ""),
    )
    try:
        await db.connect()
    except Exception as exc:
        print(f"数据库连接失败: {exc}", file=sys.stderr)
        yield None
        return
    try:
        yield db
    finally:
        try:
            await db.disconnect()
        except Exception:
            pass


def _build_kline_report(
    strategy_id: str,
    as_of: datetime,
    window_days: int,
    window_start: datetime,
    result,
) -> dict:
    """把 KlineCoverageResult 组装成 kline-audit 报告字典

    负责：展平 missing_ranges → 生成 recommended_commands → 组装固定六键 + gaps。
    纯函数（不做 IO、不做 JSON dump、不读配置）。
    """
    from ai_tuner.readiness.kline_coverage import symbol_row_to_dict

    missing_ranges: list[dict] = []
    recommended_commands: list[str] = []
    for sym in result.symbols:
        for (s, e) in sym.missing_ranges:
            s_iso = _to_utc_z(s)
            e_iso = _to_utc_z(e)
            missing_ranges.append({
                "symbol": sym.symbol,
                "interval": sym.interval,
                "start": s_iso,
                "end": e_iso,
            })
            recommended_commands.append(
                f"python3 {KLINE_FETCH_SCRIPT} "
                f"--symbols {sym.symbol} --intervals {sym.interval} "
                f"--start {s_iso} --end {e_iso}"
            )

    return {
        "strategy_id": strategy_id,
        "window": {
            "start": _to_utc_z(window_start),
            "end": _to_utc_z(as_of),
            "days": window_days,
        },
        "weighted_coverage": result.weighted_coverage,
        "symbols": [symbol_row_to_dict(s) for s in result.symbols],
        "missing_ranges": missing_ranges,
        "recommended_commands": recommended_commands,
        "gaps": list(result.gaps),
    }


# ===========================================================================
# 对外入口：run_audit / run_kline_audit
# ===========================================================================


async def run_audit(as_of: datetime, output_path: Path) -> int:
    """执行审计并写报告；返回退出码

    对外签名保持不变（已有 10 个测试依赖 monkeypatch DatabaseManager / ReadinessAuditor）。
    """
    # 延迟导入（CLI 不需要常驻的重依赖）
    from ai_tuner.readiness.auditor import ReadinessAuditor, format_ratio

    config = load_resolved_config()

    async with _open_db(config.get("database", {})) as db:
        if db is None:
            return 1
        auditor = ReadinessAuditor(db, config)
        result = await auditor.run(as_of=as_of)

    _write_json_atomic(result.to_dict(), output_path)

    # --- 摘要输出 ---
    print(f"\n=== AI Tuner MVP 准备度审计 ===")
    print(f"  as_of:        {result.as_of}")
    print(f"  window_days:  {result.window_days}")
    print(f"  decision:     {result.decision}")
    print(f"  selected:     {result.selected_strategy or '(none)'}")
    print(f"\n候选详情：")
    for sid, cr in result.candidates.items():
        elig = "✅ 合格" if cr.eligible else "❌ 不合格"
        print(f"  [{sid}] {elig}")
        print(f"    trades={cr.closed_trades} signals={cr.effective_signals} "
              f"regimes={cr.regime_count}")
        print(f"    complete={format_ratio(cr.complete_trade_ratio)} "
              f"identity={format_ratio(cr.config_identity_ratio)} "
              f"kline={format_ratio(cr.kline_coverage)} "
              f"snapshot={format_ratio(cr.external_snapshot_coverage)}")
        print(f"    快照门禁: requirement={cr.external_snapshot_requirement} "
              f"reason={cr.external_snapshot_reason or '(none)'}")
        if cr.failures:
            print(f"    失败门禁：")
            for fail in cr.failures:
                print(f"      - {fail}")
        if cr.gaps:
            print(f"    数据缺口：")
            for gap in cr.gaps:
                print(f"      ! {gap}")
    print(f"\n报告已写入: {output_path}")

    return 0 if result.decision == "SELECT" else 2


async def run_kline_audit(
    strategy_id: str,
    as_of: datetime,
    output_path: Optional[Path],
) -> int:
    """只读 K 线覆盖审计：输出 JSON 报告

    只允许 SELECT / fetch_*；recommended_commands 是文本建议，
    永不启动外部进程、永不 import 采集脚本。
    """
    from datetime import timedelta
    from ai_tuner.readiness.kline_coverage import compute_kline_coverage

    config = load_resolved_config()
    readiness_cfg = config.get("purpose_driven_mvp", {}).get("readiness", {})
    try:
        window_days = int(readiness_cfg["window_days"])
    except (KeyError, TypeError, ValueError) as exc:
        print(f"配置缺少 readiness.window_days: {exc}", file=sys.stderr)
        return 1
    universe_cfg = readiness_cfg.get("universe")
    window_start = as_of - timedelta(days=window_days)

    async with _open_db(config.get("database", {})) as db:
        if db is None:
            return 1
        try:
            result = await compute_kline_coverage(
                db, strategy_id=strategy_id, universe_cfg=universe_cfg,
                project_root=PROJECT_ROOT, window_start=window_start, window_end=as_of,
            )
        except Exception as exc:
            print(f"kline-audit 运行异常: {exc}", file=sys.stderr)
            return 1

    report = _build_kline_report(
        strategy_id=strategy_id, as_of=as_of, window_days=window_days,
        window_start=window_start, result=result,
    )
    if output_path is not None:
        _write_json_atomic(report, output_path)
        print(f"kline-audit 报告已写入: {output_path}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AI Tuner MVP：准备度审计 + 只读 K 线覆盖审计"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="执行准备度审计")
    audit.add_argument("--as-of", required=True,
                       help="审计截止时间（ISO 8601 UTC），如 2026-10-17T00:00:00Z")
    audit.add_argument("--output", required=True,
                       help="JSON 报告输出路径，如 var/readiness.json")

    kline_audit = sub.add_parser("kline-audit", help="只读 K 线覆盖审计")
    kline_audit.add_argument("--strategy", required=True,
                             help="候选策略 ID（如 btc_eth_aggressive / hrs）")
    kline_audit.add_argument("--as-of", required=True,
                             help="审计截止时间（ISO 8601 UTC），如 2026-10-21T00:00:00Z")
    kline_audit.add_argument("--output", default=None,
                             help="JSON 报告输出路径；不传则只打印摘要到 stdout")

    args = parser.parse_args()

    # --- 解析 --as-of（供 audit / kline-audit 共用）---
    try:
        as_of = _parse_as_of(args.as_of)
    except ValueError as exc:
        print(f"--as-of 格式错误: {exc}", file=sys.stderr)
        return 1

    if args.command == "audit":
        output_path = Path(args.output)
        return asyncio.run(run_audit(as_of, output_path))

    if args.command == "kline-audit":
        output_path = Path(args.output) if args.output else None
        return asyncio.run(run_kline_audit(args.strategy, as_of, output_path))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
