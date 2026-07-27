#!/usr/bin/env python3
"""
验证所有交易币种的精度处理

检查各币种的精度信息，并模拟下单参数验证精度调整是否正确。
"""
import asyncio
import sys
import os
import yaml
from pathlib import Path
from decimal import Decimal
import structlog
from dotenv import load_dotenv

# 加载环境变量
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from shared.binance_api import BinanceClient


# 配置日志
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer()
    ]
)

logger = structlog.get_logger()


async def validate_symbol_precision(client: BinanceClient, symbol: str):
    """
    验证单个币种的精度处理

    Args:
        client: 币安客户端
        symbol: 交易对名称

    Returns:
        验证结果字典
    """
    logger.info(f"开始验证 {symbol} 的精度处理")

    result = {
        'symbol': symbol,
        'success': True,
        'errors': [],
        'warnings': [],
        'precision_info': {},
        'test_cases': []
    }

    try:
        # 1. 获取交易对精度信息
        precision_info = await client.get_symbol_info(symbol)
        result['precision_info'] = {
            'quantityPrecision': precision_info.get('quantityPrecision'),
            'pricePrecision': precision_info.get('pricePrecision'),
            'stepSize': precision_info.get('stepSize'),
            'tickSize': str(precision_info.get('tickSize')),
            'minNotional': precision_info.get('minNotional')
        }

        logger.info(
            f"{symbol} 精度信息",
            quantity_precision=precision_info.get('quantityPrecision'),
            price_precision=precision_info.get('pricePrecision'),
            step_size=precision_info.get('stepSize'),
            tick_size=precision_info.get('tickSize'),
            min_notional=precision_info.get('minNotional')
        )

        # 2. 获取当前价格
        current_price = await client.get_ticker_price(symbol)
        logger.info(f"{symbol} 当前价格: {current_price} USDT")

        # 3. 定义测试用例
        test_cases = [
            # (测试名称, 数量, 价格, 期望调整后的数量, 期望调整后的价格)
            ("标准数量", Decimal("0.123456"), None, None, None),
            ("小数量", Decimal("0.001234"), None, None, None),
            ("大数量", Decimal("10.56789"), None, None, None),
            ("标准价格", None, current_price * Decimal("0.95"), None, None),
            ("精确价格", None, current_price, None, None),
            ("带小数价格", None, current_price * Decimal("1.012345"), None, None),
        ]

        step_size = precision_info.get('stepSize', '0.001')
        tick_size = precision_info.get('tickSize', Decimal('0.01'))

        # 4. 执行测试用例
        for test_name, quantity, price, expected_qty, expected_price in test_cases:
            test_result = {
                'name': test_name,
                'quantity_input': str(quantity) if quantity else None,
                'price_input': str(price) if price else None,
                'quantity_adjusted': None,
                'price_adjusted': None,
                'passed': True,
                'error': None
            }

            # 测试数量精度调整
            if quantity is not None:
                adjusted_qty = client._adjust_quantity_precision(quantity, step_size)
                test_result['quantity_adjusted'] = str(adjusted_qty)

                # 验证调整后的数量是stepSize的整数倍
                step = Decimal(step_size)
                remainder = adjusted_qty % step

                if remainder != 0:
                    test_result['passed'] = False
                    test_result['error'] = f"数量 {adjusted_qty} 不是 stepSize {step_size} 的整数倍，余数: {remainder}"
                    result['errors'].append(f"{symbol} {test_name}: {test_result['error']}")

                # 验证调整后的数量不大于原始数量（向下取整）
                if adjusted_qty > quantity:
                    test_result['passed'] = False
                    test_result['error'] = f"数量 {adjusted_qty} 大于原始数量 {quantity}（应向下取整）"
                    result['errors'].append(f"{symbol} {test_name}: {test_result['error']}")

                logger.info(
                    f"{symbol} {test_name} - 数量调整",
                    original=quantity,
                    adjusted=adjusted_qty,
                    step_size=step_size,
                    passed=test_result['passed']
                )

            # 测试价格精度调整
            if price is not None:
                adjusted_price = client._adjust_price_precision(price, tick_size)
                test_result['price_adjusted'] = str(adjusted_price)

                # 验证调整后的价格是tickSize的整数倍
                remainder = adjusted_price % tick_size

                if remainder != 0:
                    test_result['passed'] = False
                    test_result['error'] = f"价格 {adjusted_price} 不是 tickSize {tick_size} 的整数倍，余数: {remainder}"
                    result['errors'].append(f"{symbol} {test_name}: {test_result['error']}")

                logger.info(
                    f"{symbol} {test_name} - 价格调整",
                    original=price,
                    adjusted=adjusted_price,
                    tick_size=tick_size,
                    passed=test_result['passed']
                )

            result['test_cases'].append(test_result)

            if not test_result['passed']:
                result['success'] = False

        # 5. 验证最小下单金额
        min_notional = precision_info.get('minNotional', '5')
        if min_notional:
            logger.info(f"{symbol} 最小下单金额: {min_notional} USDT")

    except Exception as e:
        result['success'] = False
        result['errors'].append(f"验证过程异常: {str(e)}")
        logger.error(f"{symbol} 验证失败", error=str(e), exc_info=True)

    return result


async def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("开始验证所有交易币种的精度处理")
    logger.info("=" * 80)

    # 加载配置
    config_path = project_root / "strategies" / "btc_eth" / "config.yaml"
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    binance_config = config.get('binance', {})

    # 从环境变量获取API密钥
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')

    if not api_key or not api_secret:
        logger.error("未找到币安API密钥，请检查环境变量 BINANCE_API_KEY 和 BINANCE_API_SECRET")
        return

    # 创建币安客户端
    async with BinanceClient(
        api_key=api_key,
        api_secret=api_secret,
        testnet=False,
        use_unified_account=True
    ) as client:
        # 获取配置的交易对列表
        symbols = config.get('strategy', {}).get('symbols', [])

        if not symbols:
            logger.warning("配置文件中未找到交易对列表")
            return

        logger.info(f"待验证的交易对: {', '.join(symbols)}")

        # 验证每个币种
        results = []
        for symbol in symbols:
            result = await validate_symbol_precision(client, symbol)
            results.append(result)

        # 输出验证报告
        logger.info("\n" + "=" * 80)
        logger.info("验证报告")
        logger.info("=" * 80)

        all_passed = True
        for result in results:
            symbol = result['symbol']
            success = result['success']

            if success:
                logger.info(f"✓ {symbol} - 验证通过")
                logger.info(
                    f"  精度信息: "
                    f"数量精度={result['precision_info']['quantityPrecision']}, "
                    f"价格精度={result['precision_info']['pricePrecision']}, "
                    f"stepSize={result['precision_info']['stepSize']}, "
                    f"tickSize={result['precision_info']['tickSize']}"
                )
            else:
                all_passed = False
                logger.error(f"✗ {symbol} - 验证失败")
                for error in result['errors']:
                    logger.error(f"  错误: {error}")

            if result['warnings']:
                for warning in result['warnings']:
                    logger.warning(f"  警告: {warning}")

        logger.info("\n" + "=" * 80)
        if all_passed:
            logger.info("✓ 所有币种精度验证通过")
        else:
            logger.error("✗ 部分币种精度验证失败，请检查错误信息")
        logger.info("=" * 80)

        # 返回退出码
        sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
