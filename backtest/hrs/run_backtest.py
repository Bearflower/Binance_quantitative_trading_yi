#!/usr/bin/env python3
"""
HRS 策略 - 多交易对批量回测入口
支持多交易对、自定义天数、缓存控制、自动生成汇总报告。
支持全市场扫描模式（--scan）。

用法：
  python3 backtest/hrs/run_backtest.py --symbols LABUSDT,SAHARAUSDT --days 30
  python3 backtest/hrs/run_backtest.py --symbols LABUSDT --days 7 --no-cache
  python3 backtest/hrs/run_backtest.py --scan --days 30
"""
import sys
import os
import argparse
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

# 添加项目根目录
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

# 复用已有模块
from backtest.hrs.download_klines import download_klines
from backtest.hrs.quick_backtest import analyze_symbol
from backtest.hrs.cache_manager import (
    is_cache_valid,
    get_cache_meta,
    save_cache_meta,
    clean_old_cache,
    get_cache_size_mb,
)
from backtest.hrs.report_generator import generate_report, generate_market_report
from backtest.hrs.market_scanner import scan_market, get_candidate_symbols


def download_and_cache(symbol: str, days: int) -> bool:
    """
    下载K线数据并保存缓存元数据

    Args:
        symbol: 交易对
        days: 下载天数

    Returns:
        是否下载成功
    """
    print(f"\n📥 下载 {symbol} K线数据...")
    df = download_klines(symbol=symbol, days=days)
    if df is None or df.empty:
        print(f"❌ {symbol} 数据下载失败")
        return False

    # 保存元数据
    meta = {
        "symbol": symbol,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "rows": len(df),
        "start_time": str(df["open_time"].iloc[0]),
        "end_time": str(df["open_time"].iloc[-1]),
    }
    save_cache_meta(symbol, meta)
    print(f"✅ {symbol} 缓存元数据已保存")
    return True


def run_single_symbol(symbol: str, days: int, force_download: bool) -> Optional[Dict]:
    """
    对单个交易对执行完整回测流程

    Args:
        symbol: 交易对
        days: 数据天数
        force_download: 是否强制重新下载

    Returns:
        分析结果字典，失败返回 None
    """
    print(f"\n{'=' * 70}")
    print(f"  处理交易对: {symbol}")
    print(f"{'=' * 70}")

    # 1. 检查缓存
    cache_valid = False
    if not force_download:
        cache_valid = is_cache_valid(symbol, max_age_hours=24)
        if cache_valid:
            meta = get_cache_meta(symbol)
            if meta:
                print(f"✅ 缓存有效: {meta.get('rows', 0)} 行, "
                      f"下载时间: {meta.get('downloaded_at', '?')}")
        else:
            print(f"⚠️ 缓存无效或过期，需要重新下载")

    # 2. 下载数据（如需要）
    if force_download or not cache_valid:
        if not download_and_cache(symbol, days):
            return None

    # 3. 运行回测分析
    try:
        result = analyze_symbol(symbol)
        return result
    except Exception as e:
        print(f"❌ {symbol} 分析失败: {e}")
        traceback.print_exc()
        return None


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="HRS策略多交易对批量回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 backtest/hrs/run_backtest.py --symbols LABUSDT,SAHARAUSDT --days 30
  python3 backtest/hrs/run_backtest.py --symbols LABUSDT --days 7 --no-cache
  python3 backtest/hrs/run_backtest.py --scan --days 30
        """,
    )
    parser.add_argument(
        "--symbols",
        default="LABUSDT,SAHARAUSDT",
        help="交易对列表，逗号分隔（默认: LABUSDT,SAHARAUSDT）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="下载K线数据天数（默认: 30）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="强制重新下载，忽略缓存",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="启用全市场扫描模式：先扫描全市场筛选候选币种，再批量回测",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  HRS 策略 - 批量回测")
    print("=" * 70)
    print(f"  数据天数: {args.days}")
    print(f"  缓存策略: {'强制重新下载' if args.no_cache else '优先使用缓存(24h有效)'}")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 缓存大小检查和清理
    cache_size = get_cache_size_mb()
    print(f"📦 当前缓存大小: {cache_size:.1f} MB")
    cleaned = clean_old_cache(max_age_days=7)
    if cleaned > 0:
        print(f"🧹 已清理 {cleaned} 个过期缓存")

    # ============================================================
    # 全市场扫描模式
    # ============================================================
    if args.scan:
        _run_scan_mode(args)
        return

    # ============================================================
    # 普通批量回测模式
    # ============================================================
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("❌ 未指定有效交易对")
        sys.exit(1)

    print(f"  交易对: {', '.join(symbols)}")
    print()

    results = _run_batch_analysis(symbols, args.days, args.no_cache)

    # 生成报告
    if results:
        print("\n📝 生成回测报告...")
        try:
            report_path = generate_report(results)
            print(f"✅ 报告已生成: {report_path}")
        except Exception as e:
            print(f"❌ 报告生成失败: {e}")
            traceback.print_exc()
    else:
        print("\n⚠️ 无有效回测结果，跳过报告生成")

    print(f"\n🏁 批量回测完成，结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def _run_scan_mode(args):
    """
    全市场扫描模式：先扫描全市场筛选候选币种，再批量回测

    Args:
        args: 命令行参数
    """
    # 阶段1：全市场扫描
    print("\n" + "=" * 70)
    print("  阶段1：全市场快速扫描")
    print("=" * 70)

    try:
        scan_result = scan_market()
    except Exception as e:
        print(f"❌ 全市场扫描失败: {e}")
        traceback.print_exc()
        sys.exit(1)

    candidate_symbols = get_candidate_symbols(scan_result)
    if not candidate_symbols:
        print("\n⚠️ 未发现任何候选币种，扫描结束")
        # 仍然生成空报告
        _generate_market_report(scan_result, scan_result.get("short_candidates", []),
                                scan_result.get("long_candidates", []), [])
        return

    print(f"\n📋 候选币种共 {len(candidate_symbols)} 个: {', '.join(candidate_symbols)}")

    # 阶段2：批量回测
    print("\n" + "=" * 70)
    print("  阶段2：批量深度回测")
    print("=" * 70)

    results = _run_batch_analysis(candidate_symbols, args.days, args.no_cache)

    # 阶段3：生成全市场报告
    _generate_market_report(
        scan_result,
        scan_result.get("short_candidates", []),
        scan_result.get("long_candidates", []),
        results,
    )

    print(f"\n🏁 全市场扫描+批量回测完成，结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def _run_batch_analysis(symbols: List[str], days: int, force_download: bool) -> List[Dict]:
    """
    对交易对列表执行批量分析

    Args:
        symbols: 交易对列表
        days: 数据天数
        force_download: 是否强制重新下载

    Returns:
        分析结果列表
    """
    results = []
    failed_symbols = []
    total = len(symbols)

    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{total}] 处理 {symbol}...")
        result = run_single_symbol(symbol, days, force_download)
        if result is not None:
            results.append(result)
        else:
            failed_symbols.append(symbol)

    # 汇总
    print("\n" + "=" * 70)
    print("  批量回测汇总")
    print("=" * 70)
    print(f"  成功: {len(results)}/{total}")
    if failed_symbols:
        print(f"  失败: {', '.join(failed_symbols)}")
    return results


def _generate_market_report(
    scan_summary: Dict,
    short_candidates: List[Dict],
    long_candidates: List[Dict],
    analysis_results: List[Dict],
):
    """生成全市场回测报告"""
    print("\n📝 生成全市场回测报告...")
    try:
        report_path = generate_market_report(
            scan_summary=scan_summary,
            short_candidates=short_candidates,
            long_candidates=long_candidates,
            analysis_results=analysis_results,
        )
        print(f"✅ 全市场报告已生成: {report_path}")
    except Exception as e:
        print(f"❌ 全市场报告生成失败: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()