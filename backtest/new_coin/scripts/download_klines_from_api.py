#!/usr/bin/env python3
"""通过币安合约 API 下载新币 1h K 线数据

功能：
1. 读取 /tmp/tables.txt 获取服务器数据库查到的 67 个新币列表
2. 读取 coin_list.json 校验币种（跳过中文币种和非新币）
3. 使用币安合约公开接口 GET https://fapi.binance.com/fapi/v1/klines 下载 1h K 线
4. 异步并发下载（aiohttp），每秒最多 5 个请求，失败重试 3 次
5. 保存为 CSV（7 列，带表头），与 data_loader.py 兼容

CSV 格式：
    open_time,open_price,high_price,low_price,close_price,volume,quote_volume
    2026-05-01 00:00:00,19.94,20.25,19.54,19.81,42535.09,843573.80

注意：
- 部分新币可能在起始时间之后才上线，API 会返回从上线时间开始的数据，属正常现象
- 部分币种可能已下架，API 返回空数据或错误，脚本会跳过并记录
- open_time 使用 UTC 时间，格式 YYYY-MM-DD HH:MM:SS
- 数值保留 API 返回的原始精度（字符串形式写入，避免浮点精度损失）
"""
import asyncio
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import aiohttp

# ==================== 配置区 ====================
# 路径配置（支持环境变量覆盖，便于在服务器等不同环境运行）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
# 输出目录：优先使用环境变量，默认为项目内 data/klines
KLINES_DIR = os.environ.get("KLINES_DIR", os.path.join(DATA_DIR, "klines"))
# 币种列表文件：优先使用环境变量
COIN_LIST_PATH = os.environ.get("COIN_LIST_PATH", os.path.join(DATA_DIR, "coin_list.json"))
# 服务器数据库表名列表文件：优先使用环境变量
TABLES_FILE = os.environ.get("TABLES_FILE", "/tmp/tables.txt")

# 币安合约 API 配置
BINANCE_FAPI_BASE = "https://fapi.binance.com"
KLINES_ENDPOINT = "/fapi/v1/klines"
KLINE_INTERVAL = "1h"

# 时间范围配置
# 用户要求：2026-05-01 00:00:00 UTC ~ 2026-06-23 23:59:59 UTC
# 注意：用户原始提供的数值 1746057600000 / 1750713599000 实际对应 2025 年
#       （这些新币 2026 年才上线，2025 年无数据），已修正为正确的 2026 年时间戳。
#       使用 datetime 计算，避免硬编码错误数值，便于后续调整。
TIME_START = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
TIME_END = datetime(2026, 6, 23, 23, 59, 59, tzinfo=timezone.utc)
START_TIME_MS = int(TIME_START.timestamp() * 1000)
END_TIME_MS = int(TIME_END.timestamp() * 1000)

# 单次请求返回 K 线数量上限
# 53 天 * 24h = 1272 根，1500 一次请求足够覆盖
KLINE_LIMIT = 1500

# 并发与限流配置
MAX_CONCURRENCY = 5          # 最大并发请求数（信号量）
REQUEST_INTERVAL_SEC = 0.2   # 单个请求完成后等待时长，配合并发实现每秒约 5 个请求
MAX_RETRIES = 3              # 失败重试次数
RETRY_BACKOFF_SEC = 1.0      # 重试退避基准时长（秒），每次翻倍

# 请求超时（秒）
REQUEST_TIMEOUT = 30

# CSV 表头
CSV_HEADER = [
    "open_time", "open_price", "high_price", "low_price",
    "close_price", "volume", "quote_volume",
]

# ==================== 日志配置 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("download_klines")


# ==================== 工具函数 ====================

def is_chinese_symbol(symbol: str) -> bool:
    """判断交易对是否包含中文字符

    Args:
        symbol: 交易对符号，如 "AAOIUSDT" 或 "龙虾USDT"

    Returns:
        是否包含中文字符
    """
    # 匹配 CJK 统一汉字范围
    return bool(re.search(r"[\u4e00-\u9fff]", symbol))


def load_symbols_from_tables(tables_file: str) -> List[str]:
    """从 /tmp/tables.txt 读取新币列表

    文件格式为每行一个表名，如 "kline_aaoiusdt_1h"，
    提取其中的 symbol 部分（aaoiusdt），统一转为大写返回。

    Args:
        tables_file: 表名列表文件路径

    Returns:
        交易对符号列表（大写），如 ["AAOIUSDT", "ADBEUSDT", ...]
    """
    if not os.path.exists(tables_file):
        logger.error("表名列表文件不存在: %s", tables_file)
        return []

    symbols: List[str] = []
    with open(tables_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 格式：kline_{symbol}_1h，提取 symbol 部分
            # 使用正则提取，兼容可能的空白字符
            match = re.match(r"kline_(.+)_1h", line, re.IGNORECASE)
            if match:
                symbol = match.group(1).strip().upper()
                if symbol:
                    symbols.append(symbol)

    # 去重，保持顺序
    seen = set()
    unique_symbols = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique_symbols.append(s)

    return unique_symbols


def load_symbols_from_coin_list(coin_list_path: str) -> set:
    """从 coin_list.json 读取所有交易对（用于校验）

    Args:
        coin_list_path: coin_list.json 文件路径

    Returns:
        交易对符号集合（大写），已过滤掉中文币种
    """
    if not os.path.exists(coin_list_path):
        logger.error("coin_list.json 不存在: %s", coin_list_path)
        return set()

    with open(coin_list_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 兼容不同的 JSON 格式
    if isinstance(data, dict):
        contracts = data.get("contracts", [])
    elif isinstance(data, list):
        contracts = data
    else:
        logger.error("coin_list.json 格式错误: %s", type(data))
        return set()

    symbols = set()
    for c in contracts:
        symbol = c.get("symbol", "").strip().upper()
        if not symbol:
            continue
        # 跳过中文币种
        if is_chinese_symbol(symbol):
            logger.info("跳过中文币种: %s", symbol)
            continue
        symbols.add(symbol)

    return symbols


def filter_target_symbols(
    table_symbols: List[str], coin_list_symbols: set
) -> Tuple[List[str], List[str]]:
    """筛选目标币种列表

    规则：
    - 以 tables.txt 的 67 个币种为准
    - 跳过中文币种
    - 跳过不在 coin_list.json 中的币种（非新币）

    Args:
        table_symbols: tables.txt 中提取的币种列表
        coin_list_symbols: coin_list.json 中的币种集合

    Returns:
        (目标币种列表, 被跳过的币种列表)
    """
    target: List[str] = []
    skipped: List[str] = []

    for symbol in table_symbols:
        # 跳过中文币种
        if is_chinese_symbol(symbol):
            logger.info("跳过中文币种: %s", symbol)
            skipped.append(symbol)
            continue
        # 跳过不在 coin_list.json 中的币种（非新币）
        if symbol not in coin_list_symbols:
            logger.warning("币种 %s 不在 coin_list.json 中，跳过（非新币）", symbol)
            skipped.append(symbol)
            continue
        target.append(symbol)

    return target, skipped


def ms_to_utc_str(ts_ms: int) -> str:
    """毫秒时间戳转 UTC 时间字符串

    Args:
        ts_ms: 毫秒时间戳

    Returns:
        UTC 时间字符串，格式 YYYY-MM-DD HH:MM:SS
    """
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ==================== 核心下载逻辑 ====================

async def fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    semaphore: asyncio.Semaphore,
) -> Optional[List[list]]:
    """从币安合约 API 下载单个币种的 1h K 线数据

    使用信号量控制并发，失败时按指数退避重试。

    Args:
        session: aiohttp 会话
        symbol: 交易对符号（大写），如 "AAOIUSDT"
        semaphore: 并发信号量

    Returns:
        K 线原始数据列表（币安 API 返回的二维数组），失败返回 None
    """
    params = {
        "symbol": symbol,
        "interval": KLINE_INTERVAL,
        "startTime": START_TIME_MS,
        "endTime": END_TIME_MS,
        "limit": KLINE_LIMIT,
    }
    url = f"{BINANCE_FAPI_BASE}{KLINES_ENDPOINT}"

    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # 使用 aiohttp 的 timeout 参数控制单次请求超时
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                ) as resp:
                    # HTTP 状态码检查
                    if resp.status == 200:
                        data = await resp.json()
                        # 限流等待，避免超过每秒 5 个请求
                        await asyncio.sleep(REQUEST_INTERVAL_SEC)
                        return data

                    # 429 表示请求过于频繁，需要退避重试
                    if resp.status == 429:
                        wait_sec = RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
                        logger.warning(
                            "币种 %s 触发限流(429)，第 %d 次重试，等待 %.1f 秒",
                            symbol, attempt, wait_sec,
                        )
                        await asyncio.sleep(wait_sec)
                        continue

                    # 其他 HTTP 错误（如 400 表示币种不存在/已下架）
                    text = await resp.text()
                    logger.error(
                        "币种 %s 请求失败: HTTP %d, 响应=%s",
                        symbol, resp.status, text[:200],
                    )
                    # 400 错误通常是币种不存在，无需重试
                    if resp.status == 400:
                        return None
                    # 其他错误继续重试
                    await asyncio.sleep(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)))

            except asyncio.TimeoutError:
                logger.warning(
                    "币种 %s 请求超时，第 %d 次重试", symbol, attempt,
                )
                await asyncio.sleep(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)))
            except aiohttp.ClientError as e:
                logger.warning(
                    "币种 %s 网络错误: %s，第 %d 次重试",
                    symbol, str(e)[:100], attempt,
                )
                await asyncio.sleep(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)))
            except Exception as e:
                logger.error(
                    "币种 %s 发生未知错误: %s", symbol, str(e)[:200],
                )
                return None

    logger.error("币种 %s 重试 %d 次后仍失败", symbol, MAX_RETRIES)
    return None


def save_klines_to_csv(symbol: str, klines: List[list]) -> int:
    """将 K 线数据保存为 CSV 文件

    文件名使用小写 symbol，与现有数据文件命名一致（如 aaoiusdt_1h.csv）。
    CSV 格式：7 列，带表头，open_time 为 UTC 时间字符串。

    Args:
        symbol: 交易对符号（大写），如 "AAOIUSDT"
        klines: 币安 API 返回的 K 线二维数组

    Returns:
        写入的 K 线数量
    """
    # 文件名使用小写，与现有数据文件保持一致
    filename = f"{symbol.lower()}_{KLINE_INTERVAL}.csv"
    filepath = os.path.join(KLINES_DIR, filename)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # 写入表头
        writer.writerow(CSV_HEADER)
        # 写入数据行
        # 币安 K 线返回格式（索引）：
        # 0: open_time(ms), 1: open, 2: high, 3: low, 4: close,
        # 5: volume, 6: close_time(ms), 7: quote_volume, ...
        for k in klines:
            writer.writerow([
                ms_to_utc_str(int(k[0])),  # open_time -> UTC 字符串
                k[1],  # open_price（原始字符串）
                k[2],  # high_price
                k[3],  # low_price
                k[4],  # close_price
                k[5],  # volume
                k[7],  # quote_volume
            ])

    return len(klines)


async def download_all(symbols: List[str]) -> None:
    """异步下载所有币种的 K 线数据

    Args:
        symbols: 目标交易对符号列表（大写）
    """
    # 确保输出目录存在
    os.makedirs(KLINES_DIR, exist_ok=True)

    # 信号量控制并发，实现每秒约 5 个请求
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    # 统计结果
    success_count = 0
    empty_count = 0
    fail_count = 0
    results: List[Tuple[str, int]] = []  # (symbol, kline_count)

    # 请求头
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KlineDownloader/1.0)",
        "Accept": "application/json",
    }

    # 连接器配置：启用连接池，限制单主机连接数
    connector = aiohttp.TCPConnector(
        limit=MAX_CONCURRENCY,
        limit_per_host=MAX_CONCURRENCY,
        ssl=False,
    )

    logger.info("开始下载 %d 个币种的 1h K 线数据...", len(symbols))
    logger.info(
        "时间范围: %s ~ %s (UTC)",
        ms_to_utc_str(START_TIME_MS),
        ms_to_utc_str(END_TIME_MS),
    )

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # 为每个币种创建下载任务
        tasks = [
            fetch_klines(session, symbol, semaphore) for symbol in symbols
        ]

        # 逐个获取结果，便于实时打印进度
        for idx, (symbol, task) in enumerate(zip(symbols, tasks), start=1):
            klines = await task

            if klines is None:
                logger.error("[%d/%d] %s: 下载失败", idx, len(symbols), symbol)
                fail_count += 1
                results.append((symbol, -1))
                continue

            if len(klines) == 0:
                logger.warning(
                    "[%d/%d] %s: 无数据（可能已下架或时间范围内未上线）",
                    idx, len(symbols), symbol,
                )
                empty_count += 1
                results.append((symbol, 0))
                continue

            # 保存为 CSV
            count = save_klines_to_csv(symbol, klines)
            logger.info(
                "[%d/%d] %s: 成功下载 %d 根 K 线",
                idx, len(symbols), symbol, count,
            )
            success_count += 1
            results.append((symbol, count))

    # 打印汇总
    print()
    print("=" * 60)
    print("下载汇总")
    print("=" * 60)
    print(f"总币种数: {len(symbols)}")
    print(f"成功: {success_count}")
    print(f"无数据: {empty_count}")
    print(f"失败: {fail_count}")
    print()
    print("各币种 K 线数量明细:")
    print("-" * 60)
    for symbol, count in results:
        if count > 0:
            status = f"{count} 根"
        elif count == 0:
            status = "无数据"
        else:
            status = "失败"
        # 文件名用小写显示，与实际文件一致
        print(f"  {symbol.lower():<20} {status}")
    print("-" * 60)
    print(f"数据目录: {os.path.abspath(KLINES_DIR)}")


# ==================== 主函数 ====================

def main() -> int:
    """主函数

    Returns:
        退出码（0 成功，1 失败）
    """
    print("=" * 60)
    print("币安合约 1h K 线下载工具")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据目录: {os.path.abspath(KLINES_DIR)}")
    print("=" * 60)

    # 1. 从 /tmp/tables.txt 读取新币列表
    table_symbols = load_symbols_from_tables(TABLES_FILE)
    if not table_symbols:
        logger.error("未能从 %s 读取到任何币种", TABLES_FILE)
        return 1
    logger.info("从 %s 读取到 %d 个币种", TABLES_FILE, len(table_symbols))

    # 2. 从 coin_list.json 读取币种集合（用于校验）
    coin_list_symbols = load_symbols_from_coin_list(COIN_LIST_PATH)
    logger.info("从 coin_list.json 读取到 %d 个有效币种（已过滤中文）", len(coin_list_symbols))

    # 3. 筛选目标币种
    target_symbols, skipped_symbols = filter_target_symbols(
        table_symbols, coin_list_symbols
    )
    logger.info("目标币种数: %d，跳过币种数: %d", len(target_symbols), len(skipped_symbols))

    if skipped_symbols:
        logger.info("跳过的币种: %s", ", ".join(skipped_symbols))

    if not target_symbols:
        logger.error("没有可下载的目标币种")
        return 1

    # 4. 异步下载
    asyncio.run(download_all(target_symbols))

    return 0


if __name__ == "__main__":
    sys.exit(main())
