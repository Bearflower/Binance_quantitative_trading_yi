"""Pre-pinned local Ed25519 research selection authority, never production admission.

The controlled operator retains the anchor raw SHA independently. This does not
prove global no-peeking, exchange source authenticity, or resist a malicious OS
owner who steals keys or deletes state. The first-access marker is a protected
local journal with a digest, not an independently signed timestamp. Archives may
retain later terminal qualification receipts; selection uses research metrics.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

import cryptography
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ai_tuner import hrs_exit_experiment as experiment
from ai_tuner import hrs_exit_partition_guard as guard
from ai_tuner.replay.models import DataBlocked

VERSION = 'hrs-local-selection-freeze-v1'
CONTRACT = dict(version=VERSION, no_worse_all_tiers=['nav_return','net_pnl_per_entry_risk',
    'max_drawdown','longest_losing_streak','worst_lifecycle_r','without_best_lifecycle_net_pnl'],
    strict_any_tier=['max_drawdown','longest_losing_streak','net_pnl_per_entry_risk'],
    rank=['baseline.max_drawdown','baseline.longest_losing_streak','-baseline.net_pnl_per_entry_risk','candidate_id'],
    fallback='same_mode_baseline')
_KNOWN = {'anchor.json','private.key','authority.lock','research.json','freeze.json','first-access.json'}


def _now():
    return datetime.now(timezone.utc)


def _require(value, reason):
    if not value:
        raise DataBlocked([reason])


def _bytes(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _source_identity():
    return dict(version=VERSION,cryptography_version=cryptography.__version__,
        guard_sha256=_sha(Path(guard.__file__).read_bytes()),freezer_sha256=_sha(Path(__file__).read_bytes()),
        selector_contract_sha256=experiment.fingerprint(CONTRACT),engine_identity=experiment.engine_identity())


def _safe_dir(state):
    path=Path(state)
    metadata=path.lstat()
    _require(stat.S_ISDIR(metadata.st_mode) and not path.is_symlink()
        and metadata.st_uid==os.getuid() and stat.S_IMODE(metadata.st_mode)==0o700,'authority_directory_unsafe')
    return path.resolve()


def _read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        metadata=os.fstat(fd)
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid==os.getuid()
            and stat.S_IMODE(metadata.st_mode)==0o600 and metadata.st_nlink==1,'authority_file_unsafe')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _publish(path, encoded):
    """No replacement; an interrupted write is retained and blocks recovery."""
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as stream:
            stream.write(encoded);stream.flush();os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd=os.open(path.parent,os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _registration(path,pin):
    value=json.loads(Path(path).read_bytes())
    _require(experiment.fingerprint(value)==pin,'registration_external_pin_mismatch')
    experiment.validate_registration(value)
    guard._partition(value,Path(guard.__file__).resolve().parents[1])
    return value


def initialize_authority(registration_path,state_dir,*,expected_registration_sha256):
    """Create once before research begins. Return RAW anchor pin; never key bytes."""
    r=_registration(registration_path,expected_registration_sha256)
    issued=_now()
    _require(issued<min(experiment._time(w['start']) for w in r['windows'] if w['role']=='research'),
        'authority_initialization_after_research_start')
    state=Path(state_dir)
    state.mkdir(mode=0o700)  # Existing, partial or linked directories never rekey.
    state=_safe_dir(state)
    key=Ed25519PrivateKey.generate()
    public=key.public_key().public_bytes_raw()
    anchor=dict(version=VERSION,state_dir=str(state),registration_sha256=expected_registration_sha256,
        public_key=public.hex(),key_fingerprint=_sha(public),source_identity=_source_identity(),initialized_at=issued.isoformat())
    _publish(state/'private.key',key.private_bytes_raw())
    _publish(state/'authority.lock',b'')
    encoded=_bytes(anchor);_publish(state/'anchor.json',encoded)
    return _sha(encoded)


@contextmanager
def _locked(registration_path,state_dir,pin,registration_pin):
    """Both signer and public verifier own this exact fixed-directory flock."""
    try:
        state=_safe_dir(state_dir)
        fd=os.open(state/'authority.lock',os.O_RDONLY|os.O_NOFOLLOW)
        try:
            m=os.fstat(fd)
            _require(stat.S_ISREG(m.st_mode) and m.st_uid==os.getuid()
                and stat.S_IMODE(m.st_mode)==0o600 and m.st_nlink==1,'authority_lock_unsafe')
            fcntl.flock(fd,fcntl.LOCK_EX)
            _require(not ({p.name for p in state.iterdir()}-_KNOWN),'authority_unknown_partial_state')
            encoded=_read(state/'anchor.json')
            _require(isinstance(pin,str) and len(pin)==64 and _sha(encoded)==pin,'authority_external_pin_mismatch')
            anchor=json.loads(encoded)
            r=_registration(registration_path,registration_pin)
            _require(anchor['state_dir']==str(state) and anchor['registration_sha256']==registration_pin,'authority_binding_mismatch')
            _require(anchor['version']==VERSION and anchor['source_identity']==_source_identity(),'authority_code_or_engine_changed')
            _require(anchor['key_fingerprint']==_sha(bytes.fromhex(anchor['public_key'])),'authority_key_fingerprint_invalid')
            _require(experiment._time(anchor['initialized_at'])<min(experiment._time(w['start']) for w in r['windows'] if w['role']=='research'),
                'authority_initialization_after_research_start')
            yield state,anchor,r
        finally:
            os.close(fd)
    except (OSError,ValueError,KeyError,TypeError,ArithmeticError) as error:
        raise DataBlocked(['authority_state_invalid:'+type(error).__name__]) from error


def _metrics(row,tier):
    v=row['tiers'][tier];n=v['nav'];l=v['lifecycles']
    values=tuple(Decimal(str(x)) for x in (n['nav_return'],l['net_pnl_per_entry_risk'],
        -Decimal(str(n['max_drawdown'])),-Decimal(str(l['longest_losing_streak'])),l['worst_lifecycle_r'],l['without_best_lifecycle_net_pnl']))
    _require(all(x.is_finite() for x in values),'research_nonfinite_metrics')
    return values


def select_candidates(registration,report):
    """Fixed research-only objective; no holdout scores or caller choice."""
    selection={}
    for mode in sorted({c['mode'] for c in registration['candidates']}):
        rows=[row for row in report['evaluations'] if row['mode']==mode]
        baselines=[row for row in rows if row['is_baseline']]
        _require(len(baselines)==1,'research_baseline_ambiguous')
        base=baselines[0]; eligible=[]
        for row in rows:
            comparisons=[(_metrics(row,t),_metrics(base,t)) for t in experiment.TIERS]
            if (all(all(a>=b for a,b in zip(left,right)) for left,right in comparisons)
                and any(any(left[i]>right[i] for i in (1,2,3)) for left,right in comparisons)):
                eligible.append(row)
        def rank(row):
            m=_metrics(row,'baseline')
            return (-m[2],-m[3],-m[1],row['candidate_id'])
        selection[mode]=(min(eligible,key=rank) if eligible else base)['candidate_id']
    return selection


def _qualified(registration,report):
    _require(report.get('status')=='AVAILABLE' and not report.get('blocked_reasons'),'research_not_available')
    sidecar=dict(report.get('partition_guard',{}));digest=sidecar.pop('sha256',None)
    _require(digest==experiment.fingerprint(sidecar) and report.get('guard_sha256')==_source_identity()['guard_sha256']
        and sidecar.get('guard_sha256')==report['guard_sha256'] and sidecar.get('window_sources')==report.get('window_sources'),
        'research_guard_receipt_invalid')
    for row in report.get('evaluations',[]):
        for tier in row.get('tiers',{}).values():
            _require(tier.get('nav',{}).get('status')=='AVAILABLE' and not tier['nav'].get('blocked_reasons')
                and tier.get('lifecycles',{}).get('status')=='AVAILABLE','research_metrics_unqualified')
    selection=dict(registration_sha256=experiment.fingerprint(registration),research_report_sha256=experiment.fingerprint(report),
        candidate_by_mode=select_candidates(registration,report))
    gaps=guard._research_gaps(registration,selection,report,experiment.engine_identity())
    _require(not gaps,'research_unqualified:'+','.join(gaps))
    for w in registration['windows']:
        if w['role']!='research': continue
        source=report['partition_guard']['window_sources'].get(w['window_id'])
        boundary=report.get('window_boundaries',{}).get(w['window_id'])
        _require(source and boundary and source.get('boundary')==boundary,'research_boundary_identity_missing')
        _require(experiment._time(boundary['actual_end'])<=_now(),'research_actual_window_not_elapsed')
        for end in ('start','end'):
            resolution=boundary.get(end+'_resolution',{})
            _require(resolution.get('selected_evidence_id') and resolution.get('selected_payload_sha256'),'research_boundary_evidence_missing')
    return selection


def freeze_research(registration_path,state_dir,*,expected_anchor_sha256,expected_registration_sha256,
                    expected_snapshot_manifest_sha256=None,deployment_facts=None,expected_deployment_facts_sha256=None):
    with _locked(registration_path,state_dir,expected_anchor_sha256,expected_registration_sha256) as (state,anchor,r):
        _require(not any(os.path.lexists(state/name) for name in ('research.json','freeze.json','first-access.json')),'freeze_or_access_already_exists')
        key=Ed25519PrivateKey.from_private_bytes(_read(state/'private.key'))
        _require(key.public_key().public_bytes_raw().hex()==anchor['public_key'],'authority_private_key_mismatch')
        report=guard.run_guard(registration_path,expected_registration_sha256=expected_registration_sha256,
            execute=True,stage='research',expected_snapshot_manifest_sha256=expected_snapshot_manifest_sha256,
            deployment_facts=deployment_facts,expected_deployment_facts_sha256=expected_deployment_facts_sha256)
        selection=_qualified(r,report)
        for source in report['partition_guard']['window_sources'].values():
            _require(source.get('manifest_file_sha256')==expected_snapshot_manifest_sha256,'research_external_pin_mismatch')
        _require(_registration(registration_path,expected_registration_sha256)==r and anchor['source_identity']==_source_identity(),'freeze_inputs_changed')
        raw=_bytes(report)
        payload=dict(version=VERSION,anchor_sha256=expected_anchor_sha256,registration_sha256=expected_registration_sha256,
            research_raw_sha256=_sha(raw),research_canonical_sha256=experiment.fingerprint(report),selection=selection,
            candidates_sha256=experiment.fingerprint(r['candidates']),source_identity=anchor['source_identity'],
            research_sources=report['partition_guard']['window_sources'],research_boundaries=report['window_boundaries'],issued_at=_now().isoformat(),
            source_attested=False,performance_accepted=False,official_run_ready=False)
        receipt=dict(payload=payload,signature=key.sign(_bytes(payload)).hex())
        # Under the shared lock, partial publication is always a blocking state.
        _publish(state/'research.json',raw)
        _publish(state/'freeze.json',_bytes(receipt))
        return receipt


@contextmanager
def authenticated_holdout(registration_path,state_dir,*,expected_anchor_sha256,expected_registration_sha256,
                          expected_holdout_manifest_sha256=None):
    """Authenticate then journal BEFORE the caller can read/stat holdout files.

    The public verifier never opens or stats the signing key. Keep this context
    (the shared flock) open throughout the guarded holdout execution.
    """
    with _locked(registration_path,state_dir,expected_anchor_sha256,expected_registration_sha256) as (state,anchor,r):
        raw=_read(state/'freeze.json');receipt=json.loads(raw);payload=receipt['payload']
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(anchor['public_key'])).verify(bytes.fromhex(receipt['signature']),_bytes(payload))
        except (InvalidSignature,ValueError,KeyError) as error:
            raise DataBlocked(['freeze_signature_invalid']) from error
        _require(payload['version']==VERSION and payload['anchor_sha256']==expected_anchor_sha256
            and payload['registration_sha256']==expected_registration_sha256 and payload['source_identity']==anchor['source_identity']
            and payload['candidates_sha256']==experiment.fingerprint(r['candidates']),'freeze_authority_binding_mismatch')
        report_raw=_read(state/'research.json');report=json.loads(report_raw)
        _require(payload['research_raw_sha256']==_sha(report_raw) and payload['research_canonical_sha256']==experiment.fingerprint(report),'freeze_research_hash_mismatch')
        selection=_qualified(r,report)
        _require(payload['selection']==selection and payload['research_sources']==report['partition_guard']['window_sources']
            and payload['research_boundaries']==report['window_boundaries'],'freeze_selection_mismatch')
        _require(experiment._time(payload['issued_at'])<=_now() and experiment._time(payload['issued_at'])>=experiment._time(anchor['initialized_at']),
            'freeze_issuance_invalid')
        windows={w['window_id'] for w in r['windows'] if w['role']=='holdout'}
        pins=expected_holdout_manifest_sha256
        _require(isinstance(pins,dict) and set(pins)==windows and all(isinstance(v,str) and guard._HASH.fullmatch(v) for v in pins.values()),
            'holdout_external_manifest_pins_required')
        binding=dict(anchor_sha256=expected_anchor_sha256,registration_sha256=expected_registration_sha256,
            guard_sha256=anchor['source_identity']['guard_sha256'],freeze_raw_sha256=_sha(raw),holdout_manifest_pins=pins)
        marker_path=state/'first-access.json'
        if os.path.lexists(marker_path):
            marker=json.loads(_read(marker_path));digest=marker.pop('content_sha256')
            _require(digest==experiment.fingerprint(marker) and marker['binding']==binding,'first_access_marker_mismatch')
        else:
            marker=dict(binding=binding,first_controlled_access_at=_now().isoformat(),kind='protected_local_journal_not_signed_timestamp')
            marker['content_sha256']=experiment.fingerprint(marker)
            _publish(marker_path,_bytes(marker))
        yield dict(selection=selection,research_report=report,freeze_raw_sha256=_sha(raw))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('initialize','freeze'))
    parser.add_argument('--registration',required=True)
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--expected-registration-sha256',required=True)
    parser.add_argument('--expected-anchor-sha256')
    parser.add_argument('--expected-snapshot-manifest-sha256')
    args=parser.parse_args(argv)
    try:
        if args.action=='initialize':
            pin=initialize_authority(args.registration,args.state_dir,expected_registration_sha256=args.expected_registration_sha256)
            print(json.dumps(dict(anchor_raw_sha256=pin)))
        else:
            receipt=freeze_research(args.registration,args.state_dir,expected_registration_sha256=args.expected_registration_sha256,
                expected_anchor_sha256=args.expected_anchor_sha256,expected_snapshot_manifest_sha256=args.expected_snapshot_manifest_sha256)
            print(json.dumps(dict(status='FROZEN',freeze_sha256=experiment.fingerprint(receipt))))
        return 0
    except (DataBlocked,OSError) as error:
        print(json.dumps(dict(status='BLOCKED_DATA',reason=error.gaps if isinstance(error,DataBlocked) else type(error).__name__)))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
