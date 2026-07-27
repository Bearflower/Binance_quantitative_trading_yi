#!/usr/bin/env python3
"""从服务器 PostgreSQL 数据库导出新币策略数据到本地 CSV

功能：
1. 导出订单记录（new_coin.orders 表）
2. 导出持仓记录（new_coin.short_positions 表）
3. 导出策略状态（public.strategy_states 表）
4. 从币安 API 下载/更新 K 线数据

数据兼容性：
- 订单/持仓 CSV 带表头，便于分析
- K 线 CSV 无表头，与币安 API 原始格式一致，兼容 data_loader.py

数据库信息：
- 数据库名: trading_platform
- 用户名: trading_user
- 新币策略表在 new_coin schema 下
"""
import asyncio
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import List

# 服务器连接配置
SERVER = "root@43.156.242.184"
SSH_KEY = "/Users/yl/vscode/inspection_automation/docs/only.pem"
CONTAINER = "trading_system-postgres"
DB = "trading_platform"
DB_USER = "trading_user"

# 路径配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
KLINES_DIR = os.path.join(DATA_DIR, "klines")

# K 线下载配置
KLINE_INTERVALS = ["1h", "5m", "15m"]
KLINE_LIMIT = 500
# K 线文件缓存有效期（秒），1 小时内不重复下载
KLINE_CACHE_TTL = 3600


def _build_ssh_cmd(remote_cmd: str) -> List[str]:
    """构建 SSH 命令

    Args:
        remote_cmd: 在远程服务器上执行的命令

    Returns:
        SSH 命令参数列表
    """
    return [
        "ssh", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        SERVER,
        remote_cmd,
    ]


def run_ssh_cmd(cmd: List[str], timeout: int = 120) -> tuple:
    """执行 SSH 命令并返回结果

    Args:
        cmd: 命令参数列表
        timeout: 超时时间（秒）

    Returns:
        (stdout, returncode) 元组
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        print(f"  [错误] 命令超时（{timeout}秒）")
        return "", 1
    except Exception as e:
        print(f"  [错误] 命令执行失败: {e}")
        return "", 1


def _export_table_via_copy(sql: str, output_file: str, description: str) -> bool:
    """通过 COPY TO STDOUT 导出 PostgreSQL 表数据

    使用 psql 的 \\copy 元命令（以点号开头），将查询结果输出到标准输出，
    通过 SSH 管道捕获到本地文件。

    Args:
        sql: 查询 SQL 语句
        output_file: 本地输出文件路径
        description: 导出描述（用于日志）

    Returns:
        是否导出成功
    """
    # 使用 psql -c 执行 SQL，通过管道捕获输出
    # COPY ... TO STDOUT 是 SQL 命令，需要超级用户权限
    # trading_user 是数据库所有者，拥有该权限
    remote_cmd = (
        f'docker exec {CONTAINER} psql -U {DB_USER} -d {DB} '
        f'-c "{sql}"'
    )
    cmd = _build_ssh_cmd(remote_cmd)

    stdout, rc = run_ssh_cmd(cmd, timeout=60)
    if rc != 0 or not stdout.strip():
        print(f"  [失败] 导出{description}失败（返回码: {rc}）")
        # 打印错误信息
        stderr_lines = stdout.strip().split("\n") if stdout else []
        for line in stderr_lines[:5]:
            if "ERROR" in line.upper() or "FATAL" in line.upper():
                print(f"    {line[:200]}")
        return False

    # 检查是否包含有效数据行（COPY 输出以数据行开始，不含 psql 格式装饰）
    lines = stdout.strip().split("\n")
    # COPY TO STDOUT 输出格式：纯 CSV 数据，无 psql 边框
    # 但如果 SQL 执行出错，psql 会输出错误信息
    data_lines = [l for l in lines if l.strip() and not l.startswith("(")]
    if not data_lines:
        print(f"  [警告] 导出{description}无数据")
        return False

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(stdout)

    # 统计记录数
    record_count = len(data_lines) - 1 if "HEADER" in sql.upper() else len(data_lines)
    print(f"  [成功] 导出{description}完成: {output_file} ({record_count} 条记录)")
    return True


def export_orders() -> bool:
    """导出订单记录

    从 new_coin.orders 表导出所有新币策略的订单数据。

    Returns:
        是否导出成功
    """
    print("=" * 50)
    print("导出订单记录...")

    sql = (
        "COPY ("
        "  SELECT id, order_id, symbol, strategy, side, type, "
        "         quantity, price, status, score, created_at "
        "  FROM new_coin.orders "
        "  ORDER BY created_at ASC"
        ") TO STDOUT WITH CSV HEADER"
    )

    output_file = os.path.join(DATA_DIR, "orders.csv")
    return _export_table_via_copy(sql, output_file, "订单记录")


def export_short_positions() -> bool:
    """导出持仓记录

    从 new_coin.short_positions 表导出所有持仓数据。

    Returns:
        是否导出成功
    """
    print("=" * 50)
    print("导出持仓记录...")

    sql = (
        "COPY ("
        "  SELECT id, symbol, position_id, quantity, entry_price, "
        "         current_price, unrealized_pnl, liquidation_price, "
        "         status, opened_at, closed_at, metadata "
        "  FROM new_coin.short_positions "
        "  ORDER BY opened_at ASC"
        ") TO STDOUT WITH CSV HEADER"
    )

    output_file = os.path.join(DATA_DIR, "short_positions.csv")
    return _export_table_via_copy(sql, output_file, "持仓记录")


def export_strategy_states() -> bool:
    """导出策略状态

    从 public.strategy_states 表导出策略状态数据。

    Returns:
        是否导出成功
    """
    print("=" * 50)
    print("导出策略状态...")

    sql = (
        "COPY ("
        "  SELECT strategy_name, state_key, state_data, updated_at "
        "  FROM public.strategy_states "
        "  ORDER BY updated_at DESC"
        ") TO STDOUT WITH CSV HEADER"
    )

    output_file = os.path.join(DATA_DIR, "strategy_states.csv")
    return _export_table_via_copy(sql, output_file, "策略状态")


def get_symbols_from_coin_list() -> List[str]:
    """从本地 coin_list.json 获取交易对列表

    Returns:
        交易对符号列表
    """
    coin_list_path = os.path.join(DATA_DIR, "coin_list.json")
    if not os.path.exists(coin_list_path):
        print("  [警告] coin_list.json 不存在")
        return []

    with open(coin_list_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 兼容不同的 JSON 格式
    if isinstance(data, dict):
        contracts = data.get("contracts", [])
    elif isinstance(data, list):
        contracts = data
    else:
        print(f"  [警告] coin_list.json 格式错误: {type(data)}")
        return []

    symbols = [c.get("symbol", "") for c in contracts if c.get("symbol")]
    return symbols


def get_symbols_from_orders() -> List[str]:
    """从本地订单 CSV 中提取已交易币种

    Returns:
        交易对符号列表（去重）
    """
    orders_file = os.path.join(DATA_DIR, "orders.csv")
    if not os.path.exists(orders_file):
        return []

    symbols = set()
    with open(orders_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("symbol", "").strip()
            if symbol:
                symbols.add(symbol)

    return list(symbols)


def _is_kline_cache_valid(filepath: str) -> bool:
    """检查 K 线文件缓存是否有效

    Args:
        filepath: K 线文件路径

    Returns:
        缓存是否在有效期内
    """
    if not os.path.exists(filepath):
        return False

    mtime = os.path.getmtime(filepath)
    elapsed = datetime.now().timestamp() - mtime
    return elapsed < KLINE_CACHE_TTL


async def download_klines_from_binance(symbols: List[str]) -> None:
    """从币安 API 下载 K 线数据到本地 CSV

    使用项目现有的 BinanceClient 公开 API (get_klines)，
    CSV 格式与现有数据兼容（无表头，与币安原始格式一致）。

    Args:
        symbols: 需要下载的交易对列表
    """
    print("=" * 50)
    print(f"从币安 API 下载 K 线数据（{len(symbols)} 个币种）...")

    # 导入项目模块
    sys.path.insert(0, PROJECT_ROOT)
    from dotenv import load_dotenv
    from shared.binance_api import BinanceClient

    # 加载环境变量
    env_path = os.path.join(PROJECT_ROOT, ".env")
    load_dotenv(env_path)

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    if not api_key or not api_secret:
        print("  [错误] 未配置币安 API 密钥，请在 .env 文件中设置")
        return

    client = BinanceClient(api_key, api_secret)

    try:
        success_count = 0
        skip_count = 0
        fail_count = 0

        for symbol in symbols:
            for interval in KLINE_INTERVALS:
                filepath = os.path.join(KLINES_DIR, f"{symbol}_{interval}.csv")

                # 检查缓存是否有效
                if _is_kline_cache_valid(filepath):
                    skip_count += 1
                    continue

                try:
                    # 使用公开 API 获取 K 线数据
                    klines = await client.get_klines(
                        symbol=symbol,
                        interval=interval,
                        limit=KLINE_LIMIT,
                    )

                    if klines and len(klines) > 0:
                        # 写入 CSV（无表头，与币安原始格式一致，兼容 data_loader.py）
                        with open(filepath, "w", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            for k in klines:
                                writer.writerow([
                                    k["open_time"],
                                    k["open"],
                                    k["high"],
                                    k["low"],
                                    k["close"],
                                    k["volume"],
                                    k["close_time"],
                                    k["quote_volume"],
                                    k["trades"],
                                    k["taker_buy_base"],
                                    k["taker_buy_quote"],
                                ])
                        print(f"  [成功] {symbol}_{interval}: {len(klines)} 根 K 线")
                        success_count += 1
                    else:
                        print(f"  [跳过] {symbol}_{interval}: 无数据")
                        skip_count += 1

                except Exception as e:
                    error_msg = str(e)[:100]
                    print(f"  [失败] {symbol}_{interval}: {error_msg}")
                    fail_count += 1

        print(f"\n  K 线下载汇总: 成功={success_count}, 跳过={skip_count}, 失败={fail_count}")

    finally:
        # 关闭客户端连接
        if hasattr(client, "close"):
            await client.close()


def export_klines() -> None:
    """导出 K 线数据（从币安 API 下载）"""
    print("=" * 50)
    print("导出 K 线数据...")

    # 获取币种列表：优先从 coin_list.json，其次从订单记录
    symbols = get_symbols_from_coin_list()
    if not symbols:
        print("  [信息] coin_list.json 无数据，尝试从订单记录获取币种...")
        symbols = get_symbols_from_orders()

    if not symbols:
        print("  [警告] 无可用币种列表，跳过 K 线下载")
        return

    print(f"  需要下载 {len(symbols)} 个币种的 K 线数据")
    asyncio.run(download_klines_from_binance(symbols))


def main():
    """主函数"""
    print("新币策略数据导出工具")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据目录: {os.path.abspath(DATA_DIR)}")
    print()

    # 确保目录存在
    os.makedirs(KLINES_DIR, exist_ok=True)

    # 1. 导出订单记录
    export_orders()

    # 2. 导出持仓记录
    export_short_positions()

    # 3. 导出策略状态
    export_strategy_states()

    # 4. 下载 K 线数据
    export_klines()

    print()
    print("=" * 50)
    print("导出完成！")
    print(f"数据目录: {os.path.abspath(DATA_DIR)}")


if __name__ == "__main__":
    main()
