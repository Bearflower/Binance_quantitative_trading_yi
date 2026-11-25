"""仅为经典成交/收入采集核验封存证据，不生成权益锚点或交易运行标记。"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re

from shared.accounting.equity_bridge import signed_spot_uid_sha256
from shared.binance_account_mode import verify_cutover_ledger

_MANIFEST = "CLASSIC_COLLECTOR_RECOVERY_MANIFEST"
_PIN = "CLASSIC_COLLECTOR_RECOVERY_SHA256"


def _require(condition, label):
    if not condition:
        raise ValueError(f"经典采集恢复证据校验失败：{label}")


def _unique_fields(items):
    result = {}
    for key, value in items:
        _require(key not in result, "JSON 包含重复字段")
        result[key] = value
    return result


def _digest(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "SHA256 格式")
    return value


def _load(path, digest):
    raw = path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == _digest(digest), "文件摘要不符")
    value = json.loads(raw, object_pairs_hook=_unique_fields)
    _require(isinstance(value, dict), "凭据必须是 JSON 对象")
    return value


def _ms(value):
    _require(type(value) is int and value > 0, "毫秒时间戳")
    return value


def _zero(value):
    return type(value) is int and value == 0


def _money(value):
    amount = Decimal(str(value))
    _require(amount.is_finite(), "金额或数量无效")
    return amount


def _quantities(rows):
    _require(isinstance(rows, list), "持仓列表")
    result = {}
    for row in rows:
        symbol = row["symbol"]
        _require(isinstance(symbol, str) and symbol and symbol not in result and row["positionSide"] == "BOTH",
                 "持仓标的或持仓模式不符")
        result[symbol] = _money(row["positionAmt"])
    return {symbol: qty for symbol, qty in result.items() if qty}


def _verify_closures(pm, summary, queries, cutover):
    positions = _quantities(pm["um"])
    _require(positions == _quantities(pm["um_account"]) and pm["open"] == [], "冻结时双侧持仓或未结普通单")
    fills_seen = set()
    expected_income = {}
    for symbol, qty in positions.items():
        order = queries[f"close_order_{symbol}"]["data"]
        side = "BUY" if qty < 0 else "SELL"
        _require(order["symbol"] == symbol and order["status"] == "FILLED" and order["side"] == side
                 and order["positionSide"] == "BOTH" and order["reduceOnly"] is True
                 and _money(order["executedQty"]) == abs(qty)
                 and pm["end_ms"] <= _ms(order["updateTime"]) < cutover, "平仓委托")
        fills = queries[f"classic_fills_{symbol}"]["data"]
        _require(isinstance(fills, list) and fills, "平仓成交列表")
        filled = Decimal(0)
        for fill in fills:
            key = (symbol, fill["id"])
            amount = _money(fill["qty"])
            _require(fill["symbol"] == symbol and fill["orderId"] == order["orderId"]
                     and fill["side"] == side and fill["positionSide"] == "BOTH" and amount > 0
                     and key not in fills_seen and pm["end_ms"] <= _ms(fill["time"]) < cutover, "平仓成交")
            fills_seen.add(key)
            _require(fill["commissionAsset"] == "USDT", "平仓手续费资产")
            for kind, value in (("REALIZED_PNL", _money(fill["realizedPnl"])),
                                ("COMMISSION", -_money(fill["commission"]))):
                if value:
                    expected_income[(symbol, str(fill["id"]), kind)] = (value, fill["time"])
            filled += amount
        _require(filled == abs(qty), "平仓成交量与冻结数量不一致")
    _require(type(summary["closed_fills"]) is int and summary["closed_fills"] == len(fills_seen), "平仓成交计数")
    verified_income = {}
    for row in queries["classic_income"]["data"]:
        key = (row["symbol"], row["tradeId"], row["incomeType"])
        if key in expected_income:
            _require(key not in verified_income and row["asset"] == "USDT", "平仓收入资产或重复流水")
            verified_income[key] = (_money(row["income"]), _ms(row["time"]))
    _require(verified_income == expected_income and type(summary["attributed_close_income_rows"]) is int
             and summary["attributed_close_income_rows"] == len(verified_income), "平仓收入与成交不符")
    _require(isinstance(pm["algo"], list), "旧保护单清单")
    cancelled = set()
    for previous in pm["algo"]:
        algo_id = previous["algoId"]
        terminal = queries[f"classic_algo_{algo_id}"]["data"]
        _require(algo_id not in cancelled and terminal["algoId"] == algo_id
                 and terminal["symbol"] == previous["symbol"] and terminal["algoStatus"] == "CANCELED"
                 and not terminal["actualOrderId"] and pm["end_ms"] <= _ms(terminal["updateTime"]) < cutover,
                 "旧保护单终态")
        cancelled.add(algo_id)
    _require(type(summary["cancelled_previous_algos"]) is int
             and summary["cancelled_previous_algos"] == len(cancelled), "旧保护单计数")


def _verify_bundle(path, pin, account, cutover, now_ms, uid):
    manifest = _load(path, pin)
    _require(type(manifest["version"]) is int and manifest["version"] == 1
             and manifest["scope"] == "execution_facts_collector" and manifest["reason"] == "missing_pm_final"
             and manifest["account_id"] == account and _ms(manifest["cutover_ms"]) == cutover, "用途、账户或切点")
    old, new = manifest["pm"], manifest["classic"]

    def artifact(digest):
        return _load(path.with_name(_digest(digest) + ".json"), digest)

    pm = artifact(old["identity_evidence_sha256"])
    summary = artifact(old["closure_evidence_sha256"])
    classic = artifact(new["snapshot_evidence_sha256"])
    queries = classic["queries"]
    _require(pm["account"] == classic["account"] == "main" and pm["mode"] == "pm"
             and classic["uid_matches"] is True and _digest(uid) == pm["uid_hash"] == old["uid_sha256"]
             == new["uid_sha256"] == queries["uid"]["data"], "签名 UID 或账户")
    _require(_ms(old["identity_observed_ms"]) == _ms(pm["end_ms"])
             and _ms(pm["start_ms"]) <= pm["end_ms"] < cutover
             and _ms(new["observed_ms"]) == _ms(classic["end_ms"])
             and cutover <= _ms(classic["start_ms"]) <= classic["end_ms"] <= now_ms, "凭据采样时间")
    _require(old["writers_frozen"] is True and old["closure_reconciled"] is True
             and summary["pm_final_anchor"] == "missing" and summary["classic_runtime"] == "frozen"
             and isinstance(summary["frozen"], list) and summary["frozen"], "冻结交接")
    _verify_closures(pm, summary, queries, cutover)
    config = queries["classic_config"]["data"]
    _require(queries["account_info"]["data"]["isPortfolioMarginRetailEnabled"] is False
             and config["canTrade"] is True and config["dualSidePosition"] is False
             and config["multiAssetsMargin"] is False, "经典签名单资产、单向账户配置")
    snapshot = queries["classic_account"]["data"]
    total = _money(snapshot["totalMarginBalance"])
    receipt = summary["accounts"]["main"]
    _require(receipt["classic"] is True and receipt["can_trade"] is True and total > 0
             and _money(new["total_margin_balance"]) == total
             and _money(receipt["available_usdt"]) == _money(snapshot["availableBalance"]), "经典期初钱包")
    _require(not _quantities(snapshot["positions"]) and not _quantities(queries["classic_position"]["data"])
             and new["open_symbols"] == [] and queries["classic_orders"]["data"] == queries["classic_algos"]["data"] == []
             and all(_zero(x) for x in (new["ordinary_count"], new["algo_count"],
                                        receipt["positions"], receipt["ordinary_orders"], receipt["algo_orders"])),
             "经典期初空仓及普通单、Algo 挂单")


async def verify_collector_ledger(client, db, account, cutover, now_ms):
    """未配置恢复凭据时沿用共享门槛；恢复模式要求这个切点两侧均无权益锚点。"""
    if _MANIFEST not in os.environ and _PIN not in os.environ:
        if not client.use_unified_account:
            await verify_cutover_ledger(client, db)
        return None
    path = Path(os.environ.get(_MANIFEST, ""))
    pin = _digest(os.environ.get(_PIN))
    _require(path.is_absolute() and client.use_unified_account is False and client.testnet is False
             and account == os.environ.get("TRADE_ACCOUNT_ID") and account.strip() and cutover
             and db is not None, "恢复配置只限经典实盘指定账户")
    uid = await signed_spot_uid_sha256(client)
    try:
        _verify_bundle(path, pin, account, cutover, now_ms, uid)
    except (OSError, KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("经典采集恢复凭据无法读取或字段无效") from exc
    anchors = await db.fetch_all(
        "SELECT observation FROM trading.account_mode_equity "
        "WHERE account_id=$1 AND cutover_ms=$2 AND snapshot_date IS NULL", account, cutover)
    _require(anchors == [], "账本中已有切换锚点，请按双锚点流程核对")
    return pin
