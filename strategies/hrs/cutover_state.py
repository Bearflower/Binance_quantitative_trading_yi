"""验证 HRS 重启后可能触发订单操作的持久化状态。"""
from datetime import datetime, timezone
import json
import os

from shared.accounting.account_cutover import account_cutover_ms


def classic_timeout_identity(account, cutover):
    return {"account_id": account, "account_mode": "classic", "cutover_ms": cutover}


def state_document(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


async def verify_classic_restore(db, client):
    if client.use_unified_account is not False or client.testnet:
        return
    account = os.environ.get("TRADE_ACCOUNT_ID", "")
    cutover = account_cutover_ms(account)
    if not account or cutover is None:
        raise ValueError("HRS 经典账户切点未绑定")
    pending = await db.fetch_all("SELECT value FROM hrs.hrs_meta WHERE key LIKE 'hrs-timeout:%'")
    identity = classic_timeout_identity(account, cutover)
    for row in pending:
        item = state_document(row["value"])
        if not isinstance(item, dict) or any(item.get(key) != value for key,value in identity.items()):
            raise ValueError("HRS 保留了切换前或未核验的超时恢复任务")
    stale = await db.fetch_one("SELECT symbol FROM hrs.hrs_positions WHERE "
                              "opened_at IS NULL OR "
                              "opened_at < $1 OR opening_time_uncertain IS TRUE LIMIT 1",
                              cutover)
    if stale:
        raise ValueError("HRS 持仓表存在切换前或缺少开仓时间的持仓")
    backup = await db.fetch_one("SELECT state_data->'positions' AS backup FROM hrs.hrs_state WHERE state_key='main'")
    if not backup:
        return
    data = state_document(backup["backup"])
    positions = data.get("positions",{}) if isinstance(data, dict) else {}
    if not isinstance(positions, dict):
        raise ValueError("HRS 持仓备份格式未知")
    for pos in positions.values():
        stamp = pos.get("opened_at")
        if not stamp:
            raise ValueError("HRS 备份缺少不可变的开仓时间")
        opened = datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
        if (pos.get("opening_time_uncertain") or opened.tzinfo is None or
                opened < datetime.fromtimestamp(cutover/1000, timezone.utc)):
            raise ValueError("HRS 备份包含切换前或缺少开仓时间的持仓")
