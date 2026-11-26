"""原版/激进版共用的分阶段减仓进度，按同一仓位的订单身份恢复。"""
import asyncio
import json
import os

from shared.accounting.account_cutover import account_cutover_ms
from decimal import Decimal

import structlog

logger = structlog.get_logger()
IO_TIMEOUT_SECONDS = 1.0


def _context(strategy):
    """绑定启动时已核验的运行账户、接口模式和模式切点。"""
    account = os.environ.get('TRADE_ACCOUNT_ID', '').strip()
    client = getattr(strategy, 'binance', None)
    logger_account = getattr(getattr(client, 'trade_logger', None), 'account_id', None)
    pm = getattr(client, 'use_unified_account', None)
    if not account or logger_account != account or type(pm) is not bool:
        raise ValueError('减仓进度缺少可信运行账户身份')
    cutover = account_cutover_ms(account)
    if not pm and cutover is None:
        raise ValueError('经典账户缺少模式切点')
    return {'account_id': account, 'mode': 'pm' if pm else 'classic', 'cutover_ms': cutover}


async def load_close_progress(strategy, symbol, position):
    """只恢复同一入场单或同一 TP2 保护单的进度，避免跨仓混用。"""
    if getattr(position, '_close_progress_untrusted', False):
        return False
    # 上次已成交但未存盘时，先补存成功才允许后续软件止盈使用该进度。
    if getattr(position, '_close_progress_dirty', False):
        return await save_close_progress(strategy, symbol, position)
    if getattr(position, '_close_progress_loaded', False):
        return True
    db = getattr(strategy, 'db_manager', None)
    if db is None:
        position._close_progress_loaded = True
        return True
    try:
        row = await asyncio.wait_for(db.fetch_one(
            'SELECT state_data FROM public.strategy_states WHERE strategy_name=$1 AND state_key=$2',
            strategy.instance_id, f'close-progress:{symbol}',
        ), timeout=IO_TIMEOUT_SECONDS)
        if row:
            data = row['state_data']
            if isinstance(data, str):
                data = json.loads(data)
            if data.get('context') != _context(strategy):
                raise ValueError('减仓进度账户、模式时段或方向不匹配')
            matched = any(
                data.get(key) is not None and getattr(position, key, None) is not None
                and str(data[key]) == str(getattr(position, key))
                for key in ('entry_order_id', 'tp2_order_id')
            )
            if (matched and data.get('entry_order_id') is not None
                    and position.entry_order_id is not None
                    and str(data['entry_order_id']) != str(position.entry_order_id)):
                raise ValueError('保护单与入场身份指向不同仓位')
            if matched and data.get('direction') != position.direction:
                raise ValueError('同一仓位减仓进度方向不匹配')
            if not matched and position.entry_order_id is None:
                # 重建旧仓时若保护单也已换号，不能把未知进度当作新仓从零执行。
                logger.warning('旧仓减仓进度身份无法匹配，保留交易所保护并暂停软件止盈', symbol=symbol)
                return False
            if matched and data.get('direction') == position.direction:
                progress = {key: Decimal(str(value)) for key, value in data['confirmed'].items()}
                initial = Decimal(str(data['initial_quantity']))
                if initial <= 0 or not initial.is_finite() or any(
                    not value.is_finite() or value < 0 for value in progress.values()
                ):
                    raise ValueError('减仓进度数值无效')
                pending = data.get('pending_orders', {})
                if not isinstance(pending, dict):
                    raise ValueError('未终态减仓身份无效')
                position.pending_close_orders = pending
                position.close_progress = progress
                position.initial_quantity = initial
        position._close_progress_loaded = True
        return True
    except Exception as exc:
        logger.warning('减仓进度恢复失败，暂缓软件止盈', symbol=symbol, error=str(exc))
        return False


async def save_close_progress(strategy, symbol, position):
    """每笔已确认成交后立即保存，独立状态键不受主心跳覆盖。"""
    if getattr(position, '_close_progress_untrusted', False):
        return False
    db = getattr(strategy, 'db_manager', None)
    if db is None:
        return True
    position._close_progress_dirty = True
    try:
        data = {
            'context': _context(strategy),
            'direction': position.direction,
            'entry_order_id': position.entry_order_id,
            'tp2_order_id': position.tp2_order_id,
            'initial_quantity': str(position.initial_quantity),
            'confirmed': {key: str(value) for key, value in position.close_progress.items()},
            'pending_orders': getattr(position, 'pending_close_orders', {}),
        }
        await asyncio.wait_for(db.execute(
            'INSERT INTO public.strategy_states (strategy_name,state_key,state_data,updated_at) '
            'VALUES ($1,$2,$3,NOW()) ON CONFLICT (strategy_name,state_key) '
            'DO UPDATE SET state_data=$3,updated_at=NOW()',
            strategy.instance_id, f'close-progress:{symbol}', json.dumps(data),
        ), timeout=IO_TIMEOUT_SECONDS)
        position._close_progress_dirty = False
        return True
    except Exception as exc:
        logger.error('减仓进度保存失败，停止本轮重试', symbol=symbol, error=str(exc))
        return False
