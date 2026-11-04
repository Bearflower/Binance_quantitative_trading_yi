import copy
import pytest
from scripts.ledger_risk_manifest import ReleaseBlocked

def example():
    image={'Id':'sha256:'+'a'*64,'Config':{'Env':['PATH=/bin'],'Cmd':['python','main.py'],'WorkingDir':'/app','User':''}}
    service={'image':'same-image-new-tag','container_name':'app','environment':{'SECRET':'value'},'restart':'unless-stopped','logging':{'driver':'journald','options':{}},'networks':{'network':None}}
    row={'Id':'b'*64,'Image':image['Id'],'Name':'/app','Config':{'Image':'old-tag','Env':['PATH=/bin','SECRET=value'],'Cmd':['python','main.py'],'Entrypoint':None,'WorkingDir':'/app','User':'','Labels':{}},'HostConfig':{'RestartPolicy':{'Name':'unless-stopped','MaximumRetryCount':0},'LogConfig':{'Type':'journald','Config':{}},'NetworkMode':'project_network'},'Mounts':[],'NetworkSettings':{'Networks':{'project_network':{}}}}
    return service,row,image,{'network':{'name':'project_network'}}

def test_actual_fields_allow_only_same_image_identity_alias():
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example()
    proof=verify_runtime_config(service,row,image,networks)
    assert len(proof)==64 and 'value' not in proof

@pytest.mark.parametrize('field',['environment','command','entrypoint','working_dir','user','logging','restart','ports','volumes','privileged','unknown_setting'])
def test_non_image_drift_or_unsupported_setting_is_rejected(field):
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example()
    service[field]={'changed':'SECRET'} if field in ('environment','logging') else 'changed'
    with pytest.raises(ReleaseBlocked) as exc:verify_runtime_config(service,row,image,networks)
    assert 'SECRET' not in str(exc.value)

def test_unknown_nondefault_host_setting_blocks():
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example();row['HostConfig']['UnknownPrivilege']='enabled'
    with pytest.raises(ReleaseBlocked):verify_runtime_config(service,row,image,networks)

@pytest.mark.parametrize('kind',['mount','network','health','resource','config'])
def test_uncovered_effective_or_actual_detail_blocks(kind):
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example()
    if kind=='mount':row['Mounts']=[{'Type':'volume'}]
    if kind=='network':row['NetworkSettings']['Networks']['project_network']={'Aliases':['unreviewed-alias']}
    if kind=='health':row['Config']['Healthcheck']={'Test':['CMD','wrong']}
    if kind=='resource':service['deploy']={'resources':{'limits':{'memory':'512M'}}}
    if kind=='config':row['Config']['Uncovered']='value'
    with pytest.raises(ReleaseBlocked):verify_runtime_config(service,row,image,networks)


def test_observed_tuner_resource_mapping():
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example()
    service['deploy']={'resources':{'limits':{'cpus':.5,'memory':'536870912'},'reservations':{'cpus':.1,'memory':'268435456'}},'placement':{}}
    row['HostConfig'].update(NanoCpus=500000000,Memory=536870912,MemoryReservation=268435456,CpuQuota=0,CpuPeriod=0,CpuShares=0,MemorySwap=1073741824)
    verify_runtime_config(service,row,image,networks)
    for key in ('NanoCpus','Memory','MemoryReservation','CpuQuota','CpuPeriod','CpuShares','MemorySwap'):
        bad=copy.deepcopy(row);bad['HostConfig'][key]+=1
        with pytest.raises(ReleaseBlocked):verify_runtime_config(service,bad,image,networks)

def test_compose_null_defaults_and_pull_policy_remain_bound():
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example();service.update(command=None,entrypoint=None,pull_policy='never',depends_on={'postgres':{'condition':'service_healthy','required':True}})
    verify_runtime_config(service,row,image,networks)
    service['pull_policy']='unreviewed'
    with pytest.raises(ReleaseBlocked):verify_runtime_config(service,row,image,networks)

@pytest.mark.parametrize('value',[False,0,None,'',[],{}])
def test_unknown_host_field_rejected_even_when_empty(value):
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example();row['HostConfig']['FutureUnknownSetting']=value
    with pytest.raises(ReleaseBlocked):verify_runtime_config(service,row,image,networks)

@pytest.mark.parametrize('key,value',[('Cmd',['different']),('Entrypoint',['wrapper']),('User','1001'),('Env',['PATH=/secret']),('Healthcheck',{'Test':['CMD','different']}),('WorkingDir','/other'),('Volumes',{'/new':{}})])
def test_new_image_default_changes_rejected_before_activation(key,value):
    from scripts.ledger_risk_runtime_config import verify_image_transition
    service,row,image,networks=example();new=copy.deepcopy(image);new['Id']='sha256:'+'c'*64;new['Config'][key]=value
    with pytest.raises(ReleaseBlocked):verify_image_transition(service,image,new)

def test_compose_override_prevents_hidden_image_default_changes():
    from scripts.ledger_risk_runtime_config import verify_image_transition
    service,row,image,networks=example();service.update(command=['fixed'],entrypoint=['fixed-entry'],user='0',working_dir='/fixed',environment={'PATH':'/fixed','SECRET':'value'},healthcheck={'test':['CMD','fixed']})
    new=copy.deepcopy(image);new['Id']='sha256:'+'c'*64;new['Config'].update(Cmd=['different'],Entrypoint=['different'],User='new',WorkingDir='/new',Env=['PATH=/new'],Healthcheck={'Test':['CMD','new']})
    assert len(verify_image_transition(service,image,new))==64


def test_validated_bind_mount_order_does_not_change_runtime_digest():
    from scripts.ledger_risk_runtime_config import verify_runtime_config
    service,row,image,networks=example()
    service['volumes']=[
        {'type':'bind','source':'/host/config','target':'/app/config','read_only':True},
        {'type':'bind','source':'/host/logs','target':'/app/logs'},
    ]
    row['Mounts']=[
        {'Type':'bind','Source':'/host/config','Destination':'/app/config','RW':False,'Mode':'ro','Propagation':'rprivate'},
        {'Type':'bind','Source':'/host/logs','Destination':'/app/logs','RW':True,'Mode':'rw','Propagation':'rprivate'},
    ]
    original=copy.deepcopy(row)
    proof=verify_runtime_config(service,row,image,networks)
    swapped=copy.deepcopy(row)
    swapped['Mounts'].reverse()
    assert verify_runtime_config(service,swapped,image,networks)==proof
    assert row==original  # Fingerprinting must not mutate the captured evidence.
    swapped['Config']['Env'].reverse()
    assert verify_runtime_config(service,swapped,image,networks)!=proof
    changed=copy.deepcopy(row)
    changed['Mounts'][0]['Source']='/host/other'
    with pytest.raises(ReleaseBlocked):
        verify_runtime_config(service,changed,image,networks)
