"""Income-only atomic writes, durable pending latch, and post-rollback evidence."""
import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from shared.accounting.income_integrity import income_account_lock

_PENDING = "INSERT INTO trading.income_ingestion_attempt (attempt_id,account_id,observations) VALUES ($1,$2,$3::jsonb)"
_COMPLETE = "UPDATE trading.income_ingestion_attempt SET state='complete',finished_at=clock_timestamp() WHERE attempt_id=$1"
_CONFLICT = "INSERT INTO trading.income_ingestion_conflict (attempt_id,error_code,error_message,evidence) VALUES ($1,$2,$3,$4::jsonb)"
_MARK_CONFLICT = "UPDATE trading.income_ingestion_attempt SET state='conflicted',finished_at=clock_timestamp() WHERE attempt_id=$1"


def _canonical_json(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k,ensure_ascii=False)+':'+_canonical_json(v) for k,v in sorted(value.items())) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_canonical_json(v) for v in value) + ']'
    if isinstance(value, (int,float,Decimal)) and not isinstance(value,bool):
        number=Decimal(str(value))
        if not number.is_finite(): raise ValueError('Nonfinite income observation')
        # Do not use Decimal.normalize(): ambient precision can round long IDs.
        text=format(number,'f')
        if '.' in text: text=text.rstrip('0').rstrip('.')
        return '0' if number==0 else text
    return json.dumps(value,ensure_ascii=False,allow_nan=False)


def income_observation_digest(payload):
    """Known numeric wire fields normalize; unknown fields retain JSON semantics.

    Original payload/hash are never replaced. This digest only decides whether
    an observation is a retry. DB raw guard implements the same equivalence.
    """
    normalized=dict(payload)
    for key in ('income','tranId','tradeId','time'):
        if key in normalized and normalized[key] is not None:
            value=normalized[key]
            if key!='income' and value=='':
                normalized[key]=None
                continue
            number=Decimal(str(value))
            if not number.is_finite() or (key!='income' and number!=number.to_integral_value()):
                raise ValueError('Invalid numeric income field: '+key)
            normalized[key]=number
    return hashlib.sha256(_canonical_json(normalized).encode()).hexdigest()


async def commit_income_observations(db, account_id, queries, observations):
    """One lease, session lock, and three possible transactions; never fill writes.

    A crash/cancel/audit error leaves pending. Later successful attempts never
    resolve earlier pending/conflicted attempts. Resolution is an audited future
    operational workflow, deliberately absent from this writer.
    """
    if not db.pool: await db.connect()
    async with db.pool.acquire() as connection:
        async with income_account_lock(connection,account_id):
            attempt=str(uuid4())
            payload=json.dumps(observations,sort_keys=True,default=str,ensure_ascii=False)
            await db.execute_transaction([(_PENDING,(attempt,account_id,payload))],connection=connection)
            try:
                await db.execute_transaction(queries+[(_COMPLETE,(attempt,))],connection=connection)
            except Exception as exc:
                if getattr(exc,'sqlstate',None) not in ('P7501','P7502'):
                    # Infrastructure failure is unresolved, not an economic contradiction.
                    raise
                evidence={'old':None,'new':observations,'database_detail':getattr(exc,'detail',None)}
                evidence.update(json.loads(exc.detail))
                # The failed facts transaction has exited/rolled back before this.
                # Failure here deliberately leaves the earlier pending latch.
                await db.execute_transaction([
                    (_CONFLICT,(attempt,getattr(exc,'sqlstate',None) or type(exc).__name__,str(exc),json.dumps(evidence,sort_keys=True,default=str,ensure_ascii=False))),
                    (_MARK_CONFLICT,(attempt,)),
                ],connection=connection)
                raise
