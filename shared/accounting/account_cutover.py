"""同一交易账户由 PM 切到经典合约，切点毫秒属于经典模式。"""
import os

INCOME_SOURCES = ("um/income", "fapi/income")
FILL_SOURCES = ("userTrades", "fapi/userTrades")


def classic_cutover_ms():
    raw = os.environ.get("BINANCE_CLASSIC_CUTOVER_MS", "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdecimal() or not 0 < int(raw) < 253402300800000:
        raise ValueError("BINANCE_CLASSIC_CUTOVER_MS 必须是有效的 UTC Unix 毫秒时间戳")
    return int(raw)


def account_cutover_ms(account_id):
    # 此配置属于当前签名账户。其它账户的历史 PM 游标不受本次切换影响。
    return classic_cutover_ms() if account_id == os.environ.get("TRADE_ACCOUNT_ID", "default") else None


def order_epoch(stamp, cutover):
    """trade_records 使用北京时间，交易所成交使用有时区的 UTC。"""
    from datetime import datetime, timedelta, timezone
    if cutover is None:
        return "pm"
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone(timedelta(hours=8)))
    boundary = datetime.fromtimestamp(cutover / 1000, timezone.utc)
    return "pm" if stamp < boundary else "classic"


def collection_window(start, end, pm, cutover):
    """返回接口可证明的闭区间。两个模式永远不采同一毫秒。"""
    if cutover is not None:
        if pm:
            end = min(end, cutover - 1)
        else:
            start = max(start, cutover)
    elif not pm:
        raise ValueError("经典账户事实采集需要配置 BINANCE_CLASSIC_CUTOVER_MS")
    if cutover is not None and start > end:
        raise ValueError("请求窗口完全位于另一账户模式的采集区间")
    return start, end


def coverage_watermark(rows, account_id, start, end, sources, kind, symbol):
    """将闭区间采集游标与报表半开区间对齐，并校验来源与模式生效时间。"""
    cutover = account_cutover_ms(account_id)
    intervals = []
    for row in rows:
        if (row.get("account_id") != account_id or row.get("income_type") != kind
                or row.get("symbol") != symbol or row.get("page_complete") is not True
                or row.get("request_status") != "ok" or row.get("covered_until_ms") is None):
            continue
        left, right = int(row["window_start_ms"]), int(row["covered_until_ms"])
        if row["source"] == sources[0]:
            if cutover is not None:
                # 存量系统以 covered_until_ms 作为保守水位。切点前最后一毫秒
                # 已实际采集，允许与经典首毫秒拼接。
                right = min(right + int(right == cutover - 1), cutover)
        elif row["source"] == sources[1] and cutover is not None:
            left = max(left, cutover)
        else:
            continue
        intervals.append((left, right))
    covered = start
    for left, right in sorted(intervals):
        if left > covered:
            break
        covered = max(covered, right)
    return min(end, covered)


def observed_symbol_windows(account, start, end, incomes, fills):
    """分段确定成交覆盖范围。经典账户新交易的品种不要求有历史 PM 成交游标。"""
    from datetime import datetime, timezone
    cutover = account_cutover_ms(account)
    bounds = [start, cutover, end] if cutover is not None and start < cutover < end else [start, end]
    for left, right in zip(bounds, bounds[1:]):
        t0 = datetime.fromtimestamp(left / 1000, timezone.utc)
        t1 = datetime.fromtimestamp(right / 1000, timezone.utc)
        symbols = set()
        for rows, field in ((incomes, "event_time_utc"), (fills, "trade_time_utc")):
            for row in rows:
                stamp = row.get(field)
                if row.get("symbol") and (stamp is None or t0 <= stamp < t1):
                    symbols.add(row["symbol"])
        yield left, right, symbols
