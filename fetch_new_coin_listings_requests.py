"""
获取最近半年新上线的永续合约交易对（使用requests库）

功能：
1. 调用币安API获取交易所信息
2. 筛选最近半年内上线的永续合约
3. 保存为JSON文件

时间范围：2025-11-09 至 2026-05-09
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import requests
import structlog


# 配置日志
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()


def get_exchange_info() -> Dict[str, Any]:
    """
    获取交易所信息

    Returns:
        交易所信息字典
    """
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"

    logger.info("开始获取交易所信息", url=url)

    try:
        # 设置超时和重试
        session = requests.Session()
        session.mount('https://', requests.adapters.HTTPAdapter(
            max_retries=3
        ))

        response = session.get(url, timeout=30)
        response.raise_for_status()

        data = response.json()
        logger.info("交易所信息获取成功")

        return data

    except requests.exceptions.RequestException as e:
        logger.error("获取交易所信息失败", error=str(e))
        raise


def get_new_perpetual_contracts(
    exchange_info: Dict[str, Any],
    start_date: datetime,
    end_date: datetime
) -> List[Dict[str, Any]]:
    """
    获取指定时间范围内新上线的永续合约交易对

    Args:
        exchange_info: 交易所信息
        start_date: 开始日期
        end_date: 结束日期

    Returns:
        符合条件的交易对列表
    """
    # 提取交易对信息
    symbols = exchange_info.get('symbols', [])
    logger.info(f"获取到 {len(symbols)} 个交易对")

    # 筛选符合条件的交易对
    new_contracts = []

    for symbol_info in symbols:
        # 筛选条件1：合约类型为永续合约
        contract_type = symbol_info.get('contractType')
        if contract_type != 'PERPETUAL':
            continue

        # 筛选条件2：状态为交易中
        status = symbol_info.get('status')
        if status != 'TRADING':
            continue

        # 筛选条件3：上线时间在指定范围内
        onboard_date = symbol_info.get('onboardDate')
        if not onboard_date:
            logger.warning(f"交易对 {symbol_info.get('symbol')} 缺少上线时间")
            continue

        # 转换时间戳为datetime（毫秒时间戳）
        onboard_datetime = datetime.fromtimestamp(onboard_date / 1000, tz=timezone.utc)

        # 检查是否在时间范围内
        if start_date <= onboard_datetime <= end_date:
            # 提取关键信息
            contract_data = {
                'symbol': symbol_info.get('symbol'),
                'baseAsset': symbol_info.get('baseAsset'),
                'quoteAsset': symbol_info.get('quoteAsset'),
                'onboardDate': onboard_date,
                'onboardDateStr': onboard_datetime.strftime('%Y-%m-%d %H:%M:%S UTC'),
                'status': symbol_info.get('status'),
                'contractType': symbol_info.get('contractType'),
                'pricePrecision': symbol_info.get('pricePrecision'),
                'quantityPrecision': symbol_info.get('quantityPrecision'),
            }
            new_contracts.append(contract_data)

    # 按上线时间排序（最新的在前）
    new_contracts.sort(key=lambda x: x['onboardDate'], reverse=True)

    logger.info(f"筛选出 {len(new_contracts)} 个符合条件的永续合约")

    return new_contracts


def save_to_json(data: List[Dict[str, Any]], output_path: str) -> None:
    """
    保存数据到JSON文件

    Args:
        data: 要保存的数据
        output_path: 输出文件路径
    """
    # 确保目录存在
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # 构建输出数据
    output_data = {
        'query_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
        'time_range': {
            'start': '2025-11-09',
            'end': '2026-05-09'
        },
        'total_count': len(data),
        'contracts': data
    }

    # 保存到文件
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    logger.info(f"数据已保存到 {output_path}")


def main():
    """
    主函数
    """
    # 定义时间范围（最近半年）
    start_date = datetime(2025, 11, 9, tzinfo=timezone.utc)
    end_date = datetime(2026, 5, 9, 23, 59, 59, tzinfo=timezone.utc)

    logger.info(
        "开始获取新上线永续合约",
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d')
    )

    try:
        # 获取交易所信息
        exchange_info = get_exchange_info()

        # 获取新上线的永续合约
        new_contracts = get_new_perpetual_contracts(
            exchange_info=exchange_info,
            start_date=start_date,
            end_date=end_date
        )

        # 输出文件路径
        output_path = '/Users/yl/vscode/Binance_quantitative_trading/data/new_coin_listings.json'

        # 保存到JSON文件
        save_to_json(new_contracts, output_path)

        # 打印摘要信息
        print("\n" + "="*80)
        print("最近半年新上线的永续合约交易对")
        print("="*80)
        print(f"查询时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"时间范围: 2025-11-09 至 2026-05-09")
        print(f"交易对数量: {len(new_contracts)}")
        print("-"*80)

        if new_contracts:
            print("\n交易对列表（按上线时间倒序）：\n")
            for i, contract in enumerate(new_contracts, 1):
                print(f"{i}. {contract['symbol']}")
                print(f"   基础资产: {contract['baseAsset']}")
                print(f"   报价资产: {contract['quoteAsset']}")
                print(f"   上线时间: {contract['onboardDateStr']}")
                print(f"   合约状态: {contract['status']}")
                print()
        else:
            print("\n未找到符合条件的交易对")

        print("="*80)
        print(f"\n详细数据已保存到: {output_path}")

    except Exception as e:
        logger.error("执行失败", error=str(e))
        print(f"\n错误: {e}")
        raise


if __name__ == '__main__':
    main()
