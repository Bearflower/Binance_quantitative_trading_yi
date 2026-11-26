"""Executor-owned evidence for signal pushes; no exchange writes or advice actions.

The latest receipt is an atomic metadata projection in the existing account-bound
journal. It is matched by decision ID, never treated as permission to trade.
"""
import json
import time
from decimal import Decimal

from shared.notification_time import beijing_time

RECEIPT_KEY = 'signal_execution_receipt_v1'
PENDING_TEXT = '实盘资金与下单校验：待确认（尚无本次有效执行回执）'


def read_receipt(store):
    row = store.conn.execute('SELECT value FROM schema_meta WHERE key=?',
                             (RECEIPT_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def last_reduction(store):
    # EXIT is reduceOnly; STOP is closePosition. A prepared/cancelled order is
    # not a fill. Keep the timestamp explicitly labelled as journal time.
    row = store.conn.execute('''SELECT f.trade_id,f.exchange_order_id,f.quantity,
        f.price,f.created_at_ms,o.role FROM fill f JOIN order_intent o
        USING(client_order_id) WHERE o.role IN ('EXIT','STOP')
        ORDER BY f.created_at_ms DESC,f.rowid DESC LIMIT 1''').fetchone()
    return dict(row) if row else None


def render_reduction(fact):
    if not fact:
        return '- 退出/止损减仓成交: 本账户账本暂无记录（配对成交另见实盘成交推送）'
    return (f"- 最近退出/止损减仓成交: {fact['quantity']} ETH @ {fact['price']} USDT"
            f"；成交 ID {fact['trade_id']}\n"
            f"- 成交入账时间（北京时间）: {beijing_time(fact['created_at_ms'])}")


def _number(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError('non-finite account evidence')
    return str(value)


class SignalReceiptWriter:
    def __init__(self):
        self._token = None
        self._last_attempt = 0

    async def capture(self, service):
        store = service.store
        row = store.conn.execute('SELECT decision_id,payload_json FROM decisions ORDER BY rowid DESC LIMIT 1').fetchone()
        if row is None:
            return
        decision_id = row[0]
        decision = json.loads(row[1])
        state = store.engine_state()
        risk = service._risk_result
        token = (decision_id, store.cursor(), state['state'], state['lock_reason'],
                 service.stream.ready, risk.status.value if risk else 'UNKNOWN')
        now = int(time.time() * 1000)
        if token == self._token or now - self._last_attempt < 1000:
            return
        self._last_attempt = now
        sizing = getattr(service, '_plan_receipt', None)
        if (not sizing or sizing.get('decision_id') != decision_id
                or not 0 <= now - sizing.get('checked_at_ms', 0) <= 60_000):
            sizing = None
        report = dict(decision_id=decision_id, updated_at_ms=now, state=state['state'],
                      reason=state['lock_reason'], processed=store.cursor() == decision_id,
                      stream_ready=service.stream.ready, risk=token[-1], sizing=sizing,
                      account=None, reduction=last_reduction(store),
                      expected_leverage=service.settings.expected_leverage,
                      signal_state=decision['state'])
        try:
            account = await service.exchange.account_info()
            position = await service.exchange.position()
            orders = await service.exchange.open_orders()
            stops = await service.exchange.open_stops()
            report['account'] = dict(available=_number(account['totalAvailableBalance']),
                position_margin=(_number(account['positionInitialMargin'])
                                 if 'positionInitialMargin' in account else None),
                order_margin=(_number(account['openOrderInitialMargin'])
                              if 'openOrderInitialMargin' in account else None),
                position=_number(position[0]['positionAmt']),
                open_orders=len(orders), open_stops=len(stops))
        except Exception as exc:
            # Never substitute configured capital for missing account evidence.
            report['account_error'] = type(exc).__name__
        report['updated_at_ms'] = int(time.time() * 1000)
        store.conn.execute('INSERT INTO schema_meta(key,value) VALUES(?,?) '
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (RECEIPT_KEY, json.dumps(report, ensure_ascii=False)))
        if report['account'] is not None:
            self._token = token


def render_receipt(report, decision_id, *, now_ms=None):
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if (not report or not decision_id or report.get('decision_id') != decision_id
            or not 0 <= now - report.get('updated_at_ms', 0) <= 60_000):
        return PENDING_TEXT + '\n未确认实际下单或减码，不能将建议视为成交。'
    lines = ['💰 实盘账户与执行回执',
             f"- 账户快照时间（北京时间）: {beijing_time(report['updated_at_ms'])}"]
    account = report.get('account')
    if account:
        lines.append(f"- 可用余额: {Decimal(account['available']):.4f} USDT")
        for label, key in [('持仓初始保证金', 'position_margin'), ('挂单初始保证金', 'order_margin')]:
            value = account.get(key)
            amount = f'{Decimal(value):.4f} USDT' if value is not None else '待确认'
            lines.append(f'- {label}: {amount}')
        lines.append(f"- 实际净仓: {account['position']} ETH；普通挂单 {account['open_orders']}，条件单 {account['open_stops']}")
    else:
        lines.append('- 实际账户余额、保证金与持仓: 待确认（读取失败）')
    sizing = report.get('sizing') or {}
    if sizing.get('leverage') is not None:
        lines += [f"- 实盘杠杆: {sizing['leverage']}x（已核验账户设置）",
                  f"- 本次建单保证金预算: {sizing['budget']} USDT；实盘保证金上限 {sizing['max_margin']} USDT",
                  '- 预算取策略建议、可用余额与实盘上限的最小值。']
    elif report.get('expected_leverage'):
        lines.append(f"- 执行器要求杠杆: {report['expected_leverage']}x（本次尚无账户杠杆校验回执）")
    reasons = {'order notional below minimum': '单笔订单名义金额低于交易所最低要求',
               'execution budget below minimum quantity': '预算缩量后低于交易所最小数量',
               'no available execution margin': '没有可用建单保证金'}
    if sizing.get('status') == 'REJECTED':
        reason = sizing.get('reason', '未知')
        lines.append('- 本次建单校验: 未通过；' + reasons.get(reason, reason))
        if sizing.get('order_notional'):
            lines.append(f"- 拦截订单: {sizing['quantity']} ETH × {sizing['order_price']} USDT = {sizing['order_notional']} USDT")
    elif sizing.get('status') == 'PASSED':
        lines.append(f"- 本次建单校验通过: 每单 {sizing['quantity']} ETH，共 {sizing['order_count']} 笔委托（不等于成交）")
    else:
        lines.append('- 本次新建单校验: 未执行或待确认；既有网格以实盘状态为准')
        lines.append(f"- 信号状态: {report.get('signal_state', 'UNKNOWN')}")
    if sizing.get('min_notional'):
        lines.append(f"- 交易所单笔最低名义金额: {sizing['min_notional']} USDT")
    lines.append(f"- 执行状态: {report.get('state', 'UNKNOWN')}；本次决策{'已处理' if report.get('processed') else '尚未处理完成'}；风控 {report.get('risk', 'UNKNOWN')}")
    if report.get('reason'):
        lines.append(f"- 执行限制: {report['reason']}")
    lines += [render_reduction(report.get('reduction')),
              '- 保证金建议不作为减码成交记录；建议冷却不代表实盘操作。']
    return '\n'.join(lines)
