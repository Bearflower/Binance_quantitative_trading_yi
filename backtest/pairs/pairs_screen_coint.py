#!/usr/bin/env python3
"""统计套利 · 配对交易 — 第一步：协整筛选

目标：
    在本地 112 币日线池中筛选「统计上稳定协整」的币对，为配对交易回测
    提供候选池（第二步）。这一步的判决条件：稳定协整对的数量是否足够多。

方法（全部无前视，本地执行）：
    1. 构建收盘价面板（date × symbol），排除 BTC/ETH 锚与样本不足的币
    2. 对每个 (A, B) 币对做 Engle-Granger 协整检验（含常数项，无趋势）
       + 对价差(spread = logA - beta*logB)做 Phillips-Perron / ADF 平稳性确认
    3. 对通过的币对计算半衰期（halflife，OU 过程，决定调仓频率）
    4. 输出：候选对清单 + 各对 价差均值/标准误/半衰期/ADF 统计量

筛选判据（保守，宁缺毋滥）：
    - ADF p_value < 0.05（价差平稳，即协整）
    - 最小可交易样本（避免上市初期噪声）
    - 价差半衰期在合理范围（太低=噪声、太高=死仓，先全量报告再人工筛）

依赖：numpy pandas scipy statsmodels（已本地安装）
修复：模式化输出，纯脚本，直接 python3 运行
"""
import csv
import os
from itertools import combinations
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as sps

try:
    from statsmodels.tsa.stattools import adfuller
    from statsmodels.regression.linear_model import OLS
except ImportError:  # pragma: no cover - 依赖缺失时的降级提示
    adfuller = None
    OLS = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 复用 momentum 已下载的受限池日线数据
KLINES_DIR = os.path.join(SCRIPT_DIR, "..", "momentum", "data", "klines")

# 排除锚/主币（作基准腿用，不作为配对的普通腿）
EXCLUDE = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}
# 每对共用最小满意样本数（个交易日）
MIN_OBS = 90
# 协整显著性阈值（ADF p 值）
COINT_PRETHRESH = 0.05
# 报告时按 p 值升序取前 N 条协整对
REPORT_TOP_N = 20


def log(msg: str) -> None:
    print(msg, flush=True)


def load_panel(min_obs: int = MIN_OBS) -> pd.DataFrame:
    """加载受限池收盘价面板（date × symbol，log 后用于价差）

    Args:
        min_obs: 每个币最少样本数（不足剔除）

    Returns:
        DataFrame，index=日期，columns=symbol，值为原始收盘价
    """
    closes: Dict[str, pd.Series] = {}
    bad_files: List[str] = []
    for fname in os.listdir(KLINES_DIR):
        if not fname.endswith("_1d.csv"):
            continue
        symbol = fname[:-len("_1d.csv")].upper()
        if symbol in EXCLUDE:
            continue
        filepath = os.path.join(KLINES_DIR, fname)
        try:
            rows = list(csv.reader(open(filepath, encoding="utf-8")))
        except UnicodeDecodeError:  # 个别文件可能非 utf-8，跳过并记录
            bad_files.append(fname)
            continue
        if not rows:
            continue
        header = rows[0]
        try:
            date_idx = header.index("open_time")
            close_idx = header.index("close_price")
        except ValueError:
            bad_files.append(fname)
            continue
        times, prices = [], []
        for r in rows[1:]:
            try:
                times.append(pd.to_datetime(r[date_idx]))
                prices.append(float(r[close_idx]))
            except (ValueError, IndexError):
                continue
        if len(times) >= min_obs:
            closes[symbol] = pd.Series(prices, index=pd.DatetimeIndex(times)).sort_index()
    if bad_files:
        log(f"  [跳过] {len(bad_files)} 个文件解析异常: {bad_files[:5]}...")
    panel = pd.DataFrame(closes)
    panel = panel[~panel.index.duplicated(keep="last")].sort_index()
    return panel.dropna(how="all")


def _adf_test(residual: np.ndarray) -> float:
    """对残差/价差做 ADF 平稳性检验，返回 p 值"""
    if adfuller is None:
        return 1.0
    try:
        # autolag=None 用固定滞后期，纯统计可复现
        result = adfuller(residual, autolag=None, maxlag=5, regression="c")
        return float(result[1])  # p 值
    except Exception:  # noqa: BLE001 - ADF 数值退化时保守返回不显著
        return 1.0


def _halflife(residual: np.ndarray) -> Optional[float]:
    """用一阶自回归(AR1)估计价差半衰期（OU 近似），返回天数"""
    x = residual[:-1]
    y = residual[1:]
    # 去除均值后回归：y = beta * x
    x_c, y_c = x - x.mean(), y - y.mean()
    denom = float(np.dot(x_c, x_c))
    if denom <= 1e-12:
        return None
    beta = float(np.dot(x_c, y_c)) / denom
    if beta <= 0 or beta >= 1:
        return None  # 无非平稳回流
    return abs(float(np.log(0.5) / np.log(beta)))


def _engle_granger(log_a: np.ndarray, log_b: np.ndarray) -> Dict[str, float]:
    """Engle-Granger 两步协整检验

    Step1: OLS log_b ~ const + log_a → 残差 spread
    Step2: 对 spread 做 ADF 检验

    Returns:
        含 beta(协整系数)、spread_std、halflife、adf_p 的字典
    """
    n = len(log_a)
    X = np.vstack([np.ones(n), log_a]).T
    # 最小二乘 beta
    coef, *_ = np.linalg.lstsq(X, log_b, rcond=None)
    intercept, beta = coef[0], coef[1]
    spread = log_b - (intercept + beta * log_a)
    spread_std = float(np.std(spread))
    adf_p = _adf_test(spread)
    hl = _halflife(spread)
    return {
        "intercept": float(intercept),
        "beta": float(beta),
        "spread_std": spread_std,
        "halflife": hl,
        "adf_p": adf_p,
    }


def _robust_coint(log_a: np.ndarray, log_b: np.ndarray) -> Dict[str, float]:
    """稳健协整检验：全样本 + 前后子样本 ADF，防数据窥探

    若只在全样本显著、但子样本不显著，则协整关系不稳定，风险大。
    Returns:
        含 adf_p(全样本)、adf_p_first(前1/3)、adf_p_last(后1/3) 的字典
    """
    full = _engle_granger(log_a, log_b)
    n = len(log_a)
    split = n // 3
    # 后 1/3 用最新数据（对当前可交易性最相关）；前 1/3 验证关系是否长期存在
    first = _engle_granger(log_a[:split], log_b[:split])
    last = _engle_granger(log_a[-split:], log_b[-split:])
    return {
        "adf_p": full["adf_p"],
        "adf_p_first": first["adf_p"],
        "adf_p_last": last["adf_p"],
        "halflife": full["halflife"],
        "beta": full["beta"],
        "spread_std": full["spread_std"],
    }


def main() -> int:
    log("=" * 66)
    log("统计套利 · 协整筛选（第一步）")
    log(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 66)

    panel = load_panel()
    symbols = list(panel.columns)
    log(f"参与协整筛选的币种: {len(symbols)} (已排除 {len(EXCLUDE)} 主币/锚)")
    log(f"时间范围: {panel.index[0].date()} ~ {panel.index[-1].date()}")
    log(f"价差用对数: spread = log(B) - beta*log(A)\n")

    # 对数价格
    log_panel = np.log(panel)

    # 全对枚举
    pairs = list(combinations(symbols, 2))
    log(f"候选币对数: {len(pairs)}")

    results: List[Tuple[str, str, Dict[str, float], int]] = []
    for a, b in pairs:
        # 取两币都非缺失的样本
        mask = panel[[a, b]].notna().all(axis=1)
        if mask.sum() < MIN_OBS:
            continue
        log_a = log_panel[a][mask].values
        log_b = log_panel[b][mask].values
        r = _robust_coint(log_a, log_b)
        results.append((a, b, r, int(mask.sum())))

    # ---- 初筛：全样本协整 + 半衰期合理 ----
    base = [x for x in results
            if x[2]["adf_p"] < COINT_PRETHRESH
            and x[2]["halflife"] and 2 <= x[2]["halflife"] <= 90]
    # ---- 稳健筛选：全样本 + 后1/3子样本均显著（当前可交易性最关键） ----
    # 前1/3子样本显著性单独报告（验证长期稳定性，不硬性要求）
    COINT_SUBS_PRETHRESH = 0.10  # 子样本放松到 0.10，避免短期数据过苛
    robust = [x for x in base if x[2]["adf_p_last"] < COINT_SUBS_PRETHRESH]
    # ---- 强稳健：全样本 + 前后子样本都显著（最可靠，量最少） ----
    strong = [x for x in robust if x[2]["adf_p_first"] < COINT_SUBS_PRETHRESH]

    base.sort(key=lambda x: x[2]["adf_p"])
    robust.sort(key=lambda x: x[2]["adf_p"])
    strong.sort(key=lambda x: x[2]["adf_p"])

    log(f"\n① 基础筛选(全样本协整 + 半衰期[2,90]d): {len(base)} 对")
    log(f"② 稳健筛选(上述 + 后1/3子样本协整): {len(robust)} 对")
    log(f"③ 强稳健(上述 + 前1/3子样本也协整): {len(strong)} 对")
    log(f"   占比  ① {len(base)/len(results)*100:.1f}%  "
        f"② {len(robust)/len(results)*100:.1f}%  "
        f"③ {len(strong)/len(results)*100:.1f}%  (基数 {len(results)})")

    # 用稳健筛选作为进入回测的候选池（防止纯全样本的data snooping）
    candidates = robust if robust else base
    if not candidates:
        log("\n❌ 稳健筛选后没有协整对 → 方向终结，无需进入回测。")
        return 1

    log(f"\n进入第二步的候选池（稳健筛选，{len(candidates)} 对）Top {min(REPORT_TOP_N, len(candidates))}:")
    log(f"{'A':<12}{'B':<12}{'beta':>7}{'半衰期d':>9}{'价差std':>9}"
        f"{'全ADF':>8}{'前1/3':>8}{'后1/3':>8}{'n':>6}")
    for a, b, r, n in candidates[:REPORT_TOP_N]:
        hl = f"{r['halflife']:.1f}" if r["halflife"] else "NA"
        log(f"{a:<12}{b:<12}{r['beta']:>7.3f}{hl:>9}{r['spread_std']:>9.4f}"
            f"{r['adf_p']:>8.4f}{r['adf_p_first']:>8.4f}{r['adf_p_last']:>8.4f}{n:>6}")

    # 强稳健数量（衡量关系是否长期稳定）
    if strong:
        log(f"\n长期稳定(③强稳健)的协整对: {len(strong)} ← 最可靠，可优先考虑")
        log(f"{'A':<12}{'B':<12}{'beta':>7}{'半衰期d':>9}{'价差std':>9}{'n':>6}")
        for a, b, r, n in strong[:min(REPORT_TOP_N, len(strong))]:
            hl = f"{r['halflife']:.1f}" if r["halflife"] else "NA"
            log(f"{a:<12}{b:<12}{r['beta']:>7.3f}{hl:>9}{r['spread_std']:>9.4f}{n:>6}")
    else:
        log("\n长期稳定(③强稳健)的协整对: 0 ← 关系多在近期才成立，需警惕")

    log("\n结论依据：稳健筛选通过对数 + 长期稳定对数，判断是否值得进入价差回测。")
    log("=" * 66)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())