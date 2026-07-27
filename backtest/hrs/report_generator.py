#!/usr/bin/env python3
"""
HRS 回测报告生成器
汇总多个交易对的回测结果，生成 Markdown 格式的综合报告。
支持单批次回测报告和全市场扫描+批量回测报告。
"""
import os
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

import yaml


def generate_report(results: List[Dict], output_dir: Optional[str] = None) -> str:
    """
    生成回测报告

    Args:
        results: 回测结果列表，每个元素为 analyze_symbol 返回的字典（有效结果）
        output_dir: 输出目录，默认使用 backtest/hrs/reports/

    Returns:
        报告文件路径
    """
    if output_dir is None:
        output_dir = str(Path(__file__).parent / "reports")

    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(output_dir, f"hrs_backtest_{timestamp}.md")

    lines = _build_report(results)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return report_path


def _build_report(results: List[Dict]) -> List[str]:
    """构建报告内容"""
    lines = []

    # 标题
    lines.append("# HRS 策略回测报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    lines.append(f"**交易对数量**: {len(results)}")
    lines.append("")

    if not results:
        lines.append("> 无有效回测结果")
        return lines

    # ========== 1. 概览表格 ==========
    lines.append("## 一、概览表格")
    lines.append("")
    lines.append(
        "| 交易对 | 最新价格 | OI (USDT) | 年化费率 | 24h成交额 | "
        "OI/成交额 | EMA20偏离 | 做空候选 | 做多候选 |"
    )
    lines.append(
        "|--------|----------|-----------|----------|-----------|"
        "----------|-----------|----------|----------|"
    )

    for r in results:
        symbol = r.get("symbol", "?")
        price = _fmt_price(r.get("current_price", 0))
        oi = _fmt_volume(r.get("oi_usd", 0))
        funding = _fmt_pct(r.get("annual_funding", 0))
        volume = _fmt_volume(r.get("volume_24h", 0))
        oi_ratio = _fmt_decimal(r.get("oi_market_cap_ratio", 0))
        deviation = _fmt_pct(r.get("deviation_4h", 0))
        short_candidate = "是" if r.get("candidate_short") else "否"
        long_candidate = "是" if r.get("candidate_long") else "否"

        lines.append(
            f"| {symbol} | {price} | {oi} | {funding} | {volume} | "
            f"{oi_ratio} | {deviation} | {short_candidate} | {long_candidate} |"
        )

    lines.append("")

    # ========== 2. 做空方向汇总 ==========
    lines.append("## 二、做空方向汇总")
    lines.append("")

    for r in results:
        symbol = r.get("symbol", "?")
        lines.append(f"### {symbol}")
        lines.append("")

        short_candidate = r.get("candidate_short", False)
        lines.append(f"- **候选池**: {'通过' if short_candidate else '未通过'}")

        if short_candidate:
            _append_patterns(lines, r.get("short_pattern_result", {}), "short")
            _append_score_section(lines, r.get("short_score_result"))
            _append_entry(lines, r.get("short_should_enter", False), "short")
        lines.append("")

    # ========== 3. 做多方向汇总 ==========
    lines.append("## 三、做多方向汇总")
    lines.append("")

    for r in results:
        symbol = r.get("symbol", "?")
        lines.append(f"### {symbol}")
        lines.append("")

        long_candidate = r.get("candidate_long", False)
        lines.append(f"- **候选池**: {'通过' if long_candidate else '未通过'}")

        if long_candidate:
            _append_patterns(lines, r.get("long_pattern_result", {}), "long")
            _append_score_section(lines, r.get("long_score_result"))
            _append_entry(lines, r.get("long_should_enter", False), "long")
        lines.append("")

    # ========== 4. 推荐列表 ==========
    lines.append("## 四、按评分排序的推荐列表")
    lines.append("")

    scored_results = _collect_scored_results(results)

    if scored_results:
        lines.append(
            "| 排名 | 交易对 | 方向 | 总分 | 合约分 | 技术分 | 情绪分 | 建议入场 | 候选池 |"
        )
        lines.append(
            "|------|--------|------|------|--------|--------|--------|----------|--------|"
        )

        for i, s in enumerate(scored_results, 1):
            enter = "是" if s["should_enter"] else "否"
            candidate = "通过" if s["candidate"] else "未通过"
            lines.append(
                f"| {i} | {s['symbol']} | {s['direction']} | "
                f"{s['total_score']:.2f} | {s['contract_score']:.2f} | "
                f"{s['technical_score']:.2f} | {s['sentiment_score']:.2f} | "
                f"{enter} | {candidate} |"
            )
    else:
        lines.append("> 无有效评分结果")

    lines.append("")
    lines.append("---")
    lines.append("*报告由 HRS 回测系统自动生成*")

    return lines


def _collect_scored_results(results: List[Dict]) -> List[Dict]:
    """收集所有评分结果并按总分降序排序"""
    scored = []
    for r in results:
        symbol = r.get("symbol", "?")

        # 做空
        short_score = r.get("short_score_result")
        if short_score and isinstance(short_score, dict):
            total = short_score.get("total_score", 0)
            if total > 0:
                scored.append({
                    "symbol": symbol,
                    "direction": "做空",
                    "total_score": total,
                    "contract_score": short_score.get("contract_score", 0),
                    "technical_score": short_score.get("technical_score", 0),
                    "sentiment_score": short_score.get("sentiment_score", 0),
                    "should_enter": r.get("short_should_enter", False),
                    "candidate": r.get("candidate_short", False),
                })

        # 做多
        long_score = r.get("long_score_result")
        if long_score and isinstance(long_score, dict):
            total = long_score.get("total_score", 0)
            if total > 0:
                scored.append({
                    "symbol": symbol,
                    "direction": "做多",
                    "total_score": total,
                    "contract_score": long_score.get("contract_score", 0),
                    "technical_score": long_score.get("technical_score", 0),
                    "sentiment_score": long_score.get("sentiment_score", 0),
                    "should_enter": r.get("long_should_enter", False),
                    "candidate": r.get("candidate_long", False),
                })

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    return scored


def _append_patterns(lines: List[str], patterns: Dict, direction: str):
    """追加形态检测结果到报告行"""
    if direction == "short":
        pattern_names = [
            ("三次冲顶", "three_tops"),
            ("长上影线", "long_upper_shadow"),
            ("放量滞涨", "volume_stagnation"),
        ]
    else:
        pattern_names = [
            ("三次探底", "three_bottoms"),
            ("长下影线", "long_lower_shadow"),
            ("放量止跌", "volume_reversal"),
        ]

    lines.append("- **形态检测**:")
    for name, key in pattern_names:
        detected, score_val = patterns.get(key, (False, 0))
        status = "检测到" if detected else "未检测"
        lines.append(f"  - {name}: {status} (得分: {score_val})")


def _append_score_section(lines: List[str], score_result: Optional[Dict]):
    """追加评分结果到报告行"""
    if score_result is None:
        lines.append("- **评分**: 失败")
        return

    total = score_result.get("total_score", 0)
    contract = score_result.get("contract_score", 0)
    technical = score_result.get("technical_score", 0)
    sentiment = score_result.get("sentiment_score", 0)
    veto = score_result.get("veto", False)
    veto_reason = score_result.get("veto_reason")

    lines.append(f"- **综合评分**: {total:.2f}")
    lines.append(f"  - 合约数据分: {contract:.2f}")
    lines.append(f"  - 技术面分: {technical:.2f}")
    lines.append(f"  - 情绪面分: {sentiment:.2f}")

    if veto:
        lines.append(f"  - **否决**: {veto_reason}")


def _append_entry(lines: List[str], should_enter: bool, direction: str):
    """追加入场判断到报告行"""
    direction_label = "做空" if direction == "short" else "做多"
    status = "建议入场" if should_enter else "不建议入场"
    lines.append(f"- **{direction_label}入场**: {status}")


def _fmt_price(value: float) -> str:
    """格式化价格（根据量级自适应精度）"""
    if value == 0:
        return "-"
    if value < 0.01:
        return f"{value:.8f}"
    if value < 1:
        return f"{value:.6f}"
    return f"{value:.4f}"


def _fmt_volume(value: float) -> str:
    """格式化成交量为可读格式"""
    if value == 0:
        return "-"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def _fmt_pct(value: float) -> str:
    """格式化百分比"""
    return f"{value:+.2f}%"


def _fmt_decimal(value: float) -> str:
    """格式化小数"""
    return f"{value:.4f}"


# ============================================================
# 全市场回测报告
# ============================================================

def generate_market_report(
    scan_summary: Dict,
    short_candidates: List[Dict],
    long_candidates: List[Dict],
    analysis_results: List[Dict],
    output_dir: Optional[str] = None,
) -> str:
    """
    生成全市场扫描 + 批量回测的综合报告

    Args:
        scan_summary: 扫描概要信息，包含 market_stats 等
        short_candidates: 做空方向的扫描候选列表
        long_candidates: 做多方向的扫描候选列表
        analysis_results: 每个币种的完整分析结果（由 analyze_symbol 返回的字典列表）
        output_dir: 输出目录，默认使用 backtest/hrs/reports/

    Returns:
        报告文件路径
    """
    if output_dir is None:
        output_dir = str(Path(__file__).parent / "reports")

    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(output_dir, f"hrs_market_scan_{timestamp}.md")

    lines = _build_market_report(scan_summary, short_candidates, long_candidates, analysis_results)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return report_path


def _build_market_report(
    scan_summary: Dict,
    short_candidates: List[Dict],
    long_candidates: List[Dict],
    analysis_results: List[Dict],
) -> List[str]:
    """构建全市场回测报告内容"""
    lines = []

    # 动态加载配置，避免硬编码筛选条件
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config_path = os.path.join(project_root, "strategies", "hrs", "config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception:
        config = {}

    pool_config = config.get("candidate_pool", {})
    short_config = pool_config.get("short", {})
    long_config = pool_config.get("long", {})
    liquidity_config = pool_config.get("liquidity", {})

    # 从配置动态读取筛选条件
    short_min_oi = liquidity_config.get("min_oi_usd", 10_000_000)
    short_funding_annual = short_config.get("funding_rate_annual", 0.80)
    short_price_change = short_config.get("price_change_24h", 0.12)

    long_min_oi = liquidity_config.get("min_oi_usd", 10_000_000)
    long_funding_annual = long_config.get("funding_rate_annual", -0.20)
    long_price_change = long_config.get("price_change_24h", -0.10)

    min_volume_24h = liquidity_config.get("min_volume_24h", 50_000_000)

    # 标题
    lines.append("# HRS 全市场扫描 + 批量回测报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    lines.append("")

    # ========== 一、扫描概要 ==========
    lines.append("## 一、扫描概要")
    lines.append("")

    stats = scan_summary.get("market_stats", {})
    total_pairs = stats.get("total_trading_pairs", 0)
    pre_filtered = stats.get("pre_filtered", 0)
    short_passed = stats.get("short_passed", 0)
    long_passed = stats.get("long_passed", 0)
    oi_fetched = stats.get("oi_fetched", 0)
    analyzed_count = len(analysis_results)

    # 筛选条件（从配置动态读取，避免硬编码）
    lines.append("### 筛选条件")
    lines.append("")
    lines.append(f"- 合约类型: USDT 永续合约 (PERPETUAL)")
    lines.append(f"- 排除 BTCUSDT、ETHUSDT、稳定币、杠杆代币")
    lines.append(f"- 流动性门槛: 24h 成交额 >= {min_volume_24h / 1_000_000:.0f}万 USDT")
    lines.append(
        f"- 做空: OI >= {short_min_oi / 1_000_000:.0f}万 USDT, "
        f"年化费率 >= {short_funding_annual * 100:.0f}%, "
        f"24h涨幅 >= {short_price_change * 100:.0f}%"
    )
    lines.append(
        f"- 做多: OI >= {long_min_oi / 1_000_000:.0f}万 USDT, "
        f"年化费率 <= {long_funding_annual * 100:.0f}%, "
        f"24h跌幅 <= {long_price_change * 100:.0f}%"
    )
    lines.append("")

    # 扫描结果统计
    lines.append("### 扫描统计")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 全市场 USDT 永续合约 | {total_pairs} |")
    lines.append(f"| 初筛通过（流动性达标） | {pre_filtered} |")
    lines.append(f"| OI 数据获取成功 | {oi_fetched} |")
    lines.append(f"| 做空候选通过 | {short_passed} |")
    lines.append(f"| 做多候选通过 | {long_passed} |")
    lines.append(f"| 深度分析完成 | {analyzed_count} |")
    lines.append("")

    # 扫描错误
    errors = scan_summary.get("errors", [])
    if errors:
        lines.append("### 扫描中的错误")
        lines.append("")
        for err in errors[:10]:
            lines.append(f"- {err}")
        if len(errors) > 10:
            lines.append(f"- ... 共 {len(errors)} 个错误")
        lines.append("")

    # ========== 二、做空方向排名表 ==========
    lines.append("## 二、做空方向排名表")
    lines.append("")
    lines.append("> 只显示候选池通过且评分 > 0 的币种，按总分降序排列")
    lines.append("")

    short_scored = _collect_direction_scored(analysis_results, "short")
    if short_scored:
        lines.append(
            "| 排名 | 交易对 | 价格 | OI(USDT) | 年化费率 | 24h涨跌 | "
            "合约分 | 技术分 | 情绪分 | 总分 | 建议入场 |"
        )
        lines.append(
            "|------|--------|------|----------|----------|---------|"
            "--------|--------|--------|------|----------|"
        )

        for i, s in enumerate(short_scored, 1):
            enter_mark = "✅ 是" if s["should_enter"] else "否"
            lines.append(
                f"| {i} | {s['symbol']} | {_fmt_price(s['price'])} | "
                f"{_fmt_volume(s['oi_usd'])} | {_fmt_pct(s['annual_funding'])} | "
                f"{_fmt_pct(s['price_change'])} | "
                f"{s['contract_score']:.2f} | {s['technical_score']:.2f} | "
                f"{s['sentiment_score']:.2f} | **{s['total_score']:.2f}** | {enter_mark} |"
            )
    else:
        lines.append("> 无符合条件的做空候选")
    lines.append("")

    # ========== 三、做多方向排名表 ==========
    lines.append("## 三、做多方向排名表")
    lines.append("")
    lines.append("> 只显示候选池通过且评分 > 0 的币种，按总分降序排列")
    lines.append("")

    long_scored = _collect_direction_scored(analysis_results, "long")
    if long_scored:
        lines.append(
            "| 排名 | 交易对 | 价格 | OI(USDT) | 年化费率 | 24h涨跌 | "
            "合约分 | 技术分 | 情绪分 | 总分 | 建议入场 |"
        )
        lines.append(
            "|------|--------|------|----------|----------|---------|"
            "--------|--------|--------|------|----------|"
        )

        for i, s in enumerate(long_scored, 1):
            enter_mark = "✅ 是" if s["should_enter"] else "否"
            lines.append(
                f"| {i} | {s['symbol']} | {_fmt_price(s['price'])} | "
                f"{_fmt_volume(s['oi_usd'])} | {_fmt_pct(s['annual_funding'])} | "
                f"{_fmt_pct(s['price_change'])} | "
                f"{s['contract_score']:.2f} | {s['technical_score']:.2f} | "
                f"{s['sentiment_score']:.2f} | **{s['total_score']:.2f}** | {enter_mark} |"
            )
    else:
        lines.append("> 无符合条件的做多候选")
    lines.append("")

    # ========== 四、TOP 10 机会详情 ==========
    lines.append("## 四、TOP 10 机会详情")
    lines.append("")

    # 合并做空和做多，按总分降序取 TOP 10
    all_scored = short_scored + long_scored
    all_scored.sort(key=lambda x: x["total_score"], reverse=True)
    top10 = all_scored[:10]

    if top10:
        for i, s in enumerate(top10, 1):
            dir_label = "做空" if s["direction"] == "short" else "做多"
            enter_label = "建议入场" if s["should_enter"] else "不建议入场"
            enter_icon = "✅" if s["should_enter"] else ""

            lines.append(f"### {i}. {s['symbol']} ({dir_label}) {enter_icon}")
            lines.append("")
            lines.append(f"- **最新价格**: {_fmt_price(s['price'])}")
            lines.append(f"- **OI**: {_fmt_volume(s['oi_usd'])} USDT")
            lines.append(f"- **年化资金费率**: {_fmt_pct(s['annual_funding'])}")
            lines.append(f"- **24h涨跌**: {_fmt_pct(s['price_change'])}")
            lines.append(f"- **EMA20(4h)偏离**: {_fmt_pct(s.get('ema20_deviation', 0))}")
            lines.append("")

            lines.append("**评分拆解**:")
            lines.append("")
            lines.append("| 维度 | 得分 | 权重 | 加权得分 |")
            lines.append("|------|------|------|----------|")
            lines.append(f"| 合约数据 | {s['contract_score']:.2f} | {s.get('contract_weight', 0.25)*100:.0f}% | {s['contract_score'] * s.get('contract_weight', 0.25):.2f} |")
            lines.append(f"| 技术面 | {s['technical_score']:.2f} | {s.get('technical_weight', 0.45)*100:.0f}% | {s['technical_score'] * s.get('technical_weight', 0.45):.2f} |")
            lines.append(f"| 情绪面 | {s['sentiment_score']:.2f} | {s.get('sentiment_weight', 0.30)*100:.0f}% | {s['sentiment_score'] * s.get('sentiment_weight', 0.30):.2f} |")
            lines.append(f"| **总分** | **{s['total_score']:.2f}** | | |")
            lines.append("")

            if s.get("veto"):
                lines.append(f"- **否决**: {s.get('veto_reason', '未知原因')}")
            lines.append(f"- **入场判断**: {enter_label}")
            lines.append("")
    else:
        lines.append("> 无有效的评分结果")
        lines.append("")

    lines.append("---")
    lines.append("*报告由 HRS 全市场扫描系统自动生成*")

    return lines


def _collect_direction_scored(analysis_results: List[Dict], direction: str) -> List[Dict]:
    """
    收集指定方向的有效评分结果并按总分降序排序

    Args:
        analysis_results: 分析结果列表
        direction: "short" 或 "long"

    Returns:
        排序后的评分列表
    """
    scored = []
    for r in analysis_results:
        symbol = r.get("symbol", "?")
        price = r.get("current_price", 0)
        oi_usd = r.get("oi_usd", 0)
        annual_funding = r.get("annual_funding", 0)
        price_change = r.get("price_change_24h", 0)
        ema20_deviation = r.get("deviation_4h", 0)

        if direction == "short":
            candidate = r.get("candidate_short", False)
            score_result = r.get("short_score_result")
            should_enter = r.get("short_should_enter", False)
        else:
            candidate = r.get("candidate_long", False)
            score_result = r.get("long_score_result")
            should_enter = r.get("long_should_enter", False)

        # 只收录候选池通过且评分 > 0 的
        if not candidate:
            continue
        if score_result is None or not isinstance(score_result, dict):
            continue
        total = score_result.get("total_score", 0)
        if total <= 0:
            continue

        scored.append({
            "symbol": symbol,
            "direction": direction,
            "price": price,
            "oi_usd": oi_usd,
            "annual_funding": annual_funding,
            "price_change": price_change,
            "ema20_deviation": ema20_deviation,
            "total_score": total,
            "contract_score": score_result.get("contract_score", 0),
            "technical_score": score_result.get("technical_score", 0),
            "sentiment_score": score_result.get("sentiment_score", 0),
            "contract_weight": score_result.get("contract_weight", 0.25),
            "technical_weight": score_result.get("technical_weight", 0.45),
            "sentiment_weight": score_result.get("sentiment_weight", 0.30),
            "should_enter": should_enter,
            "veto": score_result.get("veto", False),
            "veto_reason": score_result.get("veto_reason"),
        })

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    return scored