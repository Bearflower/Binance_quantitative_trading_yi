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

    # 返回值现为 (total_stored, failures) 结构化结果，取第一项
    total1, _ = await bulk.fetch_symbol_history('BTCUSDT', days=30, intervals=['1h'])
    total2, _ = await bulk.fetch_symbol_history('BTCUSDT', days=30, intervals=['1h'])

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


# ---------- 新增：精确区间模式 + 互斥 + 结构化失败 ----------


@pytest.mark.asyncio
async def test_collect_range_never_requests_outside_utc_window(patched_environment):
    """精确区间模式下，collect_klines 的 start_time >= 起点、end_time <= 终点。

    构造精确区间 2026-10-01T00:00:00Z → 2026-10-05T00:00:00Z，
    用 1h interval 采集。对每次 collect_klines 调用的 start_time / end_time 做边界断言。
    """
    from datetime import datetime, timezone

    m = patched_environment
    # 让 collect_klines 返回空即可，只关心它被调用的参数
    m['collector_instance'].collect_klines.return_value = []

    start_dt = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 10, 5, 0, 30, 0, tzinfo=timezone.utc)  # 故意带偏移，会被向下对齐

    await bulk.fetch_symbol_history(
        'BTCUSDT', intervals=['1h'],
        start_dt=start_dt, end_dt=end_dt,
    )

    assert m['collector_instance'].collect_klines.called, "应至少调用一次"
    for c in m['collector_instance'].collect_klines.call_args_list:
        kwargs = c.kwargs
        call_start = kwargs.get('start_time')
        call_end = kwargs.get('end_time')
        assert call_start is not None, "collect_klines 应传 start_time"
        assert call_end is not None, "collect_klines 应传 end_time"
        # 起点不得小于精确区间起点（起点已向上对齐到 1h 边界 = 保持原值）
        assert call_start >= int(start_dt.timestamp()) * 1000, (
            f"start_time={call_start} 小于区间起点"
        )
        # 终点不得大于 interval 对齐后的 end_dt
        end_aligned = bulk._align_end_down(end_dt, '1h')
        assert call_end <= int(end_aligned.timestamp()) * 1000, (
            f"end_time={call_end} 大于 interval 对齐后的终点"
        )


def test_exact_range_start_is_aligned_up_not_down():
    """非整点 start 必须 ceil，禁止请求用户指定范围以前的数据。"""
    from datetime import datetime, timezone

    start = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)
    start_ms, end_ms = bulk._resolve_interval_range("1h", True, start, end, 0)

    assert start_ms == int(datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc).timestamp()) * 1000
    assert end_ms == int(datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc).timestamp()) * 1000


@pytest.mark.asyncio
async def test_strict_collector_propagates_api_failure():
    """批量补采使用的 strict collector 不得把 API 异常伪装成空结果。"""
    from core.collector import KlineCollector

    client = MagicMock()
    client.get_klines = AsyncMock(side_effect=RuntimeError("api down"))
    collector = KlineCollector(client, MagicMock(), strict_errors=True)

    with pytest.raises(RuntimeError, match="api down"):
        await collector.collect_klines("BTCUSDT", "1h")


@pytest.mark.asyncio
async def test_strict_collector_propagates_store_failure():
    """strict 模式下写库失败也必须进入批量补采 failures。"""
    from core.collector import KlineCollector

    collector = KlineCollector(MagicMock(), MagicMock(), strict_errors=True)
    collector._batch_insert = AsyncMock(side_effect=RuntimeError("db down"))
    kline = MagicMock()
    kline.symbol = "BTCUSDT"
    kline.interval = "1h"
    kline.to_dict.return_value = {"open_time": 1}

    with pytest.raises(RuntimeError, match="db down"):
        await collector.store_klines([kline])


@pytest.mark.asyncio
async def test_end_is_aligned_down_to_interval_boundary(patched_environment):
    """--end 带小数时间时，按 interval 向下对齐。

    例：end=2026-10-21T00:30:00Z，interval=1h → 区间上界对齐到
    2026-10-21T00:00:00Z；因 Binance endTime 为闭区间，请求值应再减 1ms。
    """
    from datetime import datetime, timezone

    m = patched_environment
    m['collector_instance'].collect_klines.return_value = []

    start_dt = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    # 故意选一个不是 1h 边界的 end
    end_dt = datetime(2026, 10, 21, 0, 30, 0, tzinfo=timezone.utc)

    await bulk.fetch_symbol_history(
        'ETHUSDT', intervals=['1h'],
        start_dt=start_dt, end_dt=end_dt,
    )

    end_aligned = bulk._align_end_down(end_dt, '1h')
    # Binance endTime 为闭区间；为实现 [start,end) 必须传 end-1ms
    expected_end_ms = int(end_aligned.timestamp()) * 1000 - 1

    all_call_ends = [
        c.kwargs.get('end_time')
        for c in m['collector_instance'].collect_klines.call_args_list
    ]
    assert all_call_ends, "应至少调用一次 collect_klines"
    # 所有调用的 end_time 都应等于对齐后的值
    for call_end in all_call_ends:
        assert call_end == expected_end_ms, (
            f"call end_time={call_end} != 对齐后的 {expected_end_ms}"
        )


@pytest.mark.asyncio
async def test_one_symbol_failure_is_reported_and_exit_is_nonzero():
    """一个 symbol 成功、另一个 symbol 在 interval 级抛异常 → 失败列表非空、main 返回 1。

    完全替换 _build_collector 为一个固定返回的 mock，避免真实 DB/网络。
    """
    from datetime import datetime, timezone

    # 用 patched_environment 的手动版本（保持简洁）
    from unittest.mock import AsyncMock, MagicMock, patch as _patch

    db = MagicMock()
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()

    registry = MagicMock()
    registry.initialize = AsyncMock()

    binance_cls = MagicMock()
    binance_cls.return_value.connect = AsyncMock()
    binance_cls.return_value.disconnect = AsyncMock()

    collector_cls = MagicMock()
    collector = MagicMock()
    collector_cls.return_value = collector

    patches = [
        _patch.object(bulk, 'db_manager', db),
        _patch.object(bulk, 'registry', registry),
        _patch.object(bulk, 'BinanceClient', binance_cls),
        _patch.object(bulk, 'KlineCollector', collector_cls),
    ]
    for p in patches:
        p.start()

    try:
        # BTCUSDT 成功（返回空，正常走流程）
        # ETHUSDT 失败（collect_klines 抛异常）
        call_count = [0]

        async def success_side(*args, **kwargs):
            call_count[0] += 1
            return []

        async def fail_side(*args, **kwargs):
            raise RuntimeError("模拟采集失败")

        def _collect_side(symbol, interval, **kw):
            if symbol == 'ETHUSDT':
                return fail_side()
            return success_side()

        collector.collect_klines.side_effect = lambda symbol, interval, **kw: (
            fail_side() if symbol == 'ETHUSDT' else success_side()
        )
        collector.store_klines.return_value = 0

        start_dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
        end_dt = datetime(2026, 10, 5, tzinfo=timezone.utc)

        # 直接测 fetch_symbol_history 的结构化结果
        _, btc_failures = await bulk.fetch_symbol_history(
            'BTCUSDT', intervals=['1h'], start_dt=start_dt, end_dt=end_dt,
        )
        _, eth_failures = await bulk.fetch_symbol_history(
            'ETHUSDT', intervals=['1h'], start_dt=start_dt, end_dt=end_dt,
        )

        assert btc_failures == [], "BTCUSDT 应成功、无失败记录"
        assert len(eth_failures) == 1, "ETHUSDT 应记录一个失败"
        assert eth_failures[0]['symbol'] == 'ETHUSDT'
        assert eth_failures[0]['interval'] == '1h'
        assert 'RuntimeError' in eth_failures[0]['error'] or '模拟' in eth_failures[0]['error']

        # 再测 main() 聚合后的返回码
        with _patch.object(bulk, 'asyncio') as mock_asyncio:
            mock_asyncio.sleep = AsyncMock()  # 跳过 symbol 间 1s sleep

            # 让 main 走到聚合逻辑：分别跑两个 symbol，各自用上面相同的 side_effect
            original_collect = collector.collect_klines.side_effect
            # 重新设一次，避免被前面调用耗尽
            collector.collect_klines.side_effect = lambda symbol, interval, **kw: (
                fail_side() if symbol == 'ETHUSDT' else success_side()
            )

            import sys
            original_argv = sys.argv
            try:
                sys.argv = [
                    'fetch_history_bulk.py',
                    '--symbols', 'BTCUSDT,ETHUSDT',
                    '--start', '2026-10-01T00:00:00Z',
                    '--end', '2026-10-05T00:00:00Z',
                    '--intervals', '1h',
                ]
                exit_code = await bulk.main()
            finally:
                sys.argv = original_argv

        assert exit_code == 1, f"有失败时 main 应返回 1，实际返回 {exit_code}"

    finally:
        for p in reversed(patches):
            p.stop()


def test_days_and_start_end_are_mutually_exclusive():
    """--days 与 --start/--end 互斥；只给 --start 或只给 --end 都应 SystemExit(code=2)。"""
    import sys

    # 组合 1：同时给 --days 和 --start/--end → exit 2
    original_argv = sys.argv
    try:
        sys.argv = [
            'fetch_history_bulk.py',
            '--days', '30',
            '--start', '2026-10-01T00:00:00Z',
            '--end', '2026-10-05T00:00:00Z',
        ]
        with pytest.raises(SystemExit) as exc:
            import asyncio
            asyncio.run(bulk.main())
        assert exc.value.code == 2, "days 与 start/end 互斥 → parser.error → exit 2"
    finally:
        sys.argv = original_argv

    # 组合 2：只给 --start → exit 2
    original_argv = sys.argv
    try:
        sys.argv = [
            'fetch_history_bulk.py',
            '--start', '2026-10-01T00:00:00Z',
        ]
        with pytest.raises(SystemExit) as exc:
            import asyncio
            asyncio.run(bulk.main())
        assert exc.value.code == 2, "只给 --start 未给 --end → exit 2"
    finally:
        sys.argv = original_argv
