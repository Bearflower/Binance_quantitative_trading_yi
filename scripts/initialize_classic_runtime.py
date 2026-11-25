"""用已核实的经典期初空仓归档旧策略的运行缓存。"""
import asyncio
import json
import os

from scripts.export_execution_facts import _maybe_open_sink
from shared.accounting.runtime_cutover import classic_epoch, initialize_classic_runtime
from shared.binance_account_mode import require_no_pm_claims, verify_cutover_ledger, verify_trading_mode
from shared.readonly_client import ReadOnlyBinanceClient


async def main():
    client = ReadOnlyBinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"],
                                   instance_id="classic-runtime-cutover")
    db = None
    try:
        epoch = classic_epoch(client)
        if epoch is None:
            raise RuntimeError("归档运行状态仅适用于实盘经典账户切点")
        _, db = await _maybe_open_sink()
        if db is None:
            raise RuntimeError("归档运行状态需要数据库写入权限")
        await verify_trading_mode(client)
        await verify_cutover_ledger(client, db)
        await require_no_pm_claims(db, *epoch)
        await initialize_classic_runtime(client, db)
        print(json.dumps({"account_id": epoch[0], "cutover_ms": epoch[1], "runtime_ready": True}))
    finally:
        await client.close()
        if db is not None:
            await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
