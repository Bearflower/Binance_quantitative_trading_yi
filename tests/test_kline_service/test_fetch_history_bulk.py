"""测试 fetch_history_bulk.py 历史补采脚本"""

import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path

# 确保 services/kline_service 在 sys.path（脚本运行时也依赖此路径）
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_KLINE_SERVICE = str(_PROJECT_ROOT / "services" / "kline_service")
if _KLINE_SERVICE not in sys.path:
    sys.path.insert(0, _KLINE_SERVICE)

# 导入被测脚本
import services.kline_service.scripts.fetch_history_bulk as bulk


# ---------- 测试 INTERVAL_DAYS 常量 ----------


def test_interval_days_1d_cap_is_200():
    """INTERVAL_DAYS['1d'] 必须为 200 天"""
    assert bulk.INTERVAL_DAYS['1d'] == 200


# ---------- 测试 _parse_intervals ----------


def test_parse_intervals_filters_to_valid_keys():
    """合法 intervals 原样返回"""
    assert bulk._parse_intervals('1h,4h,1d') == ['1h', '4h', '1d']


def test_parse_intervals_strips_whitespace():
    """每个 interval 会去除前后空白"""
    assert bulk._parse_intervals(' 1h , 4h ') == ['1h', '4h']


def test_parse_intervals_empty_and_blank_filtered():
    """空字符串和全空白项被过滤掉；仍有非空有效项则正常返回"""
    result = bulk._parse_intervals('1h,, ,4h')
    assert result == ['1h', '4h']


def test_parse_intervals_all_blank_exits_nonzero():
    """全空白输入（如 ',,,' 或 ' , , '）应 exit(2)，
    而非静默返回 [] 导致后续 '采集 0 条' 的漏采假象。"""
    for raw in [',,,', ' , , ', '  ', '']:
        with pytest.raises(SystemExit) as exc:
            bulk._parse_intervals(raw)
        assert exc.value.code == 2, f"input={raw!r} expected exit code 2"


def test_parse_intervals_deduplicates_preserving_order():
    """重复 interval 去重且保持首次出现顺序（保序去重）"""
    assert bulk._parse_intervals('1h,1h,4h,1h,1d,4h') == ['1h', '4h', '1d']


def test_parse_intervals_unknown_exits_nonzero():
    """无法识别的 interval 必须报错并以非零码退出。

    选择「立即退出」而非告警跳过：用户显式指定 intervals 时，
    静默跳过非法值会造成"命令看似成功但实际漏采"的假象。
    """
    with pytest.raises(SystemExit) as exc:
        bulk._parse_intervals('1h,99x')
    assert exc.value.code == 2


# ---------- 测试 fetch_symbol_history（主流程） ----------


def _make_kline_batch(symbol, interval, start_open_time, count):
    """构造一批 KlineData，open_time 连续 + 1ms，close_time 对应结束时间"""
    from services.kline_service.models.kline import KlineData
    period_ms = {
        '1m': 60_000, '5m': 300_000, '15m': 900_000,
        '1h': 3600_000, '4h': 14400_000, '1d': 86400_000,
    }[interval]
    klines = []
    for i in range(count):
        open_t = start_open_time + i * period_ms
        klines.append(KlineData(
            symbol=symbol, interval=interval,
            open_time=open_t, close_time=open_t + period_ms - 1,
            open_price=100.0, high_price=105.0, low_price=95.0,
            close_price=102.0, volume=1.0, quote_volume=100.0,
            trade_count=10, taker_buy_volume=0.5, taker_buy_quote_volume=50.0,
        ))
    return klines


def _make_side_effect(count):
    """构造一个固定返回 count 条闭合 K 线的 collect_klines side_effect。

    用 **kwargs 吸收未使用的 end_time/limit 等关键字参数，
    既消除幽灵参数警告，又保持与被测函数关键字调用的签名兼容。
    """
    def side_effect(symbol, interval, start_time=None, **kwargs):
        return _make_kline_batch(symbol, interval, start_time, count)
    return side_effect


@pytest.fixture
def patched_environment():
    """patch 所有外部依赖，返回便于断言的 mock 收集器"""
    mocks = {}

    # db_manager：连接/断开
    mocks['db'] = MagicMock()
    mocks['db'].connect = AsyncMock()
    mocks['db'].disconnect = AsyncMock()
    mocks['db'].get_connection = MagicMock()

    # registry：initialize
    mocks['registry'] = MagicMock()
    mocks['registry'].initialize = AsyncMock()

    # BinanceClient
    mocks['binance_cls'] = MagicMock()
    mocks['binance_instance'] = MagicMock()
    mocks['binance_instance'].connect = AsyncMock()
    mocks['binance_instance'].disconnect = AsyncMock()
    mocks['binance_cls'].return_value = mocks['binance_instance']

    # KlineCollector
    mocks['collector_cls'] = MagicMock()
    mocks['collector_instance'] = MagicMock()
    mocks['collector_instance'].collect_klines = AsyncMock()
    mocks['collector_instance'].store_klines = AsyncMock()
    mocks['collector_cls'].return_value = mocks['collector_instance']

    patches = [
        patch.object(bulk, 'db_manager', mocks['db']),
        patch.object(bulk, 'registry', mocks['registry']),
        patch.object(bulk, 'BinanceClient', mocks['binance_cls']),
        patch.object(bulk, 'KlineCollector', mocks['collector_cls']),
    ]

    for p in patches:
        p.start()
    yield mocks
    for p in reversed(patches):
        p.stop()


@pytest.mark.asyncio
async def test_fetch_symbol_history_initializes_registry(patched_environment):
    """fetch_symbol_history 必须在连接 DB 后调用 registry.initialize()"""
    m = patched_environment

    # collect_klines 返回空，让流程快速结束
    m['collector_instance'].collect_klines.return_value = []

    await bulk.fetch_symbol_history('XAUUSDT', days=1, intervals=['1d'])

    m['db'].connect.assert_awaited_once()
    m['registry'].initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_symbol_history_respects_intervals_filter(patched_environment):
    """只采集用户指定的 interval，忽略 INTERVAL_DAYS 中其余周期"""
    m = patched_environment

    m['collector_instance'].collect_klines.side_effect = _make_side_effect(500)
    m['collector_instance'].store_klines.return_value = 500

    await bulk.fetch_symbol_history('XAUUSDT', days=30, intervals=['1h', '4h', '1d'])

    # extract 所有传给 collect_klines 的 interval 参数
    called_intervals = sorted({
        c.kwargs.get('interval') or (c.args[1] if len(c.args) > 1 else None)
        for c in m['collector_instance'].collect_klines.call_args_list
    })
    assert called_intervals == ['1d', '1h', '4h']


@pytest.mark.asyncio
async def test_fetch_symbol_history_all_intervals_when_none(patched_environment):
    """intervals=None 时采集 INTERVAL_DAYS 全部周期"""
    m = patched_environment

    m['collector_instance'].collect_klines.side_effect = _make_side_effect(500)
    m['collector_instance'].store_klines.return_value = 500

    await bulk.fetch_symbol_history('BTCUSDT', days=30, intervals=None)

    called_intervals = sorted({
        c.kwargs.get('interval') or (c.args[1] if len(c.args) > 1 else None)
        for c in m['collector_instance'].collect_klines.call_args_list
    })
    assert set(called_intervals) == set(bulk.INTERVAL_DAYS.keys())


@pytest.mark.asyncio
async def test_pagination_advances_by_last_close_time_plus_one(patched_environment):
    """分页推进：第二次 collect_klines 的 start_time 等于第一批最后一根 close_time + 1。

    选择 1m interval（1 天 = 1440 根 > BATCH_LIMIT=1000）制造跨页场景，
    1d/4h/1h 单天范围不够分两批。
    """
    m = patched_environment

    batch_count = 0
    first_batch_last_close = None

    def side_effect(symbol, interval, start_time=None, end_time=None, limit=None):
        nonlocal batch_count, first_batch_last_close
        batch_count += 1
        if batch_count == 1:
            batch = _make_kline_batch(symbol, interval, start_time, bulk.BATCH_LIMIT)
            first_batch_last_close = batch[-1].close_time
            return batch
        # 第二批返回不足 BATCH_LIMIT，触发终止
        return _make_kline_batch(symbol, interval, start_time, 440)

    m['collector_instance'].collect_klines.side_effect = side_effect
    m['collector_instance'].store_klines.return_value = bulk.BATCH_LIMIT

    # days=1, interval=1m → actual_days=1, 共 1440 根，需要分两批
    await bulk.fetch_symbol_history('BTCUSDT', days=1, intervals=['1m'])

    assert batch_count >= 2, "分页推进测试需要至少两次 collect_klines 调用"
    second_call = m['collector_instance'].collect_klines.call_args_list[1]
    second_start = second_call.kwargs.get('start_time') or second_call.args[2]
    assert second_start == first_batch_last_close + 1


@pytest.mark.asyncio
async def test_unclosed_klines_not_stored(patched_environment):
    """close_time >= 当前时间的未闭合 K 线不得入库"""
    m = patched_environment

    now_ms = int(__import__('datetime').datetime.now().timestamp() * 1000)
    from services.kline_service.models.kline import KlineData

    closed_kline = KlineData(
        symbol='BTCUSDT', interval='1h',
        open_time=now_ms - 7200_000, close_time=now_ms - 3600_000 - 1,
        open_price=100.0, high_price=105.0, low_price=95.0,
        close_price=102.0, volume=1.0, quote_volume=100.0,
        trade_count=10, taker_buy_volume=0.5, taker_buy_quote_volume=50.0,
    )
    # close_time 在未来 1 分钟（未闭合）
    unclosed_kline = KlineData(
        symbol='BTCUSDT', interval='1h',
        open_time=now_ms - 3600_000, close_time=now_ms + 60_000,
        open_price=102.0, high_price=108.0, low_price=98.0,
        close_price=105.0, volume=2.0, quote_volume=200.0,
        trade_count=20, taker_buy_volume=1.0, taker_buy_quote_volume=100.0,
    )

    m['collector_instance'].collect_klines.return_value = [closed_kline, unclosed_kline]
    m['collector_instance'].store_klines.return_value = 1  # 只存闭合的那一条

    await bulk.fetch_symbol_history('BTCUSDT', days=1, intervals=['1h'])

    # store_klines 只收到闭合的 K 线
    m['collector_instance'].store_klines.assert_called_once()
    stored_arg = m['collector_instance'].store_klines.call_args.args[0]
    assert len(stored_arg) == 1
    assert stored_arg[0].close_time < now_ms


@pytest.mark.asyncio
async def test_idempotent_repeated_runs_same_range_produce_no_duplicates(patched_environment):
    """对同一时间范围补采两次，store_klines 入库行数应不增加。

    幂等来自 collector.store_klines 内部 ON CONFLICT (open_time) DO NOTHING，
    此处断言脚本传给 store_klines 的 K 线全部满足闭合条件（已由 _filter_unclosed
    过滤），从而保证相同 open_time 不会被重复入库。
    """
    m = patched_environment

    m['collector_instance'].collect_klines.side_effect = _make_side_effect(200)
    # store_klines 第一次返回实际条数，第二次因为 ON CONFLICT DO NOTHING 返回 0
    store_counts = iter([200, 0])
    m['collector_instance'].store_klines.side_effect = lambda kl: next(store_counts)

    total1 = await bulk.fetch_symbol_history('BTCUSDT', days=30, intervals=['1h'])
    total2 = await bulk.fetch_symbol_history('BTCUSDT', days=30, intervals=['1h'])

    assert total1 == 200
    assert total2 == 0  # 重复范围不会产生新行


@pytest.mark.asyncio
async def test_fetch_symbol_history_skips_empty_interval_cap(patched_environment):
    """days 小于 interval cap 时 actual_days 取 min，days=0 时跳过该 interval"""
    m = patched_environment

    m['collector_instance'].collect_klines.return_value = []
    await bulk.fetch_symbol_history('BTCUSDT', days=0, intervals=['1h', '4h', '1d'])

    # collect_klines 不应被调用（actual_days <= 0 时 continue）
    m['collector_instance'].collect_klines.assert_not_called()
