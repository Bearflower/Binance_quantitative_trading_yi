import copy
import json
from pathlib import Path
import pytest
from scripts.ledger_risk_manifest import COMPOSE,SERVICES,COLLECTOR_RESTORATION,digest,ReleaseBlocked

class Fixture:
    def __init__(self,root):
        self.root=root;self.calls=[];self.rows={};self.stacks={s:COMPOSE[:] for s in SERVICES}
        for name in COMPOSE:(root/name).write_text('fixture compose '+name)
        for i,service in enumerate(SERVICES+['postgres','grid-strategy','ai-tuner-snapshot','ai-tuner-funding-schedule']):
            cid=f'{i+1:064x}'
            self.rows[cid]={'Id':cid,'Image':'sha256:'+'b'*64,'Name':'/trading_system-'+service,'State':{'StartedAt':'2026-11-04T08:01:00Z'},'Config':{'Env':['PASSWORD=DO_NOT_PRINT_SECRET'],'Cmd':['private command'],'Labels':{'com.docker.compose.project':'trading_system','com.docker.compose.service':service,'com.docker.compose.config-hash':'e'*64}},'HostConfig':{'LogConfig':{'Type':'journald','Config':{}}}}
            if service=='execution-facts-collector':
                self.rows[cid]['Config']['Cmd']=COLLECTOR_RESTORATION['command'][:]
                self.rows[cid]['Config']['Healthcheck']={'Test':COLLECTOR_RESTORATION['healthcheck']['test'][:],'Interval':60000000000,'Timeout':5000000000,'Retries':2,'StartPeriod':180000000000}
    def run(self,args):
        self.calls.append(args)
        if args[:3]==['docker','ps','-a']:return '\n'.join(self.rows)
        if args[:2]==['docker','inspect']:return json.dumps([self.rows[args[2]]])
        if args[:3]==['docker','image','inspect']:return json.dumps([{'Id':'sha256:'+'b'*64,'Config':{'Cmd':['private command']}}])
        if args[:2]==['docker','exec']:
            assert args[3]=='sha256sum'
            return '\n'.join('c'*64+'  '+path for path in args[4:])
        if args[:2]==['docker','compose']:
            if '--hash' in args:return args[-1]+' '+'e'*64
            return json.dumps({'services':{s:dict(COLLECTOR_RESTORATION,environment={'PASSWORD':'DO_NOT_PRINT_SECRET'},image='old') if s=='execution-facts-collector' else {'image':'old','environment':{'PASSWORD':'DO_NOT_PRINT_SECRET'},'logging':{'driver':'journald','options':{}}} for s in SERVICES}})
        raise AssertionError(args)

def test_complete_redacted_readonly_evidence(tmp_path):
    from scripts.capture_ledger_risk_evidence import capture
    f=Fixture(tmp_path);result=capture(tmp_path,f.stacks,run=f.run,free_bytes=20*1024**3)
    encoded=json.dumps(result)
    assert 'DO_NOT_PRINT_SECRET' not in encoded and 'Env' not in encoded and 'private command' not in encoded
    assert set(result['containers'])=={r['Config']['Labels']['com.docker.compose.service'] for r in f.rows.values()}
    assert set(result['service_config_sha256'])==set(SERVICES)
    assert result['containers']['hrs-strategy']['source_sha256']['strategies/hrs/risk_budget.py']=='c'*64
    row=next(r for r in f.rows.values() if r['Config']['Labels']['com.docker.compose.service']=='grid-strategy')
    assert result['containers']['grid-strategy']['config_sha256']==digest({'Config':row['Config'],'HostConfig':row['HostConfig']})
    assert all(call[1] in ('ps','inspect','exec','compose','image') for call in f.calls)
    assert all('up' not in call and 'stop' not in call and 'run' not in call for call in f.calls)

@pytest.mark.parametrize('failure',['secret_command','missing_target','duplicate_service','low_disk','unsafe_stack','drift'])
def test_unknown_or_inconsistent_evidence_fails_without_payload(tmp_path,failure):
    from scripts.capture_ledger_risk_evidence import capture
    f=Fixture(tmp_path);free=20*1024**3
    if failure=='secret_command':next(r for r in f.rows.values() if r['Config']['Labels']['com.docker.compose.service']=='execution-facts-collector')['Config']['Cmd']=['PASSWORD=DO_NOT_PRINT_SECRET']
    if failure=='missing_target':del f.rows[next(iter(f.rows))]
    if failure=='duplicate_service':f.rows['d'*64]=copy.deepcopy(next(iter(f.rows.values())));f.rows['d'*64]['Id']='d'*64
    if failure=='low_disk':free=1
    if failure=='unsafe_stack':f.stacks[SERVICES[0]]=['/tmp/private.yml']
    def run(args):
        result=f.run(args)
        if failure=='drift' and args[:2]==['docker','compose']:(tmp_path/COMPOSE[0]).write_text('changed')
        return result
    with pytest.raises(ReleaseBlocked) as error:capture(tmp_path,f.stacks,run=run,free_bytes=free)
    assert 'DO_NOT_PRINT_SECRET' not in str(error.value)

def test_command_failure_never_echoes_secret(monkeypatch):
    from scripts.capture_ledger_risk_evidence import read_command
    from types import SimpleNamespace
    import scripts.capture_ledger_risk_evidence as module
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout='PASSWORD=SECRET',stderr='postgresql://secret'))
    with pytest.raises(ReleaseBlocked) as exc:read_command(['docker','inspect','test'])
    assert 'SECRET' not in str(exc.value) and 'postgresql' not in str(exc.value)

def test_cli_errors_emit_no_evidence_or_exception_payload(tmp_path,monkeypatch,capsys):
    import scripts.capture_ledger_risk_evidence as module
    stacks=tmp_path/'stacks.json';stacks.write_text('{}')
    def fail(*a,**kw):raise ValueError('PASSWORD=DO_NOT_PRINT_SECRET')
    monkeypatch.setattr(module,'capture',fail)
    assert module.main(['--root',str(tmp_path),'--stacks-file',str(stacks)])==2
    output=capsys.readouterr()
    assert output.out=='' and 'DO_NOT_PRINT_SECRET' not in output.err
    assert json.loads(output.err)['error']=='evidence_capture_blocked'

@pytest.mark.parametrize('field',['Healthcheck','LogConfig'])
def test_collector_unknown_fields_not_emitted(tmp_path,field):
    from scripts.capture_ledger_risk_evidence import capture
    f=Fixture(tmp_path);row=next(r for r in f.rows.values() if r['Config']['Labels']['com.docker.compose.service']=='execution-facts-collector')
    row['Config' if field=='Healthcheck' else 'HostConfig'][field]={'credential':'DO_NOT_PRINT_SECRET'}
    with pytest.raises(ReleaseBlocked) as exc:capture(tmp_path,f.stacks,run=f.run,free_bytes=20*1024**3)
    assert 'DO_NOT_PRINT_SECRET' not in str(exc.value)

@pytest.mark.parametrize('label',['f'*64,None,''])
def test_different_label_requires_successful_semantic_proof(tmp_path,label):
    from scripts.capture_ledger_risk_evidence import capture
    f=Fixture(tmp_path)
    row=next(r for r in f.rows.values() if r['Config']['Labels']['com.docker.compose.service']=='hrs-strategy')
    row['Config']['Labels']['com.docker.compose.config-hash']=label
    result=capture(tmp_path,f.stacks,run=f.run,free_bytes=20*1024**3)
    assert result['compose_hash_matches']['hrs-strategy'] is False
    row['Config']['Cmd']=['changed actual command']
    with pytest.raises(ReleaseBlocked):capture(tmp_path,f.stacks,run=f.run,free_bytes=20*1024**3)

def test_semantic_failure_cli_exposes_category_not_values(tmp_path,monkeypatch,capsys):
    import scripts.capture_ledger_risk_evidence as module
    stacks=tmp_path/'stacks.json';stacks.write_text('{}')
    def fail(*a,**kw):raise ReleaseBlocked('runtime/effective configuration mismatch or unsupported field: environment')
    monkeypatch.setattr(module,'capture',fail)
    assert module.main(['--stacks-file',str(stacks)])==2
    output=capsys.readouterr();assert not output.out
    assert json.loads(output.err)['reason'].endswith('environment')
