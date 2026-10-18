#!/usr/bin/env python3
"""
AI Tuner MVP 阶段一：90 天准备度审计 CLI

用法：
  python3 scripts/ai_tuner_mvp.py audit --as-of 2026-10-17T00:00:00Z --output var/readiness.json

退出码：
  0  = 选出一个候选策略（SELECT）
  2  = 所有候选都不合格（STOP），报告里列失败原因
  1  = 运行时错误

输出：按 as_of 排序的 UTF-8 JSON 证据报告（原子写入）
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]

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


async def run_audit(as_of: datetime, output_path: Path) -> int:
    """执行审计并写报告；返回退出码"""
    # 延迟导入（CLI 不需要常驻的重依赖）
    from shared.database import DatabaseManager
    from ai_tuner.readiness.auditor import ReadinessAuditor

    config = load_resolved_config()

    db_cfg = config.get("database", {})
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
        return 1

    try:
        auditor = ReadinessAuditor(db, config)
        result = await auditor.run(as_of=as_of)
    finally:
        try:
            await db.disconnect()
        except Exception:
            pass

    # --- 原子写入 JSON（先写 .tmp 再 rename）---
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, output_path)

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
        print(f"    complete={cr.complete_trade_ratio:.1%} "
              f"identity={cr.config_identity_ratio:.1%} "
              f"kline={cr.kline_coverage:.1%} "
              f"snapshot={cr.external_snapshot_coverage:.1%}")
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AI Tuner MVP 阶段一：90 天准备度审计"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="执行准备度审计")
    audit.add_argument("--as-of", required=True,
                       help="审计截止时间（ISO 8601 UTC），如 2026-10-17T00:00:00Z")
    audit.add_argument("--output", required=True,
                       help="JSON 报告输出路径，如 var/readiness.json")

    args = parser.parse_args()

    if args.command == "audit":
        # 解析 --as-of 时间（支持 Z 后缀）
        raw = args.as_of.replace("Z", "+00:00")
        try:
            as_of = datetime.fromisoformat(raw)
        except ValueError as exc:
            print(f"--as-of 格式错误: {exc}", file=sys.stderr)
            return 1
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

        output_path = Path(args.output)
        return asyncio.run(run_audit(as_of, output_path))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
