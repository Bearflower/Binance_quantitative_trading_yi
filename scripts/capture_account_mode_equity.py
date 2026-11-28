"""冻结开仓并核对空仓后，签名只读采样并在数据库保存切换权益锚点。"""
import argparse
import asyncio
from datetime import datetime, timezone
import json

from scripts.check_account_cutover import flat_inventory
from scripts.export_execution_facts import _build_client_from_env, _maybe_open_sink
from shared.accounting.equity_bridge import main_account, save_observation, signed_spot_uid_sha256


async def capture(phase, evidence):
    client, _ = _build_client_from_env()
    if client is None:
        raise RuntimeError("缺少签名凭据")
    db = None
    try:
        if (phase == "pm_final") != client.use_unified_account:
            raise ValueError("所选锚点与签名客户端模式不一致")
        _, db = await _maybe_open_sink()
        if db is None:
            raise ValueError("保存锚点需要 EXECUTION_FACTS_DB_WRITE=true")
        uid_hash = await signed_spot_uid_sha256(client)
        inventory = await flat_inventory(client)
        if not inventory["flat"]:
            raise ValueError("合约账户尚有持仓或挂单")
        account = await client.get_account_info()
        observed = await save_observation(db, phase, datetime.now(timezone.utc),
                                          account["totalMarginBalance"], account["availableBalance"],
                                          0, evidence, uid_hash)
        print(json.dumps(dict(account_id=main_account(), phase=phase,
                              valuation_unit="USD" if client.use_unified_account else "USDT", **observed), default=str))
    finally:
        await client.close()
        if db is not None:
            await db.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["pm_final", "classic_open"])
    parser.add_argument("--evidence-ref", required=True, help="空仓、清单与划转核对记录的引用")
    args = parser.parse_args()
    asyncio.run(capture(args.phase, args.evidence_ref))
