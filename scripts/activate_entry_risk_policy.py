"""Review entry-only ceilings; no trades or holdings-allocation writes.

Default dry run. --activate requires explicit expected revision and reason.
Provide administrator DSN via RISK_POLICY_ADMIN_DSN, never a CLI password.
Frozen exposure ceilings do not represent actual withdrawable cash.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asyncpg
from shared.entry_risk_policy import account_id, current_allocation, number, activate_policies


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--verified-equity',required=True,help='Explicit verified current account equity in USDT')
    p.add_argument('--reason',required=True)
    p.add_argument('--account',default=account_id())
    p.add_argument('--expected-revision',type=int,required=True)
    p.add_argument('--valid-hours',type=int,default=72)
    p.add_argument('--enable-reduced',action='store_true',help='Explicitly permit aggressive reduced entry; every entry still requires live reconciliation')
    p.add_argument('--approved-ceilings',help='JSON strategy-to-absolute-ceiling map; never increase these reviewed limits')
    p.add_argument('--activate',action='store_true',help='Atomic administrative policy activation (default: DRY RUN)')
    return p


async def run(args):
    equity=number(args.verified_equity)
    ceilings=json.loads(args.approved_ceilings) if getattr(args,'approved_ceilings',None) else None
    if ceilings is not None:
        if set(ceilings)!={'hrs','btc_eth_aggressive','btc_eth','new_coin'}:raise ValueError('exact four approved ceilings required')
        ceilings={key:number(value,zero=True) for key,value in ceilings.items()}
    pool=await asyncpg.create_pool(os.environ['RISK_POLICY_ADMIN_DSN'],min_size=1,max_size=2)
    try:
        candidates=[]
        async with pool.acquire() as conn:
            for strategy,ratio,mode,mult in [('hrs','0','monitor_only','0'),('btc_eth_aggressive','.15','reduced' if args.enable_reduced else 'monitor_only','.5'),('btc_eth','.20','capped','1'),('new_coin','.25','capped','1')]:
                allocation=await current_allocation(conn,strategy) if strategy != 'hrs' else number(0,zero=True)
                cap=min(allocation,equity*number(ratio,zero=True))
                if ceilings is not None:cap=min(cap,ceilings[strategy])
                candidates.append(dict(account_id=args.account,strategy_id=strategy,mode=mode,allow_add=False,
                    multiplier=mult,ratio_cap=ratio,entry_ceiling=str(cap),reason=args.reason,
                    **({'allocation_amount':str(allocation)} if strategy != 'hrs' else {})))
        result=dict(dry_run=not args.activate,expected_revision=args.expected_revision,verified_equity=str(equity),
                    candidates=candidates,holdings_allocation_mutated=False,close_callbacks=0,
                    reduced_runtime_requires_reconciliation=True,ceilings_are_withdrawable_cash=False)
        if args.activate:
            result['active_revision']=await activate_policies(pool,candidates,expected_revision=args.expected_revision,valid_hours=args.valid_hours)
        print(json.dumps(result,indent=2,ensure_ascii=False))
        return result
    finally:await pool.close()


if __name__=='__main__':
    asyncio.run(run(parser().parse_args()))
