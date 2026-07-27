#!/usr/bin/env python3
"""
验证所有交易币种的精度处理（离线版本）

基于币安官方文档的精度信息，验证精度调整逻辑是否正确。
"""
from decimal import Decimal
import structlog

# 配置日志
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer()
    ]
)

logger = structlog.get_logger()


# 币安合约交易对精度信息（基于官方文档）
# 数据来源：https://binance-docs.github.io/apidocs/futures/cn/
SYMBOL_PRECISION_INFO = {
    "BTCUSDT": {
        "quantityPrecision": 3,
        "pricePrecision": 1,
        "stepSize": "0.001",
        "tickSize": Decimal("0.1"),
        "minNotional": "5",
        "current_price": Decimal("104000"),  # 大约价格
    },
    "ETHUSDT": {
        "quantityPrecision": 3,
        "pricePrecision": 2,
        "stepSize": "0.001",
        "tickSize": Decimal("0.01"),
        "minNotional": "5",
        "current_price": Decimal("2400"),  # 大约价格
    },
    "BNBUSDT": {
        "quantityPrecision": 2,
        "pricePrecision": 2,
        "stepSize": "0.01",
        "tickSize": Decimal("0.01"),
        "minNotional": "5",
        "current_price": Decimal("650"),  # 大约价格
    },
    "SOLUSDT": {
        "quantityPrecision": 2,
        "pricePrecision": 2,
        "stepSize": "0.01",
        "tickSize": Decimal("0.01"),
        "minNotional": "5",
        "current_price": Decimal("170"),  # 大约价格
    },
    "XRPUSDT": {
        "quantityPrecision": 0,
        "pricePrecision": 4,
        "stepSize": "1",
        "tickSize": Decimal("0.0001"),
        "minNotional": "5",
        "current_price": Decimal("2.3"),  # 大约价格
    },
    "TRXUSDT": {
        "quantityPrecision": 0,
        "pricePrecision": 5,
        "stepSize": "1",
        "tickSize": Decimal("0.00001"),
        "minNotional": "5",
        "current_price": Decimal("0.35"),  # 大约价格
    },
}


def adjust_quantity_precision(quantity: Decimal, step_size: str) -> Decimal:
    """
    调整数量精度（向下取整到stepSize的整数倍）

    注意：数量必须向下取整，避免超出账户余额

    Args:
        quantity: 原始数量
        step_size: 步长（如 '0.001'）

    Returns:
        调整后的数量
    """
    if not step_size or step_size == '0':
        return quantity

    step = Decimal(step_size)
    # 向下取整到stepSize的整数倍
    adjusted = (quantity // step) * step

    return adjusted


def adjust_price_precision(price: Decimal, tick_size: Decimal) -> Decimal:
    """
    调整价格精度（四舍五入到tickSize的整数倍）

    注意：价格应该四舍五入到最近的tickSize整数倍

    Args:
        price: 原始价格
        tick_size: 价格步长

    Returns:
        调整后的价格
    """
    if not tick_size or tick_size == 0:
        return price

    # 四舍五入到tickSize的整数倍
    # 使用 Decimal 的 quantize 方法进行精确的四舍五入
    adjusted = (price / tick_size).quantize(Decimal('1'), rounding='ROUND_HALF_UP') * tick_size

    return adjusted


def validate_symbol_precision(symbol: str, precision_info: dict) -> dict:
    """
    验证单个币种的精度处理

    Args:
        symbol: 交易对名称
        precision_info: 精度信息字典

    Returns:
        验证结果字典
    """
    logger.info(f"开始验证 {symbol} 的精度处理")

    result = {
        'symbol': symbol,
        'success': True,
        'errors': [],
        'warnings': [],
        'precision_info': precision_info,
        'test_cases': []
    }

    try:
        step_size = precision_info['stepSize']
        tick_size = precision_info['tickSize']
        current_price = precision_info['current_price']

        # 定义测试用例
        test_cases = [
            # (测试名称, 数量, 价格)
            ("标准数量", Decimal("0.123456"), None),
            ("小数量", Decimal("0.001234"), None),
            ("大数量", Decimal("10.56789"), None),
            ("标准价格", None, current_price * Decimal("0.95")),
            ("精确价格", None, current_price),
            ("带小数价格", None, current_price * Decimal("1.012345")),
        ]

        # 执行测试用例
        for test_name, quantity, price in test_cases:
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
                adjusted_qty = adjust_quantity_precision(quantity, step_size)
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
                adjusted_price = adjust_price_precision(price, tick_size)
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

    except Exception as e:
        result['success'] = False
        result['errors'].append(f"验证过程异常: {str(e)}")
        logger.error(f"{symbol} 验证失败", error=str(e), exc_info=True)

    return result


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("开始验证所有交易币种的精度处理（离线版本）")
    logger.info("=" * 80)

    # 验证每个币种
    results = []
    for symbol, precision_info in SYMBOL_PRECISION_INFO.items():
        result = validate_symbol_precision(symbol, precision_info)
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


if __name__ == "__main__":
    main()
