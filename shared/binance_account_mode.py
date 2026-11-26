"""币安主账户模式配置与经典合约启动检查。"""
import os


def configured_pm(environ=None):
    env = os.environ if environ is None else environ
    modes = set()
    for key, choices in (
        ("BINANCE_ACCOUNT_MODE", {"pm": True, "classic": False}),
        ("BINANCE_USE_PM", {"true": True, "false": False}),
        ("USE_UNIFIED_ACCOUNT", {"true": True, "false": False}),
    ):
        value = env.get(key, "").strip().lower()
        if value:
            if value not in choices:
                raise ValueError(f"{key} 配置值无效")
            modes.add(choices[value])
    if len(modes) > 1:
        raise ValueError("币安账户模式配置不一致")
    return modes.pop() if modes else True


async def verify_trading_mode(client):
    if client.use_unified_account:
        return
    config = await client._request("GET", "/fapi/v1/accountConfig", signed=True)
    if not isinstance(config, dict) or not (
        config.get("canTrade") is True
        and config.get("dualSidePosition") is False
        and config.get("multiAssetsMargin") is False
    ):
        raise RuntimeError("经典合约账户需要已启用交易的单向、单资产模式")


async def verify_cutover_ledger(client, db, *, trading_writer=False):
    """经典实盘启动核验；交易专用清单仅限已经归档的交易写入者。"""
    from shared.accounting.collector_recovery import trading_recovery_configured, verify_trading_recovery
    recovery = trading_writer and trading_recovery_configured()
    if client.use_unified_account or client.testnet:
        if recovery:
            raise RuntimeError("交易恢复凭据仅限经典实盘账户")
        return
    from shared.accounting.account_cutover import classic_cutover_ms
    from shared.accounting.equity_bridge import read_anchors, signed_spot_uid_sha256
    cutover = classic_cutover_ms()
    account = os.environ.get("TRADE_ACCOUNT_ID", "")
    if not account.strip() or cutover is None or db is None:
        raise RuntimeError("经典实盘交易缺少账户身份、切点或数据库")
    pin = None
    if recovery:
        pin = await verify_trading_recovery(client, db.fetch_all, account, cutover)
    else:
        await read_anchors(db, cutover, expected_uid=await signed_spot_uid_sha256(client))
    if trading_writer:
        from shared.accounting.runtime_cutover import require_runtime_marker
        await require_no_pm_claims(db, account, cutover)
        await require_runtime_marker(client, db, recovery_pin=pin)


async def require_no_pm_claims(db, account, cutover):
    await check_pm_claims(db.fetch_one, account, cutover)


async def check_pm_claims(fetch_one, account, cutover):
    pending = await fetch_one("""
        SELECT EXISTS(SELECT 1 FROM trading.entry_risk_reservation
            WHERE account_id=$1 AND state IN ('reserved','submitting')
              AND created_at < to_timestamp($2::double precision / 1000)) AS reservations,
            EXISTS(SELECT 1 FROM trading.position_claims
            WHERE claim_state IN ('PENDING','ACTIVE')
              AND created_at < timezone('UTC', to_timestamp($2::double precision / 1000)))
            AS claims
    """, account, cutover)
    if not pending or pending["reservations"] or pending["claims"]:
        raise RuntimeError("仍有切换前的风险预留或持仓认领需要核对")
