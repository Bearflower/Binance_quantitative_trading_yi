"""HRS-only immutable policy CAS for the user-approved minimum-order risk floor.

Host stdlib only. Preview is read-only; --apply inserts revision7 plus audit,
never changes capital, reservations, claims, existing policy rows or expiry.
"""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import hashlib,json,os
from pathlib import Path
import re,subprocess

FLOOR='hrs_minimum_executable_v1'

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def _previous(p):
    if (not isinstance(p,dict) or p.get('account_id')!='default' or p.get('strategy_id')!='hrs'
        or type(p.get('revision')) is not int or p['revision']!=6 or p.get('mode')!='reduced'
        or p.get('allow_add') is not False or p.get('freeze_allocation') is not True):
        raise ValueError('unapproved_previous_policy')
    for name,wanted in [('multiplier','.5'),('ratio_cap','.15'),('entry_ceiling','26.18')]:
        value=p.get(name)
        if isinstance(value,bool) or Decimal(str(value))!=Decimal(wanted):raise ValueError('unapproved_previous_policy')
    for name in ('effective_at','expires_at','created_at'):
        value=datetime.fromisoformat(p[name].replace('Z','+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:raise ValueError('unproven_policy_clock')
    if not isinstance(p.get('reason'),str) or not p['reason'].strip():raise ValueError('previous_reason_missing')
    return p

def _reason(sha):
    if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('release_sha_invalid')
    return canonical(dict(hrs_risk_budget_version=FLOOR,release_sha=sha,
        authorization='user_requested_minimum_executable_order_floor_2026-11-19',
        base_single_ratio='0.01',base_portfolio_ratio='0.03',
        account_margin_and_slots_unchanged=True,sltp_unchanged=True))

def prepare_candidate(previous,release_sha):
    _previous(previous)
    return dict(schema_version=1,account_id='default',strategy_id='hrs',expected_revision=6,new_revision=7,
        previous_policy=json.loads(canonical(previous)),previous_policy_sha256=hashlib.sha256(canonical(previous).encode()).hexdigest(),
        release_sha=release_sha,new_reason=_reason(release_sha))

def _checked(c):
    if (not isinstance(c,dict) or c.get('schema_version')!=1 or c.get('account_id')!='default'
        or c.get('strategy_id')!='hrs' or c.get('expected_revision')!=6 or c.get('new_revision')!=7):
        raise ValueError('candidate_contract_invalid')
    p=_previous(c['previous_policy'])
    if c.get('previous_policy_sha256')!=hashlib.sha256(canonical(p).encode()).hexdigest():raise ValueError('previous_policy_changed')
    if c.get('new_reason')!=_reason(c['release_sha']):raise ValueError('budget_reason_changed')
    return p

def _literal(s):return "'"+s.replace("'","''")+"'"

def activation_sql(candidate,*,rollback=False):
    p=_checked(candidate);prior=_literal(canonical(p));reason=_literal(candidate['new_reason'])
    body=f"""
DECLARE old_policy trading.strategy_risk_policy%ROWTYPE;
        active_revision bigint; max_revision bigint;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('default',7183));
 SELECT revision INTO active_revision FROM trading.strategy_risk_active
 WHERE account_id='default' AND strategy_id='hrs' FOR UPDATE;
 SELECT * INTO old_policy FROM trading.strategy_risk_policy
 WHERE account_id='default' AND strategy_id='hrs' AND revision=6;
 IF NOT FOUND OR to_jsonb(old_policy)<>{prior}::jsonb THEN
  RAISE EXCEPTION 'previous_policy_changed';
 END IF;
 SELECT max(revision) INTO max_revision FROM trading.strategy_risk_policy
 WHERE account_id='default' AND strategy_id='hrs';
"""
    if rollback:
        body+=f"""
 IF active_revision<>7 OR max_revision<>7 OR NOT EXISTS(
  SELECT 1 FROM trading.strategy_risk_policy WHERE account_id='default' AND strategy_id='hrs'
  AND revision=7 AND reason={reason}) THEN RAISE EXCEPTION 'rollback_policy_changed'; END IF;
 UPDATE trading.strategy_risk_active SET revision=6 WHERE account_id='default' AND strategy_id='hrs' AND revision=7;
 INSERT INTO trading.strategy_risk_audit(account_id,strategy_id,previous_revision,revision,reason)
 VALUES('default','hrs',7,6,'rollback of minimum executable budget; original policy retained');
"""
    else:
        body+=f"""
 IF active_revision=7 AND max_revision=7 AND EXISTS(SELECT 1 FROM trading.strategy_risk_policy
  WHERE account_id='default' AND strategy_id='hrs' AND revision=7 AND reason={reason}) THEN RETURN; END IF;
 IF active_revision<>6 OR max_revision<>6 OR old_policy.expires_at<=clock_timestamp() THEN
  RAISE EXCEPTION 'active_or_max_revision_changed_or_expired';
 END IF;
 INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,
  mode,allow_add,multiplier,ratio_cap,entry_ceiling,freeze_allocation,reason)
 VALUES(old_policy.account_id,old_policy.strategy_id,7,clock_timestamp(),old_policy.expires_at,
  old_policy.mode,old_policy.allow_add,old_policy.multiplier,old_policy.ratio_cap,
  old_policy.entry_ceiling,old_policy.freeze_allocation,{reason});
 UPDATE trading.strategy_risk_active SET revision=7 WHERE account_id='default' AND strategy_id='hrs' AND revision=6;
 IF NOT FOUND THEN RAISE EXCEPTION 'active_policy_cas_failed'; END IF;
 INSERT INTO trading.strategy_risk_audit(account_id,strategy_id,previous_revision,revision,reason)
 VALUES('default','hrs',6,7,{reason});
"""
    body+='END;'
    tag='$hrs_floor_'+hashlib.sha256(body.encode()).hexdigest()+'$'
    while tag in body:tag=tag[:-1]+'x$'
    return 'BEGIN;\nDO '+tag+'\n'+body+'\n'+tag+';\nCOMMIT;\n'

def _psql(sql):
    result=subprocess.run(['docker','exec','-i','trading_system-postgres','psql','-X','-v','ON_ERROR_STOP=1',
        '-U','trading_migration_admin','-d','trading_platform','-t','-A'],input=sql.encode(),capture_output=True,timeout=35)
    if result.returncode:raise RuntimeError('policy_database_operation_failed')
    return result.stdout.decode()

def read_active():
    text=_psql("SELECT row_to_json(p) FROM trading.strategy_risk_policy p JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision) WHERE p.account_id='default' AND p.strategy_id='hrs';")
    return json.loads(text)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--release-sha');parser.add_argument('--apply',action='store_true');parser.add_argument('--rollback',action='store_true');args=parser.parse_args()
    if args.rollback and not args.apply:raise ValueError('rollback_requires_explicit_apply')
    if not args.apply:
        c=prepare_candidate(read_active(),args.release_sha);args.candidate.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(args.candidate,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as h:json.dump(c,h,ensure_ascii=False,indent=2)
        print(canonical(dict(status='prepared',candidate=str(args.candidate),revision=7,expires_at=c['previous_policy']['expires_at'],budget_version=FLOOR)))
        return
    c=json.loads(args.candidate.read_text());_psql(activation_sql(c,rollback=args.rollback));row=read_active()
    expected=6 if args.rollback else 7
    if row['revision']!=expected or (not args.rollback and row['reason']!=c['new_reason']):raise RuntimeError('policy_activation_unproven')
    print(canonical(dict(status='rolled_back' if args.rollback else 'activated',revision=row['revision'],expires_at=row['expires_at'],
        multiplier=row['multiplier'],ratio_cap=row['ratio_cap'],entry_ceiling=row['entry_ceiling'],allow_add=row['allow_add'],freeze_allocation=row['freeze_allocation'],budget_version='hrs_percent_v1' if args.rollback else FLOOR)))

if __name__=='__main__':main()
