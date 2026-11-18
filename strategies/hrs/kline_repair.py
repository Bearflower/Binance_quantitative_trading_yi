"""Bounded HRS-only reconstruction from actual closed Binance hourly bars.

Existing arrays are immutable evidence. Validate the entire original span before
applying the ordinary configured windows; never rescue a gap by taking a suffix.
Old four-hour arrays are derived caches, not authoritative exchange observations.
"""
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import aiohttp

from shared.binance_api import BinanceAPIError
from .core_decision import DecisionInputsUnavailable, validate_klines
from .decision_capture import canonical_payload

HOUR_MS = 3_600_000
MAX_HISTORY_HOURS = 4096
MAX_REQUESTS = 64
REPAIR_TIMEOUT_SECONDS = 30.0
PRICE_FIELDS = ('open', 'high', 'low', 'close', 'volume')


class KlineHistoryUnavailable(DecisionInputsUnavailable):
    def __init__(self, reason, receipt):
        super().__init__(reason)
        self.reason = reason
        self.receipt = receipt


@dataclass(frozen=True)
class PreparedKlines:
    klines_1h: list
    klines_4h: list
    available_at: datetime
    receipt: dict


def _fail(reason, receipt):
    receipt['available_at'] = datetime.now(timezone.utc)
    receipt['status'] = 'unavailable'
    receipt['reason'] = reason
    raise KlineHistoryUnavailable(reason, receipt)


def _normalized(rows, interval, decision_at, receipt):
    result = {}
    dropped = 0
    for original in rows:
        try:
            timestamp = original['open_time']
            if isinstance(timestamp, bool) or int(timestamp) != timestamp or int(timestamp) % interval:
                _fail('kline_timestamp_invalid', receipt)
            timestamp = int(timestamp)
            row = dict(original)
            row['open_time'] = timestamp
            for key in PRICE_FIELDS:
                row[key] = Decimal(str(row[key]))
            validate_klines([row], decision_at, interval_ms=interval)
        except DecisionInputsUnavailable as exc:
            if isinstance(exc, KlineHistoryUnavailable):
                raise
            if str(exc) == 'kline_not_closed':
                dropped += 1
                continue
            _fail(str(exc), receipt)
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            _fail('kline_ohlcv_invalid', receipt)
        previous = result.get(timestamp)
        if previous is not None:
            # Extra source fields are compared whenever both observations have
            # them. A legacy cache lacking quote/trade fields cannot invent them.
            for key in previous.keys() & row.keys():
                try:
                    same = Decimal(str(previous[key])) == Decimal(str(row[key]))
                except InvalidOperation:
                    same = previous[key] == row[key]
                if not same:
                    _fail('kline_timestamp_conflict', receipt)
        else:
            result[timestamp] = row
    return result, dropped


def complete_four_hour_bars(rows):
    """Only four contiguous UTC children establish a full synthetic candle."""
    by_time = {}
    for row in rows:
        timestamp = row['open_time']
        if not isinstance(timestamp, int) or timestamp % HOUR_MS:
            raise DecisionInputsUnavailable('kline_timestamp_invalid')
        previous = by_time.get(timestamp)
        if previous is not None and any(Decimal(str(previous[k])) != Decimal(str(row[k])) for k in PRICE_FIELDS):
            raise DecisionInputsUnavailable('kline_timestamp_conflict')
        by_time[timestamp] = row
    result = []
    for slot in sorted({t // (4*HOUR_MS)*(4*HOUR_MS) for t in by_time}):
        children = [by_time.get(slot+i*HOUR_MS) for i in range(4)]
        if any(child is None for child in children):
            continue
        result.append(dict(open_time=slot, open=float(children[0]['open']),
            high=max(float(c['high']) for c in children), low=min(float(c['low']) for c in children),
            close=float(children[-1]['close']), volume=sum(float(c['volume']) for c in children)))
    return result


def _decode_response(raw, receipt):
    if not isinstance(raw, list):
        _fail('kline_repair_response_invalid', receipt)
    rows = []
    try:
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 12:
                _fail('kline_repair_response_invalid', receipt)
            rows.append(dict(open_time=item[0], open=item[1], high=item[2], low=item[3],
                close=item[4], volume=item[5], close_time=item[6], quote_volume=item[7],
                trades=item[8], taker_buy_base=item[9], taker_buy_quote=item[10]))
    except (IndexError, TypeError):
        _fail('kline_repair_response_invalid', receipt)
    return rows


async def prepare_klines(client, symbol, cached_1h, cached_4h, *, decision_at,
                         keep_count, four_hour_count, minimum, page_limit):
    """Return actual decision windows plus a receipt of the full-span proof.

    All hashes use decision_capture.canonical_payload. Each request response is
    additionally retained by the existing public-response capture transport.
    The returned availability is the real completion time, never decision_at.
    """
    if not isinstance(decision_at, datetime) or decision_at.tzinfo is None:
        raise DecisionInputsUnavailable('decision_time_invalid')
    deadline = time.monotonic()+REPAIR_TIMEOUT_SECONDS
    receipt = dict(version='hrs_kline_repair_v1', symbol=symbol, decision_at=decision_at,
        started_at=datetime.now(timezone.utc),
        hash_algorithm='decision_capture.canonical_payload/sha256',
        input_1h_sha256=canonical_payload(cached_1h)[1], input_4h_sha256=canonical_payload(cached_4h)[1],
        input_1h_count=len(cached_1h), input_4h_count=len(cached_4h), requests=[])
    hourly, dropped = _normalized(cached_1h, HOUR_MS, decision_at, receipt)
    derived, _ = _normalized(cached_4h, 4*HOUR_MS, decision_at, receipt)
    receipt['dropped_unclosed_count'] = dropped
    last = int(decision_at.timestamp()*1000)//HOUR_MS*HOUR_MS-HOUR_MS
    # Include enough raw children for normal 1h and 4h windows even on first use.
    desired = max(keep_count, four_hour_count*4)
    starts = [*hourly, *derived]
    start = min(starts) if starts else last-(desired-1)*HOUR_MS
    start = start//(4*HOUR_MS)*(4*HOUR_MS)
    count = (last-start)//HOUR_MS+1
    receipt.update(full_start_time=start, full_end_time=last, full_count=count)
    if count > MAX_HISTORY_HOURS:
        _fail('kline_repair_span_exceeded', receipt)
    page_limit = max(1, min(int(page_limit), 100))
    missing = [t for t in range(start, last+1, HOUR_MS) if t not in hourly]
    while missing:
        if len(receipt['requests']) >= MAX_REQUESTS:
            _fail('kline_repair_request_limit', receipt)
        begin = missing[0]
        end = begin
        for t in missing[1:page_limit]:
            if t != end+HOUR_MS:
                break
            end = t
        params = dict(symbol=symbol, interval='1h', startTime=begin, endTime=end+HOUR_MS-1,
                      limit=min(page_limit, (end-begin)//HOUR_MS+1))
        request = dict(path='/fapi/v1/klines', params=params, started_at=datetime.now(timezone.utc))
        receipt['requests'].append(request)
        try:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                _fail('kline_repair_budget_exceeded', receipt)
            raw = await asyncio.wait_for(client._request('GET', '/fapi/v1/klines', params, signed=False),
                                         timeout=remaining)
        except (TimeoutError, aiohttp.ClientError, BinanceAPIError) as exc:
            request.update(available_at=datetime.now(timezone.utc), error_type=type(exc).__name__)
            _fail('kline_repair_budget_exceeded' if time.monotonic() >= deadline
                  else 'kline_repair_source_unavailable', receipt)
        request.update(available_at=datetime.now(timezone.utc), response_sha256=canonical_payload(raw)[1])
        decoded = _decode_response(raw, receipt)
        request['received_count'] = len(decoded)
        rows, _ = _normalized(decoded, HOUR_MS, decision_at, receipt)
        if any(t < begin or t > end for t in rows):
            _fail('kline_repair_response_out_of_bounds', receipt)
        if not rows or any(t not in rows for t in range(begin,end+1,HOUR_MS)):
            _fail('kline_repair_incomplete', receipt)
        hourly.update(rows)
        missing = [t for t in missing if t not in rows]
    full = [hourly[t] for t in range(start, last+1, HOUR_MS)]
    try:
        validate_klines(full, decision_at, minimum=minimum)
        four = complete_four_hour_bars(full)
        validate_klines(four, decision_at, interval_ms=4*HOUR_MS, minimum=1)
        one_window, four_window = full[-keep_count:], four[-four_hour_count:]
        validate_klines(one_window, decision_at, minimum=minimum)
    except DecisionInputsUnavailable as exc:
        _fail(str(exc), receipt)
    available = datetime.now(timezone.utc)
    receipt.update(status='complete', elapsed_seconds=REPAIR_TIMEOUT_SECONDS-(deadline-time.monotonic()),
        full_1h_sha256=canonical_payload(full)[1],
        full_4h_sha256=canonical_payload(four)[1], full_4h_count=len(four),
        output_1h_sha256=canonical_payload(one_window)[1], output_4h_sha256=canonical_payload(four_window)[1],
        output_1h_count=len(one_window), output_4h_count=len(four_window), available_at=available)
    return PreparedKlines(one_window, four_window, available, receipt)
