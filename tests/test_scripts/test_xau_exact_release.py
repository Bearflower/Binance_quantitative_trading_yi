"""精确容器切换的状态机与配置守护。"""
import copy
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('release',ROOT/'deployment/xau-attribution/activate.py')
release=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(release)
OLD='a'*64;NEW='b'*64;SHA='c'*40
NAME='trading_system-mtcps-xau-shadow'


def runtime():
    return {'Id':OLD,'Name':'/'+NAME,'Image':'sha256:'+'d'*64,
        'Config':{'Image':'old','Hostname':OLD[:12],'Env':['SECRET=literal$token','SHADOW_SKIP_INITIAL_COLLECTION=1'],
                  'Labels':{'com.docker.compose.project':'trading_system'},'Healthcheck':{'Test':['CMD','true']}},
        'HostConfig':{'Binds':['/logs:/app/logs:rw'],'Memory':123,'OomKillDisable':None,
                      'RestartPolicy':{'Name':'unless-stopped','MaximumRetryCount':0}},
        'Mounts':[{'Type':'bind','Source':'/logs','Destination':'/app/logs','RW':True,'Mode':'rw','Propagation':'rprivate'}],
        'NetworkSettings':{'Networks':{'net':{'Aliases':[NAME,'mtcps-xau-shadow'],'IPAMConfig':None,'DriverOpts':None}}},
        'State':{'Status':'running','Running':True,'Health':{'Status':'healthy'}},'RestartCount':0}


def test_clone_is_exact_and_stopped_restart_policy():
    assert hasattr(release,'clone_body'), '不能用 Compose 按 service 标签枚举替换'
    old=runtime();before=copy.deepcopy(old)
    body=release.clone_body(old,'sha256:'+'e'*64,SHA)
    assert old==before
    assert body['Env']==old['Config']['Env']
    assert body['HostConfig']['RestartPolicy']=={'Name':'no','MaximumRetryCount':0}
    assert body['HostConfig']['Memory']==123
    assert body['HostConfig']['Binds']==old['HostConfig']['Binds']
    assert body['HostConfig']['OomKillDisable'] is False
    assert body['Labels']['org.opencontainers.image.revision']==SHA
    assert 'Hostname' not in body


@pytest.mark.parametrize('field,value', [('Hostname','custom-name'),('MacAddress','12:34:56:78:90:ab')])
def test_custom_identity_rejected_before_stop(field,value):
    old=runtime();old['Config'][field]=value
    with pytest.raises(ValueError):release.clone_body(old,'sha256:'+'e'*64,SHA)


@pytest.mark.parametrize('mutation', ['volume','volumes_from','ipam','extra_alias'])
def test_mount_or_network_drift_rejected(mutation):
    old=runtime()
    if mutation=='volume':old['Mounts'][0]['Type']='volume'
    elif mutation=='volumes_from':old['HostConfig']['VolumesFrom']=['other']
    elif mutation=='ipam':old['NetworkSettings']['Networks']['net']['IPAMConfig']={'IPv4Address':'1.2.3.4'}
    else:
        new=DockerFake(release).inspect(NEW)
        release.validate_clone(old,new,'sha256:'+'e'*64,SHA,staged=True)
        new['NetworkSettings']['Networks']['net']['Aliases'].append('unexpected')
        with pytest.raises(ValueError,match='network aliases'):release.validate_clone(old,new,'sha256:'+'e'*64,SHA,staged=True)
        return
    with pytest.raises(ValueError):release.clone_body(old,'sha256:'+'e'*64,SHA)


def test_preexisting_stopped_backups_are_not_exempt():
    before={NAME:{'id':OLD},NAME+'-before-full-old':{'id':'f'*64}}
    after=copy.deepcopy(before);after.pop(NAME+'-before-full-old')
    with pytest.raises(ValueError):release.verify_excluded(before,after)


def test_funding_is_not_a_target():
    assert set(release.TARGETS)=={'mtcps-xau-shadow','mtcps-xau-calibrated-shadow'}
    assert set(release.FILES)=={'shadow'}


class DockerFake:
    """按精确 ID 操作的最小 Docker 状态机；保留同标签停机备份。"""
    def __init__(self, module, fail_at=None, after=False):
        self.module=module;self.fail_at=fail_at;self.after=after;self.calls=0;self.failed=False
        self.allowed={OLD,NEW}
        old=runtime();body=module.clone_body(old,'sha256:'+'e'*64,SHA)
        candidate=copy.deepcopy(old)
        candidate.update(Id=NEW,Name='/'+NAME+'-ready-xau-'+SHA[:8],Image=body['Image'])
        candidate['Config']={k:v for k,v in body.items() if k not in ('HostConfig','NetworkingConfig')}
        candidate['Config']['Hostname']=NEW[:12]
        candidate['HostConfig']=body['HostConfig']
        candidate['State']={'Status':'created','Running':False,'Health':{'Status':'starting'},'StartedAt':'now'}
        backup=copy.deepcopy(old);backup.update(Id='9'*64,Name='/'+NAME+'-before-full-previous')
        backup['State']['Running']=False;backup['State']['Status']='exited'
        self.rows={OLD:copy.deepcopy(old),NEW:candidate,backup['Id']:backup}
        self.backup=copy.deepcopy(backup)
        self.plan={'name':NAME,'old':old,'new_id':NEW,'new_image':body['Image'],
                   'backup_name':NAME+'-before-xau-'+SHA[:8],
                   'stage_name':candidate['Name'].lstrip('/'),'old_hashes':{}}
        self.before=self.snapshot();self.before.pop(candidate['Name'].lstrip('/'))

    def inspect(self,name):
        if name in self.rows:return copy.deepcopy(self.rows[name])
        return copy.deepcopy(next(v for v in self.rows.values() if v['Name'].lstrip('/')==name))

    def snapshot(self):
        return {v['Name'].lstrip('/'):{'id':v['Id'],'image':v['Image'],'running':v['State']['Running'],
                 'restarts':v['RestartCount'],'health':v['State']['Health']['Status']} for v in self.rows.values()}

    def run(self,*args,**kwargs):
        assert args[0]=='docker' and args[1] in ('update','stop','rename','start','logs','rm'),args
        if args[1]=='logs':return ''
        target=args[2] if args[1]=='rename' else args[-1]
        assert target in self.allowed,'不得按服务名或同标签备份执行变更'
        self.calls+=1
        trigger=not self.failed and self.calls==self.fail_at
        if trigger and not self.after:
            self.failed=True;raise RuntimeError('injected transition failure')
        row=self.rows[target]
        if args[1]=='update':row['HostConfig']['RestartPolicy']={'Name':args[2].split('=',1)[1],'MaximumRetryCount':0}
        elif args[1]=='stop':row['State'].update(Running=False,Status='exited')
        elif args[1]=='start':row['State'].update(Running=True,Status='running',StartedAt='now',Health={'Status':'healthy'})
        elif args[1]=='rename':
            assert not any(v['Name']=='/'+args[3] for v in self.rows.values())
            row['Name']='/'+args[3]
        elif args[1]=='rm':
            assert not row['State']['Running'];self.rows.pop(target)
        if trigger and self.after:
            self.failed=True;raise RuntimeError('injected uncertain command result')
        return ''

    def wire(self,monkeypatch):
        monkeypatch.setattr(self.module,'inspect',self.inspect)
        monkeypatch.setattr(self.module,'snapshot',self.snapshot)
        monkeypatch.setattr(self.module,'run',self.run)
        def health(name,image,**kwargs):
            row=self.inspect(name);assert row['Image']==image and row['State']['Running'];return row
        monkeypatch.setattr(self.module,'wait_health',health)
        monkeypatch.setattr(self.module,'verify_files',lambda *args:None)
        monkeypatch.setattr(self.module,'verify_version',lambda *args:None)


@pytest.mark.parametrize('fail_at',range(1,7))
@pytest.mark.parametrize('after',[False,True])
def test_each_transition_failure_restores_original_id_without_touching_backups(monkeypatch,tmp_path,fail_at,after):
    fake=DockerFake(release,fail_at,after);fake.wire(monkeypatch)
    with pytest.raises(RuntimeError,match='injected'):
        release.execute_plans([fake.plan],tmp_path,{'git_sha':SHA,'files':{'shadow':{}}},fake.before)
    assert fake.inspect(NAME)['Id']==OLD
    assert fake.inspect(OLD)['HostConfig']['RestartPolicy']['Name']=='unless-stopped'
    assert fake.inspect(OLD)['State']['Running']
    assert fake.rows['9'*64]==fake.backup
    assert NEW not in fake.rows
    assert fake.plan['phase']=='candidate_removed'


def test_success_preserves_stopped_original_and_every_other_container(monkeypatch,tmp_path):
    fake=DockerFake(release);fake.wire(monkeypatch)
    release.execute_plans([fake.plan],tmp_path,{'git_sha':SHA,'files':{'shadow':{}}},fake.before)
    assert fake.inspect(NAME)['Id']==NEW
    assert fake.inspect(OLD)['Name'].endswith('-before-xau-'+SHA[:8])
    assert not fake.inspect(OLD)['State']['Running']
    assert fake.inspect(OLD)['HostConfig']['RestartPolicy']['Name']=='no'
    assert fake.rows['9'*64]==fake.backup


def test_validation_failure_rolls_back_same_original_container(monkeypatch,tmp_path):
    fake=DockerFake(release);fake.wire(monkeypatch)
    monkeypatch.setattr(release,'verify_version',lambda *a: (_ for _ in ()).throw(ValueError('version mismatch')))
    with pytest.raises(ValueError,match='version mismatch'):
        release.execute_plans([fake.plan],tmp_path,{'git_sha':SHA,'files':{'shadow':{}}},fake.before)
    assert fake.inspect(NAME)['Id']==OLD and fake.inspect(OLD)['State']['Running']
    assert fake.rows['9'*64]==fake.backup


def test_durable_phase_allows_fresh_process_recovery(monkeypatch,tmp_path):
    import json
    fake=DockerFake(release);fake.wire(monkeypatch)
    release.set_phase(tmp_path,fake.plan,'rename_candidate')
    fake.run('docker','update','--restart=no',OLD)
    fake.run('docker','stop','-t','60',OLD)
    fake.run('docker','rename',OLD,fake.plan['backup_name'])
    fake.run('docker','rename',NEW,NAME)
    # 丢弃内存 plan，模拟断电后仅从磁盘恢复，并确认仍未启动的候选被移开。
    saved=json.loads((tmp_path/(NAME+'.state.json')).read_text())
    release.restore_plan(saved,tmp_path)
    assert fake.inspect(NAME)['Id']==OLD and fake.inspect(OLD)['State']['Running']
    assert fake.rows['9'*64]==fake.backup


def test_rollback_failure_retains_both_exact_containers_and_evidence(monkeypatch,tmp_path):
    fake=DockerFake(release);fake.wire(monkeypatch)
    monkeypatch.setattr(release,'verify_version',lambda *a: (_ for _ in ()).throw(ValueError('version mismatch')))
    monkeypatch.setattr(release,'restore_plan',lambda *a: (_ for _ in ()).throw(ValueError('recovery unavailable')))
    with pytest.raises(RuntimeError,match='rollback requires attention'):
        release.execute_plans([fake.plan],tmp_path,{'git_sha':SHA,'files':{'shadow':{}}},fake.before)
    assert OLD in fake.rows and NEW in fake.rows
    assert fake.rows['9'*64]==fake.backup
    assert (tmp_path/'rollback.json').exists()


def test_recover_command_handles_candidate_creation_without_ack_and_is_idempotent(monkeypatch,tmp_path):
    fake=DockerFake(release);fake.wire(monkeypatch)
    monkeypatch.setattr(release,'ROOT',tmp_path)
    directory=tmp_path/'.releases/xau-attribution'/SHA;directory.mkdir(parents=True)
    release.save(directory/'before.json',fake.before)
    plan=copy.deepcopy(fake.plan);plan.pop('new_id');plan['phase']='creating_candidate'
    release.save(directory/(NAME+'.state.json'),plan)
    release.recover_release(directory)
    assert fake.inspect(NAME)['Id']==OLD and NEW not in fake.rows
    release.recover_release(directory)
    assert fake.inspect(NAME)['Id']==OLD and fake.rows['9'*64]==fake.backup


def test_successful_release_cannot_be_rolled_back_by_recovery_command(monkeypatch,tmp_path):
    monkeypatch.setattr(release,'ROOT',tmp_path)
    directory=tmp_path/'.releases/xau-attribution'/SHA;directory.mkdir(parents=True)
    release.save(directory/'result.json',{'status':'verified'})
    with pytest.raises(ValueError,match='verified release'):
        release.recover_release(directory)


@pytest.mark.parametrize('after',[False,True])
def test_second_target_failure_restores_both_original_ids(monkeypatch,tmp_path,after):
    fake=DockerFake(release,7,after)
    second_name='trading_system-mtcps-xau-calibrated-shadow';second_old='1'*64;second_new='2'*64
    old=runtime();old.update(Id=second_old,Name='/'+second_name);old['Config']['Hostname']=second_old[:12]
    old['NetworkSettings']['Networks']['net']['Aliases']=[second_name,'mtcps-xau-calibrated-shadow']
    body=release.clone_body(old,'sha256:'+'e'*64,SHA)
    new=copy.deepcopy(old);new.update(Id=second_new,Name='/'+second_name+'-ready-xau-'+SHA[:8],Image=body['Image'])
    new['Config']={k:v for k,v in body.items() if k not in ('HostConfig','NetworkingConfig')}
    new['Config']['Hostname']=second_new[:12];new['HostConfig']=body['HostConfig']
    new['State']={'Status':'created','Running':False,'Health':{'Status':'starting'}}
    fake.rows[second_old]=copy.deepcopy(old);fake.rows[second_new]=new;fake.allowed.update((second_old,second_new))
    plan={'name':second_name,'old':old,'new_id':second_new,'new_image':body['Image'],
          'backup_name':second_name+'-before-xau-'+SHA[:8],
          'stage_name':new['Name'].lstrip('/'),'old_hashes':{}}
    before=fake.snapshot()
    for candidate in [fake.plan,plan]:before.pop(candidate['stage_name'])
    fake.wire(monkeypatch)
    with pytest.raises(RuntimeError,match='injected'):
        release.execute_plans([fake.plan,plan],tmp_path,{'git_sha':SHA,'files':{'shadow':{}}},before)
    assert fake.inspect(NAME)['Id']==OLD and fake.inspect(second_name)['Id']==second_old
    assert fake.inspect(OLD)['State']['Running'] and fake.inspect(second_old)['State']['Running']
    assert NEW not in fake.rows and second_new not in fake.rows
    assert fake.rows['9'*64]==fake.backup


def test_target_dying_after_switch_is_not_reported_as_verified(monkeypatch,tmp_path):
    fake=DockerFake(release);fake.wire(monkeypatch)
    switch=release.switch_plan
    def dying(plan,directory,hashes):
        switch(plan,directory,hashes)
        fake.rows[NEW]['State'].update(Running=False,Status='exited',Health={'Status':'unhealthy'})
    monkeypatch.setattr(release,'switch_plan',dying)
    with pytest.raises(ValueError,match='final candidate'):
        release.execute_plans([fake.plan],tmp_path,{'git_sha':SHA,'files':{'shadow':{}}},fake.before)
    assert not (tmp_path/'result.json').exists()
    assert fake.inspect(NAME)['Id']==OLD and fake.inspect(OLD)['State']['Running']


def test_drifted_backup_is_never_started_during_recovery(monkeypatch,tmp_path):
    fake=DockerFake(release);fake.wire(monkeypatch)
    fake.run('docker','stop','-t','60',OLD)
    fake.run('docker','rename',OLD,fake.plan['backup_name'])
    fake.run('docker','rename',NEW,NAME)
    fake.run('docker','start',NEW)
    fake.rows[OLD]['HostConfig']['Memory']=999
    calls=fake.calls
    with pytest.raises(ValueError,match='runtime changed'):
        release.restore_plan(fake.plan,tmp_path)
    assert not fake.inspect(OLD)['State']['Running']
    assert fake.calls==calls,'回退前置校验失败不得执行任何恢复变更'


def test_cleaned_staged_candidate_can_be_recovered_idempotently(monkeypatch,tmp_path):
    fake=DockerFake(release);fake.wire(monkeypatch)
    monkeypatch.setattr(release,'ROOT',tmp_path)
    directory=tmp_path/'.releases/xau-attribution'/SHA;directory.mkdir(parents=True)
    release.save(directory/'before.json',fake.before)
    release.set_phase(directory,fake.plan,'cleanup_candidate')
    fake.run('docker','rm',NEW)
    release.recover_release(directory)
    assert fake.inspect(NAME)['Id']==OLD and fake.rows['9'*64]==fake.backup

@pytest.mark.parametrize('failure', ['stdout', 'result_after_replace'])
def test_success_commit_boundary_never_rolls_back_verified_release(monkeypatch,tmp_path,failure):
    import builtins
    fake=DockerFake(release);fake.wire(monkeypatch)
    if failure=='stdout':
        def broken_output(*args,**kwargs):raise BrokenPipeError('stdout disconnected')
        monkeypatch.setattr(builtins,'print',broken_output)
    else:
        original_save=release.save
        def uncertain_save(path,value):
            original_save(path,value)
            if path.name=='result.json':raise OSError('directory fsync uncertain')
        monkeypatch.setattr(release,'save',uncertain_save)
    with pytest.raises(OSError):
        release.execute_plans([fake.plan],tmp_path,{'git_sha':SHA,'files':{'shadow':{}}},fake.before)
    assert fake.inspect(NAME)['Id']==NEW and fake.inspect(NEW)['State']['Running']
    assert not fake.inspect(OLD)['State']['Running']
    assert fake.rows['9'*64]==fake.backup
    assert (tmp_path/'result.json').exists()
    assert not (tmp_path/'rollback.json').exists()
