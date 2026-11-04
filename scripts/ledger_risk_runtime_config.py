"""Conservative effective-Compose versus actual Docker configuration comparison.

Values remain private. Unknown settings fail closed; errors identify categories,
never values. This is not an automatic approval of arbitrary Compose changes.
"""
from decimal import Decimal
import re
from scripts.ledger_risk_manifest import digest,ReleaseBlocked

_ALLOWED={'image','container_name','environment','command','entrypoint','working_dir','user','healthcheck','logging','restart','volumes','networks','ports','expose','depends_on','labels','deploy','cpus','mem_limit','mem_reservation','pull_policy'}
_DEFAULTS={'NetworkMode':None,'IpcMode':'private','CgroupnsMode':'private','ShmSize':67108864,'Runtime':'runc','ConsoleSize':[0,0],
 'MaskedPaths':['/proc/acpi','/proc/asound','/proc/interrupts','/proc/kcore','/proc/keys','/proc/latency_stats','/proc/sched_debug','/proc/scsi','/proc/timer_list','/proc/timer_stats','/sys/firmware','/sys/devices/virtual/powercap'],
 'ReadonlyPaths':['/proc/bus','/proc/fs','/proc/irq','/proc/sys','/proc/sysrq-trigger']}

# Explicitly known Docker HostConfig neutral values. New API fields are never
# assumed harmless merely because their current value is empty.
_EMPTY_HOST={
 **{key:(False,) for key in ('AutoRemove','Privileged','PublishAllPorts','ReadonlyRootfs','OomKillDisable','Init')},
 **{key:(0,) for key in ('OomScoreAdj','BlkioWeight','CpuRealtimePeriod','CpuRealtimeRuntime','CpuCount','CpuPercent','IOMaximumIOps','IOMaximumBandwidth')},
 **{key:('',) for key in ('ContainerIDFile','VolumeDriver','Cgroup','PidMode','UTSMode','UsernsMode','Isolation','CgroupParent','CpusetCpus','CpusetMems')},
 **{key:(None,[]) for key in ('VolumesFrom','CapAdd','CapDrop','Dns','DnsOptions','DnsSearch','ExtraHosts','GroupAdd','Links','BlkioWeightDevice','BlkioDeviceReadBps','BlkioDeviceWriteBps','BlkioDeviceReadIOps','BlkioDeviceWriteIOps','Devices','DeviceCgroupRules','DeviceRequests','Ulimits','SecurityOpt')},
 'Init':(None,False), 'OomKillDisable':(None,False), 'MemorySwappiness':(None,), 'PidsLimit':(None,0), 'StorageOpt':(None,{}), 'Sysctls':(None,{})}

def _deny(category):raise ReleaseBlocked('runtime/effective configuration mismatch or unsupported field: '+category)
def _env(values):
    if isinstance(values,dict):
        if any(value is None for value in values.values()):_deny('unresolved environment')
        return {key:str(value) for key,value in values.items()}
    result={}
    for item in values or []:
        if not isinstance(item,str) or '=' not in item:_deny('environment encoding')
        key,value=item.split('=',1)
        if key in result:_deny('duplicate environment')
        result[key]=value
    return result

def _duration(value):
    if isinstance(value,int):return value
    parts=re.findall(r'(\d+(?:\.\d+)?)(ns|us|µs|ms|h|m|s)',str(value))
    if not parts or ''.join(number+unit for number,unit in parts)!=str(value):_deny('duration')
    scale={'h':3600*10**9,'m':60*10**9,'s':10**9,'ms':10**6,'us':1000,'µs':1000,'ns':1}
    return int(sum(Decimal(number)*scale[unit] for number,unit in parts))

def _health(service,image):
    result=dict(image.get('Healthcheck') or {})
    if service is None:return result
    if not isinstance(service,dict) or set(service)-{'test','interval','timeout','retries','start_period','start_interval','disable'}:_deny('healthcheck')
    if service.get('disable'):return {'Test':['NONE']}
    for key,value in service.items():
        target={'test':'Test','interval':'Interval','timeout':'Timeout','retries':'Retries','start_period':'StartPeriod','start_interval':'StartInterval'}.get(key)
        if target:result[target]=_duration(value) if key not in ('test','retries') else value
    return result

def _memory(value):
    match=re.fullmatch(r'(\d+)([kmgt]?)(?:b)?',str(value).lower())
    if not match:_deny('memory quantity')
    return int(match[1])*1024**('kmgt'.index(match[2])+1 if match[2] else 0)

def _resources(service):
    if any(service.get(key) is not None for key in ('cpus','mem_limit','mem_reservation')):_deny('duplicate top-level resources')
    deploy=service.get('deploy') or {}
    if not isinstance(deploy,dict) or set(deploy)-{'resources','placement'} or deploy.get('placement') not in (None,{}):_deny('deploy settings')
    resources=deploy.get('resources') or {}
    if set(resources)-{'limits','reservations'}:_deny('resource groups')
    limits=resources.get('limits') or {};reserve=resources.get('reservations') or {}
    if set(limits)-{'cpus','memory'} or set(reserve)-{'cpus','memory'}:_deny('resource field')
    cpu=Decimal(str(limits.get('cpus',0)));requested=Decimal(str(reserve.get('cpus',0)))
    if not cpu.is_finite() or not requested.is_finite() or cpu<0 or requested<0 or (cpu and requested>cpu):_deny('CPU resource quantity')
    memory=_memory(limits.get('memory',0));reserved=_memory(reserve.get('memory',0))
    if memory and reserved>memory:_deny('memory reservation')
    # Standalone Compose up has no CPU-reservation HostConfig field. Preserve the
    # declaration in the evidence, require zero shares/quota/period, never claim
    # reservations.cpus is an enforced CPU guarantee.
    return {'NanoCpus':int(cpu*10**9),'Memory':memory,'MemoryReservation':reserved,'CpuShares':0,'CpuQuota':0,'CpuPeriod':0,'MemorySwap':2*memory}

def verify_runtime_config(service,row,image,networks,*,allow_collector_restoration=False):
    if not isinstance(service,dict) or set(service)-_ALLOWED:_deny('unsupported Compose field')
    if service.get('pull_policy') not in (None,'never','missing','if_not_present','always'):_deny('pull policy')
    if image.get('Id')!=row.get('Image'):_deny('resolved image identity')
    config=row['Config'];host=row['HostConfig'];defaults=image.get('Config') or {}
    known={'Hostname','Domainname','User','AttachStdin','AttachStdout','AttachStderr','ExposedPorts','Tty','OpenStdin','StdinOnce','Env','Cmd','Healthcheck','ArgsEscaped','Image','Volumes','WorkingDir','Entrypoint','OnBuild','Labels','StopSignal','StopTimeout','Shell','MacAddress'}
    if set(config)-known:_deny('uncovered Config field')
    if config.get('MacAddress'):_deny('explicit container MAC')
    if config.get('Hostname') not in (None,'',row['Id'][:12]):_deny('hostname')
    expected_env=_env(defaults.get('Env'));expected_env.update(_env(service.get('environment',{})))
    if expected_env!=_env(config.get('Env')):_deny('environment')
    for source,target,empty in [('command','Cmd',None),('entrypoint','Entrypoint',None),('working_dir','WorkingDir',''),('user','User','')]:
        expected=service[source] if service.get(source) is not None else defaults.get(target,empty)
        if source in ('command','entrypoint') and expected is not None and not isinstance(expected,list):_deny(source+' must be normalized argv')
        if source=='command' and allow_collector_restoration:continue
        if (expected or empty)!=(config.get(target) or empty):_deny(source)
    if service.get('container_name') and row.get('Name','').lstrip('/')!=service['container_name']:_deny('container_name')
    health=_health(service.get('healthcheck'),defaults)
    actual_health={k:v for k,v in (config.get('Healthcheck') or {}).items() if v not in (0,None)}
    if not allow_collector_restoration and {k:v for k,v in health.items() if v not in (0,None)}!=actual_health:_deny('healthcheck')
    logging=service.get('logging')
    if logging is None or set(logging)-{'driver','options'}:_deny('explicit logging required')
    if not allow_collector_restoration and host.get('LogConfig')!={'Type':logging['driver'],'Config':logging.get('options',{})}:_deny('logging')
    restart=service.get('restart','no');retry=0
    if ':' in restart:restart,count=restart.split(':',1);retry=int(count)
    if host.get('RestartPolicy',{'Name':'no','MaximumRetryCount':0})!={'Name':restart,'MaximumRetryCount':retry}:_deny('restart')
    expected_mounts=[]
    for mount in service.get('volumes',[]):
        if not isinstance(mount,dict) or mount.get('type')!='bind' or set(mount)-{'type','source','target','read_only','bind'}:_deny('mount type/options')
        options=mount.get('bind') or {}
        if set(options)-{'create_host_path','propagation'}:_deny('bind options')
        expected_mounts.append((mount['source'],mount['target'],not mount.get('read_only',False),options.get('propagation','rprivate')))
    actual_mounts=[]
    for mount in row.get('Mounts',[]):
        if mount['Type']!='bind' or mount.get('Mode','rw') not in ('rw','ro'):_deny('actual mount type/options')
        actual_mounts.append((mount['Source'],mount['Destination'],mount['RW'],mount.get('Propagation','rprivate')))
    if sorted(expected_mounts)!=sorted(actual_mounts):_deny('mounts')
    requested=service.get('networks') or {}
    if not isinstance(requested,dict):_deny('networks')
    resolved=[]
    for key,options in requested.items():
        if options not in (None,{}):_deny('explicit network options require review')
        name=networks.get(key,{}).get('name')
        if not name:_deny('network name')
        resolved.append(name)
    if set(resolved)!=set(row.get('NetworkSettings',{}).get('Networks',{})) or (resolved and host.get('NetworkMode') not in resolved):_deny('network attachment')
    allowed_aliases={row.get('Name','').lstrip('/'),row['Id'][:12],config.get('Labels',{}).get('com.docker.compose.service')}
    for endpoint in row.get('NetworkSettings',{}).get('Networks',{}).values():
        if endpoint.get('IPAMConfig') or endpoint.get('Links') or endpoint.get('DriverOpts') or set(endpoint.get('Aliases') or [])-allowed_aliases:_deny('network endpoint customization')
    bindings={};exposed=set((defaults.get('ExposedPorts') or {}).keys())
    for value in service.get('expose',[]):exposed.add(str(value) if '/' in str(value) else str(value)+'/tcp')
    for port in service.get('ports',[]):
        if not isinstance(port,dict) or set(port)-{'mode','target','published','protocol','host_ip'} or port.get('mode','ingress')!='ingress':_deny('ports')
        key=str(port['target'])+'/'+port.get('protocol','tcp');exposed.add(key)
        bindings.setdefault(key,[]).append({'HostIp':port.get('host_ip',''),'HostPort':str(port.get('published',''))})
    if (host.get('PortBindings') or {})!=bindings or set((config.get('ExposedPorts') or {}).keys())!=exposed:_deny('ports/exposed')
    labels={k:v for k,v in (config.get('Labels') or {}).items() if not k.startswith('com.docker.compose.')}
    expected_labels=dict(defaults.get('Labels') or {});expected_labels.update(service.get('labels') or {})
    if labels!=expected_labels:_deny('labels')
    resources=_resources(service)
    for key,value in resources.items():
        if (host.get(key) or 0)!=value:_deny('HostConfig.'+key)
    handled={'Binds','Mounts','NetworkMode','PortBindings','RestartPolicy','LogConfig'}|set(resources)
    for key,value in host.items():
        if key in handled:continue
        if key in _DEFAULTS:
            if key in ('MaskedPaths','ReadonlyPaths'):
                if not isinstance(value,list) or sorted(value)!=sorted(_DEFAULTS[key]):_deny('HostConfig.'+key)
            elif value!=_DEFAULTS[key]:_deny('HostConfig.'+key)
        elif key in _EMPTY_HOST:
            if not any(type(value) is type(default) and value==default for default in _EMPTY_HOST[key]):_deny('HostConfig.'+key)
        else:_deny('uncovered HostConfig field')
    for key in ('Domainname','StopSignal','StopTimeout','Shell','Volumes','OnBuild'):
        if (config.get(key) or None)!=(defaults.get(key) or None):_deny('Config.'+key)
    for key in ('Tty','OpenStdin','StdinOnce'):
        if config.get(key,False):_deny('Config.'+key)
    # Bind the full actual fields in the evidence; only the image reference string
    # is ignored after independently resolving it to the identical image ID.
    return digest({'service':service,'image_id':image['Id'],'actual_config':config,'actual_host':host,'mounts':row.get('Mounts',[]),'networks':sorted(resolved)})


def _effective_image_defaults(service,image):
    config=image.get('Config') or {}
    known={'Hostname','Domainname','User','AttachStdin','AttachStdout','AttachStderr','ExposedPorts','Tty','OpenStdin','StdinOnce','Env','Cmd','Healthcheck','ArgsEscaped','Image','Volumes','WorkingDir','Entrypoint','OnBuild','Labels','StopSignal','StopTimeout','Shell','MacAddress'}
    if set(config)-known:_deny('uncovered image Config field')
    result={key:config.get(key) for key in known-{'Image','Env','Healthcheck','Labels','Cmd','Entrypoint','User','WorkingDir','ExposedPorts'}}
    for key in ('Hostname','Domainname','MacAddress'):result[key]=result.get(key) or ''
    for key in ('Tty','OpenStdin','StdinOnce','AttachStdin','AttachStdout','AttachStderr','ArgsEscaped'):result[key]=result.get(key) or False
    result['Env']=_env(config.get('Env'));result['Env'].update(_env(service.get('environment',{})))
    for source,target,empty in [('command','Cmd',None),('entrypoint','Entrypoint',None),('working_dir','WorkingDir',''),('user','User','')]:
        value=service[source] if service.get(source) is not None else config.get(target,empty)
        if source in ('command','entrypoint') and value is not None and not isinstance(value,list):_deny('image transition '+source)
        result[target]=value or empty
    if service.get('entrypoint') is not None and service.get('command') is None:result['Cmd']=None
    result['Healthcheck']={k:v for k,v in _health(service.get('healthcheck'),config).items() if v not in (0,None)}
    result['Labels']=dict(config.get('Labels') or {});result['Labels'].update(service.get('labels') or {})
    exposed=set((config.get('ExposedPorts') or {}).keys())
    for value in service.get('expose',[]):exposed.add(str(value) if '/' in str(value) else str(value)+'/tcp')
    for port in service.get('ports',[]):exposed.add(str(port['target'])+'/'+port.get('protocol','tcp'))
    result['ExposedPorts']=sorted(exposed)
    return result


def verify_image_transition(service,old_image,new_image):
    """Prove image replacement cannot silently change effective startup defaults."""
    for key in ('Architecture','Os','Variant'):
        if old_image.get(key)!=new_image.get(key):_deny('image platform')
    old=_effective_image_defaults(service,old_image);new=_effective_image_defaults(service,new_image)
    if old!=new:
        # Report categories only; Env/default values can contain credentials.
        changed=','.join(sorted(key for key in old if old[key]!=new[key]))
        _deny('new image effective defaults '+changed)
    return digest(new)
