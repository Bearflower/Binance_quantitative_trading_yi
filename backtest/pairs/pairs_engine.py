"""统计套利 · 配对交易 — 共享引擎

提供配对交易研究与回测共用的核心能力：
    1. 加载受限池日线收盘价面板（date × symbol）
    2. Engle-Granger 协整检验 + ADF 平稳性 + 半衰期估计
    3. 稳健协整筛选（全样本 + 前/后子样本，防数据窥探）
    4. 锚腿去冗余（同一锚币在组合中使用次数有上限）

供配对交易全链路复用，避免在协整筛选与价差回测之间复制代码。

依赖：numpy pandas scipy statsmodels（本地已安装）
"""
import csv
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from statsmodels.tsa.stattools import adfuller
except ImportError:  # pragma: no cover - 依赖缺失时的降级提示
    adfuller = None

# 复用 momentum 已下载的受限池日线数据
KLINES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "momentum", "data", "klines",
)

# 排除主币/锚（作为基准腿，不参与普通配对）
EXCLUDE = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}

# 每对共用最小样本数（交易日）
MIN_OBS = 90
# 协整显著性阈值（ADF p 值）
COINT_PRETHRESH = 0.05
# 子样本稳重阈值（前/后 1/3，防数据窥探，稍放宽避免短期过苛）
COINT_SUBS_PRETHRESH = 0.10
# 半衰期合理化区间（太低=噪声，太高=死仓）
HALFLIFE_MIN, HALFLIFE_MAX = 2.0, 90.0
# 组合层：单个锚币最多使用的次数（锚腿去冗余，避免重仓同一标的）
MAX_ANCHOR_USES = 3
# 新币过滤：单币最少历史样本（交易日），剔除上市期过短的假协整
MIN_HISTORY = 400


def load_panel(min_obs: int = MIN_OBS) -> pd.DataFrame:
    """加载受限池收盘价面板（date × symbol）

    Args:
        min_obs: 每个币最少样本数（不足剔除）

    Returns:
        DataFrame，index=日期，columns=symbol(含 USDT 后缀)，值为原始收盘价
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
        try:
            header = rows[0]
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
        print(f"  [提示] 跳过 {len(bad_files)} 个无法解析的文件")
    panel = pd.DataFrame(closes)
    panel = panel[~panel.index.duplicated(keep="last")].sort_index()
    return panel.dropna(how="all")


def _adf_pvalue(residual: np.ndarray) -> float:
    """对残差/价差做 ADF 平稳性检验，返回 p 值"""
    if adfuller is None:
        return 1.0
    try:
        result = adfuller(residual, autolag=None, maxlag=5, regression="c")
        return float(result[1])
    except Exception:  # noqa: BLE001 - ADF 数值退化时保守返回不显著
        return 1.0


def halflife_days(residual: np.ndarray) -> Optional[float]:
    """用一阶自回归(AR1)估计价差半衰期（OU 近似），返回天数"""
    x = residual[:-1]
    y = residual[1:]
    x_c, y_c = x - x.mean(), y - y.mean()
    denom = float(np.dot(x_c, x_c))
    if denom <= 1e-12:
        return None
    beta = float(np.dot(x_c, y_c)) / denom
    if beta <= 0 or beta >= 1:
        return None
    return abs(float(np.log(0.5) / np.log(beta)))


def engle_granger(log_a: np.ndarray, log_b: np.ndarray) -> Dict[str, float]:
    """Engle-Granger 两步协整检验

    Args:
        log_a: 币 A 的对数收盘价
        log_b: 币 B 的对数收盘价

    Returns:
        含 beta(协整系数)、intercept、spread_std、halflife、adf_p 的字典
    """
    n = len(log_a)
    X = np.vstack([np.ones(n), log_a]).T
    coef, *_ = np.linalg.lstsq(X, log_b, rcond=None)
    intercept, beta = coef[0], coef[1]
    spread = log_b - (intercept + beta * log_a)
    return {
        "intercept": float(intercept),
        "beta": float(beta),
        "spread_std": float(np.std(spread)),
        "halflife": halflife_days(spread),
        "adf_p": _adf_pvalue(spread),
    }


def coint_stats(log_a: np.ndarray, log_b: np.ndarray) -> Dict[str, float]:
    """稳健协整检验：全样本 + 前/后子样本 ADF

    Args:
        log_a: 币 A 对数收盘价
        log_b: 币 B 对数收盘价

    Returns:
        含 adf_p(全样本)、adf_p_first(前1/3)、adf_p_last(后1/3)、
        beta、halflife、spread_std、intercept 的字典
    """
    full = engle_granger(log_a, log_b)
    n = len(log_a)
    split = n // 3
    if split < 30:  # 子样本过小时直接用全样本代替，避免统计量不稳定
        return {**full, "adf_p_first": full["adf_p"], "adf_p_last": full["adf_p"]}
    first = engle_granger(log_a[:split], log_b[:split])
    last = engle_granger(log_a[-split:], log_b[-split:])
    return {
        "adf_p": full["adf_p"],
        "adf_p_first": first["adf_p"],
        "adf_p_last": last["adf_p"],
        "halflife": full["halflife"],
        "beta": full["beta"],
        "spread_std": full["spread_std"],
        "intercept": full["intercept"],
    }


def is_robust(stats: Dict[str, float]) -> bool:
    """是否通过稳健协整筛选（全样本 + 后 1/3 子样本均显著，半衰期合理）"""
    return (
        stats["adf_p"] < COINT_PRETHRESH
        and stats["adf_p_last"] < COINT_SUBS_PRETHRESH
        and stats["halflife"] is not None
        and HALFLIFE_MIN <= stats["halflife"] <= HALFLIFE_MAX
    )


def filter_min_history(
    pairs: List[Tuple[str, str, Dict[str, float]]],
    count: "Dict[str, int]",
    min_history: int = MIN_HISTORY,
) -> List[Tuple[str, str, Dict[str, float]]]:
    """过滤上市期过短（样本不足）的币所参与的对（防新币强动量假信号）

    以每币在面板中的有效样本数（交易日）为准，任一腿历史不足 min_history
    的对直接剔除。

    Args:
        pairs: 候选对列表
        count: symbol -> 有效样本数 映射
        min_history: 最少样本天数

    Returns:
        过滤后的对列表
    """
    return [x for x in pairs
            if count.get(x[0], 0) >= min_history
            and count.get(x[1], 0) >= min_history]


def dedup_anchors(
    pairs: List[Tuple[str, str, Dict[str, float]]],
    max_uses: int = MAX_ANCHOR_USES,
) -> List[Tuple[str, str, Dict[str, float]]]:
    """锚腿去冗余：限制任意单币在组合中的出现次数

    若同一币与多个币"协整"，往往是共同大盘因子（如所有币都跟某个锚同涨
    同跌）形成的伪协整，而非真实两两配对机会。这类对若全部加入组合会
    使所有实际敞口坍缩到同一个锚上，丧失分散价值。

    处理：按关系强度（adf_p 升序，越强越优先保留）贪心选择，任一生符在
    组合中的出现次数不超过 max_uses。

    Args:
        pairs: 候选对列表 [(A, B, stats)]，stats 含 adf_p
        max_uses: 单个币最多被使用的次数

    Returns:
        去冗余后的对列表
    """
    pairs = list(pairs)
    pairs.sort(key=lambda x: x[2]["adf_p"])
    count: Dict[str, int] = {}
    kept: List[Tuple[str, str, Dict[str, float]]] = []
    for a, b, stats in pairs:
        if count.get(a, 0) >= max_uses or count.get(b, 0) >= max_uses:
            continue
        count[a] = count.get(a, 0) + 1
        count[b] = count.get(b, 0) + 1
        kept.append((a, b, stats))
    return kept