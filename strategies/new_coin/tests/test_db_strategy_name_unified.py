"""
new_coin 平仓链路策略名口径修复的功能测试（$1 绑定值统一为 my_record_name）。

被测变更（只读验证，不修改生产代码）：
1. strategy.py 导入 load_ownership_config，并在 __init__ 中缓存
   self.my_record_name = load_ownership_config(self.config)['my_record_name']。
2. 4 处 DB 查询的 $1 绑定值由 config.db_strategy_name（配置中从未定义，恒为中文旧
   显示名「新币做空策略」）替换为 self.my_record_name，SQL 文本与参数个数/顺序不变。

覆盖验收条件（对应 fix-2026-10-15 需求文档 §6.3/§6.5）：
- AC1：构造期 my_record_name 取 strategy.record_name 有值分支。
- AC5：record_name 缺失时回退 strategy.name，不抛异常且记 warning。
- AC4：_weekly_review 查询 $1 == my_record_name，且有记录时不走「无记录」早退分支。
- AC3：_monitor_positions 的「补正止损打标」「平仓回写」两分支查询 $1 == my_record_name
  且 SQL 结构保持（WHERE strategy = $1 / LIMIT 1）。
- 附带：_backfill_close_pnl 查询 $1 == my_record_name（4 处替换点全部覆盖）。
- 哨兵用例：证明 $1 绑定值运行期确实取自 self.my_record_name 属性，而非凑巧等值。

fixture 约定：所有异步依赖一律 AsyncMock（严禁 MagicMock 直接 await）；
db 查询不 mock 掉被断言的调用本身，只 mock 返回值。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import shared.position_ownership as position_ownership
from strategies.new_coin.strategy import NewCoinStrategy

# 测试约定写库名（与 config.yaml record_name / main.py TradeLogger 第二参一致）
MY_RECORD_NAME = 'new_coin'


def make_strategy_shell() -> NewCoinStrategy:
    """构造绕过 __init__ 的策略实例，注入 4 处查询共用的最小属性集。"""
    strategy = object.__new__(NewCoinStrategy)
    strategy.config = {'strategy': {'name': 'new_coin', 'record_name': MY_RECORD_NAME}}
    strategy.my_record_name = MY_RECORD_NAME
    strategy.positions = {}
    strategy.consecutive_losses = 0
    return strategy


def wire_monitor_deps(strategy: NewCoinStrategy) -> None:
    """注入 _monitor_positions 依赖的异步/同步桩（全部显式声明，避免 MagicMock 被误 await）。"""
    # 交易执行器：持仓管理与取消条件单均为空操作
    executor = MagicMock()
    executor.check_position_management = AsyncMock()
    executor._update_short_position_closed = AsyncMock()
    executor.cancel_all_algo_orders = AsyncMock(return_value={'failed': 0})
    executor.clear_position_tracking = MagicMock()
    strategy.trading_executor = executor

    # 数据库：返回值由各用例自行配置
    strategy.db = MagicMock()
    strategy.db.fetch_one = AsyncMock()

    # 币安客户端 + trade_logger（打标与盈亏回写桩）
    binance_client = MagicMock()
    binance_client.get_position = AsyncMock(return_value=[])
    trade_logger = MagicMock()
    trade_logger.log_stop_loss = AsyncMock()
    trade_logger.update_realized_pnl = AsyncMock()
    binance_client.trade_logger = trade_logger
    strategy.binance_client = binance_client

    # 打桩内部辅助方法，聚焦被测分支
    strategy._get_position_pnl = AsyncMock()
    strategy._check_max_drawdown = AsyncMock()
    strategy._check_consecutive_loss_pause = AsyncMock()
    strategy._add_to_stop_loss_monitor = AsyncMock()
    strategy._save_state = AsyncMock()


def make_old_position_entry() -> dict:
    """构造一笔 2 小时前入场的做空持仓（避开 60 秒入场防抖分支）。"""
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    return {'entry_price': Decimal('100'), 'entry_time': long_ago}


def test_ac1_构造期_my_record_name取record_name有值分支():
    """config 提供 record_name 时，真实 __init__ 缓存的 my_record_name 应等于 record_name。"""
    config = {'strategy': {'name': 'new_coin', 'record_name': MY_RECORD_NAME}}

    strategy = NewCoinStrategy(config)  # 真实 __init__，构造期不应有外部 IO

    assert strategy.my_record_name == MY_RECORD_NAME


def test_ac5_构造期_record_name缺失回退strategy_name且记warning不抛异常(monkeypatch):
    """record_name 缺失时应回退 strategy.name、不抛异常，并记录一条 warning。"""
    fake_logger = MagicMock()
    monkeypatch.setattr(position_ownership, 'logger', fake_logger)
    config = {'strategy': {'name': 'new_coin'}}  # 故意缺失 record_name

    strategy = NewCoinStrategy(config)  # 不应抛异常

    assert strategy.my_record_name == 'new_coin'
    fake_logger.warning.assert_called_once()  # 回退必须显式告警，禁止静默


async def test_ac4_周报查询绑定1为my_record_name且有记录时发送周报():
    """_weekly_review：$1 == my_record_name；有记录时应发送周报（不走无记录早退分支）。"""
    strategy = make_strategy_shell()
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(
        return_value=[{'realized_pnl': Decimal('10.5')}, {'realized_pnl': Decimal('-4.2')}]
    )
    strategy.notification_client = MagicMock()
    strategy.notification_client.send = AsyncMock()

    await strategy._weekly_review()

    strategy.db.fetch_all.assert_awaited_once()
    sql, bind_value = strategy.db.fetch_all.await_args.args[0], strategy.db.fetch_all.await_args.args[1]
    assert bind_value == MY_RECORD_NAME  # SQL 的第一个位置参数（$1）
    assert 'WHERE strategy = $1' in sql  # SQL 文本与参数顺序不变
    # 拿到记录后未走「本周无交易记录，跳过复盘」早退分支的证据：周报已发送
    strategy.notification_client.send.assert_awaited_once()
    assert '【新币做空策略周报】' in strategy.notification_client.send.await_args.kwargs['message']


async def test_ac3_补正止损打标分支_查询绑定1为my_record_name且SQL含LIMIT1():
    """pnl=None 走补正打标路径：fetch_one 的 $1 == my_record_name，SQL 保持 LIMIT 1。"""
    strategy = make_strategy_shell()
    wire_monitor_deps(strategy)
    strategy.db.fetch_one = AsyncMock(
        return_value={'order_id': 'CLOSE123', 'realized_pnl': Decimal('-8.5')}
    )
    strategy._get_position_pnl = AsyncMock(return_value=None)  # 触发补正打标分支
    symbol = 'NEWCOINUSDT'
    strategy.positions[symbol] = make_old_position_entry()

    await strategy._monitor_positions()

    strategy.db.fetch_one.assert_awaited_once()
    sql, bind_value = strategy.db.fetch_one.await_args.args[0], strategy.db.fetch_one.await_args.args[1]
    assert bind_value == MY_RECORD_NAME  # SQL 的第一个位置参数（$1）
    assert 'WHERE strategy = $1' in sql
    assert 'LIMIT 1' in sql
    # 分支真实执行到底的证据：亏损记录触发补正打标并累加连续亏损计数
    strategy.binance_client.trade_logger.log_stop_loss.assert_awaited_once()
    assert strategy.consecutive_losses == 1


async def test_ac3_平仓回写分支_查询绑定1为my_record_name且SQL含LIMIT1():
    """pnl 非 None 走平仓回写路径：fetch_one 的 $1 == my_record_name，SQL 保持 LIMIT 1。"""
    strategy = make_strategy_shell()
    wire_monitor_deps(strategy)
    strategy.db.fetch_one = AsyncMock(
        return_value={'order_id': 'CLOSE456', 'executed_at': datetime.now()}
    )
    strategy._get_position_pnl = AsyncMock(return_value=Decimal('15.5'))  # 盈利平仓
    symbol = 'NEWCOINUSDT'
    strategy.positions[symbol] = make_old_position_entry()

    await strategy._monitor_positions()

    strategy.db.fetch_one.assert_awaited_once()
    sql, bind_value = strategy.db.fetch_one.await_args.args[0], strategy.db.fetch_one.await_args.args[1]
    assert bind_value == MY_RECORD_NAME  # SQL 的第一个位置参数（$1）
    assert 'WHERE strategy = $1' in sql
    assert 'LIMIT 1' in sql
    # 分支真实执行到底的证据：拿到平仓单后回写盈亏被调用
    strategy.binance_client.trade_logger.update_realized_pnl.assert_awaited_once()


async def test_backfill_close_pnl_查询绑定1为my_record_name():
    """_backfill_close_pnl：$1 == my_record_name，SQL 保持 LIMIT 1，命中后回写盈亏。"""
    strategy = make_strategy_shell()
    strategy.db = MagicMock()
    strategy.db.fetch_one = AsyncMock(
        return_value={'order_id': 'CLOSE789', 'executed_at': datetime.now()}
    )
    binance_client = MagicMock()
    binance_client.trade_logger = MagicMock()
    binance_client.trade_logger.update_realized_pnl = AsyncMock()
    strategy.binance_client = binance_client

    await strategy._backfill_close_pnl(
        'NEWCOINUSDT', (datetime.now() - timedelta(hours=1)).isoformat(), Decimal('-5')
    )

    strategy.db.fetch_one.assert_awaited_once()
    sql, bind_value = strategy.db.fetch_one.await_args.args[0], strategy.db.fetch_one.await_args.args[1]
    assert bind_value == MY_RECORD_NAME  # SQL 的第一个位置参数（$1）
    assert 'WHERE strategy = $1' in sql
    assert 'LIMIT 1' in sql
    strategy.binance_client.trade_logger.update_realized_pnl.assert_awaited_once()


async def test_查询绑定值与my_record_name属性恒等_哨兵值验证():
    """哨兵用例：$1 绑定值运行期取自 self.my_record_name 属性，而非任何其他硬编码来源。"""
    strategy = make_strategy_shell()
    strategy.my_record_name = 'sentinel_record_name'  # 注入非默认哨兵值
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[{'realized_pnl': Decimal('1')}])
    strategy.notification_client = MagicMock()
    strategy.notification_client.send = AsyncMock()

    await strategy._weekly_review()

    assert strategy.db.fetch_all.await_args.args[1] == 'sentinel_record_name'
