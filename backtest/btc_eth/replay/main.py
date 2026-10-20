"""
影子回放器 CLI 入口。

调用示例：
    python -m backtest.btc_eth.replay.main \
        --events research_events.jsonl \
        --run-manifest run_manifest.json \
        --snapshot-dir ./snapshot/ \
        --profile-orig strategies/btc_eth/shadow/config/xau_original.yaml \
        --profile-aggr strategies/btc_eth/shadow/config/xau_aggressive.yaml \
        --output-dir ./reports/

安全：默认不连生产 PostgreSQL；DATABASE_URL 缺省时直接报错。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .engine import ReplayEngine
from .report import write_reports
from .snapshot import verify_snapshot


logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="XAUUSDT MTCPS 影子回放器（本地离线）")
    parser.add_argument("--events", required=True, help="决策事件 JSON Lines 文件（来自 shadow 导出）")
    parser.add_argument("--run-manifest", required=True, help="run manifest JSON 文件")
    parser.add_argument("--snapshot-dir", required=True,
                        help="快照目录（含 1m K 线 / funding / exchangeInfo / manifest.json）")
    parser.add_argument("--profile-orig", required=True, help="原版 profile 冻结配置 (xau_original.yaml)")
    parser.add_argument("--profile-aggr", required=True, help="激进版 profile 冻结配置 (xau_aggressive.yaml)")
    parser.add_argument("--replay-config", default=None, help="回放 config.yaml；缺省用 replay/config.yaml")
    parser.add_argument("--output-dir", required=True, help="报告输出目录")
    parser.add_argument("--skip-snapshot-verify", action="store_true", help="跳过 manifest 文件 SHA-256 校验")
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help="允许快照存在数据缺口时继续回放（默认 False；"
             "缺口时 fail-closed 并拒绝产出准入结论）",
    )
    parser.add_argument("--no-db", action="store_true", help="显式声明不连 DB（默认如此；仅为防御）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 安全守卫：禁止连生产 PostgreSQL
    # 回放器只读本地 JSON / JSONL 文件，严禁回退连生产库
    if os.environ.get("DATABASE_URL"):
        logger.warning(
            "检测到 DATABASE_URL 环境变量=%s 会被忽略（回放器离线运行，不连 DB）",
            os.environ.get("DATABASE_URL", "")[:30] + "...",
        )

    # 默认回放配置
    default_replay_cfg = str(Path(__file__).resolve().parent / "config.yaml")
    replay_cfg = args.replay_config or default_replay_cfg

    snapshot_dir = Path(args.snapshot_dir)
    manifest_path = snapshot_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 快照文件路径解析：Binance USDⓈ-M TradFi 合约符号含斜杠变体；snapshot 里存的是原始符号
    symbol = manifest["symbol"]
    # snapshot.py 里落盘用 symbol.lower() + "_1m.jsonl" 等
    klines_path = snapshot_dir / f"{symbol.lower()}_1m.jsonl"
    funding_path = snapshot_dir / f"{symbol.lower()}_funding.json"
    exchange_info_path = snapshot_dir / f"{symbol.lower()}_exchange_info.json"

    # data_completeness：记录快照数据质量，传递给 write_reports 决定 admission 契约
    # 默认 complete=True；仅当 verify 跑完后覆盖。
    data_completeness = {"gaps": 0, "complete": True}

    if not args.skip_snapshot_verify:
        verify = verify_snapshot(str(manifest_path))
        if not verify["files_ok"]:
            logger.error("快照校验失败：%s", verify["mismatches"])
            return 2

        gaps = verify["gaps"]
        data_completeness = {"gaps": gaps, "complete": gaps == 0}

        if gaps > 0:
            if not args.allow_gaps:
                # —— fail-closed：数据不完整且未显式允许 → 拒绝产出准入结论 ——
                # 退出码 3（区别于其他错误码 2/4），stdout/stderr 都打印结构化错误信息，
                # 方便 CI 脚本判定"准入被拒绝"而非运行时崩溃。
                err_msg = (
                    f"快照数据不完整（gaps={gaps}），"
                    f"未显式传 --allow-gaps；按设计要求 fail-closed。"
                )
                logger.error(err_msg)
                # stdout 也打印结构化 JSON（与正常成功路径同格式，便于调用方解析）
                print(json.dumps({
                    "admission": "insufficient_data",
                    "data_completeness": data_completeness,
                    "error": err_msg,
                }, indent=2, ensure_ascii=False))
                return 3
            else:
                logger.warning(
                    "快照存在 %d 处缺口（已显式传 --allow-gaps，继续回放；"
                    "报告将标注数据不完整）",
                    gaps,
                )

    with open(args.run_manifest, "r", encoding="utf-8") as f:
        run_manifest = json.load(f)

    engine = ReplayEngine(
        replay_config_path=replay_cfg,
        profile_config_path_original=args.profile_orig,
        profile_config_path_aggressive=args.profile_aggr,
        klines_1m_path=str(klines_path),
        funding_rates_path=str(funding_path),
        exchange_info_json_path=str(exchange_info_path),
        snapshot_manifest_path=str(manifest_path),
        run_manifest=run_manifest,
    )
    engine.load_events(args.events)
    engine_result = engine.run()

    outputs = write_reports(
        engine_result=engine_result,
        run_manifest=run_manifest,
        output_dir=args.output_dir,
        replay_config_path=replay_cfg,
        data_completeness=data_completeness,
    )

    logger.info("回放完成：admission=%s data_completeness=%s",
                outputs["admission"], data_completeness)
    print(json.dumps({
        "admission": outputs["admission"],
        "data_completeness": data_completeness,
        "md_report": outputs["md_path"],
        "json_report": outputs["json_path"],
        "replay_run_id": engine_result["engine_metadata"]["replay_run_id"],
        "invariant_violations": len(engine_result["engine_metadata"]["invariant_violations"]),
    }, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
