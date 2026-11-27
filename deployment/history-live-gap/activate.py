"""Activate reviewed HRS NAV and AI protection fixes on pinned production images."""
import argparse,copy,fcntl,hashlib,http.client,json,os,socket,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PLAN=json.loads((ROOT/'plan.json').read_text())

def run(*args):
    p=subprocess.run(args,capture_output=True,text=True)
    if p.returncode:
        raise RuntimeError(f"{args[0]} command failed ({p.returncode}): {p.stderr[-250:]}")
    return p.stdout.strip()

def inspect(name):
    return json.loads(run('docker','inspect',name))[0]

def save(name,value):
    p=ROOT/name;p.write_text(json.dumps(value,indent=2));p.chmod(0o600)

class Engine(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')

def create(name,body):
    c=Engine('localhost')
    c.request('POST','/v1.44/containers/create?name='+name,json.dumps(body),{'Content-Type':'application/json'})
    r=c.getresponse();data=json.loads(r.read());c.close()
    if r.status!=201:raise RuntimeError('container creation failed: '+str(data.get('message',''))[:200])
    return data['Id']

def mounts(c):
    return sorted((m['Type'],m['Source'],m['Destination'],m['RW']) for m in c['Mounts'])

def probe(r):
    run('docker','pull',r['tag'])
    image=json.loads(run('docker','image','inspect',r['tag']))[0]
    assert image['Config']['Labels']['org.opencontainers.image.revision']==PLAN['source']
    assert r['digest_ref'] in image['RepoDigests']
    base=json.loads(run('docker','image','inspect',r['image']))[0]
    assert image['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers']
    # No production mounts/credentials, no strategy execution.
    checks={"expected":r['hashes'],"source":PLAN['source'],"deploy":PLAN['deploy_id']}
    code="import hashlib,json,pathlib; c="+repr(checks)+"; assert all(hashlib.sha256((pathlib.Path('/app')/p).read_bytes()).hexdigest()==h for p,h in c['expected'].items()); v=pathlib.Path('/app/VERSION').read_text(); assert 'GIT_SHA='+c['source'] in v; assert 'DEPLOY_ID='+c['deploy'] in v; [compile((pathlib.Path('/app')/p).read_text(),p,'exec') for p in c['expected']]; print(v.strip())"
    version=run('docker','run','--rm','--network','none','--read-only','--entrypoint','python',image['Id'],'-B','-c',code)
    assert image['Config']['Cmd']==base['Config']['Cmd']
    return image['Id'],version

def stage():
    names={r['name'] for r in PLAN['services']}
    protected={}
    for name in run('docker','ps','--format','{{.Names}}').splitlines():
        if name not in names:
            protected[name]=inspect(name)['Id']
    save('protected-pre-release.json',protected)
    for r in PLAN['services']:
        old=inspect(r['name'])
        assert old['Image']==r['image'] and old['State']['Running']
        r['next_cmd']=old['Config']['Cmd']
        save(r['name']+'-before.json',old)
        image,version=probe(r)
        body=copy.deepcopy(old['Config']);body.pop('Hostname',None);body.pop('MacAddress',None)
        host=copy.deepcopy(old['HostConfig']);host['RestartPolicy']={'Name':'no','MaximumRetryCount':0}
        body.update(Image=image,Cmd=r['next_cmd'],HostConfig=host)
        networks=old['NetworkSettings']['Networks']
        assert all(not x.get('IPAMConfig') and not x.get('DriverOpts') for x in networks.values())
        body['NetworkingConfig']={'EndpointsConfig':{n:{'Aliases':x.get('Aliases') or []} for n,x in networks.items()}}
        body['Labels']=dict(body.get('Labels') or {}, **{'codex.runtime.source':PLAN['source'], 'org.opencontainers.image.revision':PLAN['source']})
        if r['group']=='hrs':
            env=dict(x.split('=',1) for x in body['Env'])
            env.update(HRS_IMAGE_REVISION=PLAN['source'],HRS_RELEASE_SHA=PLAN['source'])
            body['Env']=[k+'='+v for k,v in env.items()]
        new_id=create(r['name']+'-runtime-ready-'+PLAN['source'][:8],body)
        new=inspect(new_id)
        assert new['Image']==image and mounts(new)==mounts(old) and new['Config']['Env']==body['Env']
        assert set(new['NetworkSettings']['Networks'])==set(networks)
        for n,x in networks.items():
            assert set(x.get('Aliases') or []) <= set(new['NetworkSettings']['Networks'][n].get('Aliases') or [])
        save(r['service']+'-ready.json',dict(id=new_id,image=image,version=version,old_id=old['Id'],cmd=r['next_cmd']))
        print(json.dumps(dict(staged=r['service'],image=image,version=version)),flush=True)
    save('plan.json',PLAN)

def healthy(name):
    deadline=time.monotonic()+100
    while time.monotonic()<deadline:
        c=inspect(name)
        if not c['State']['Running'] or c['RestartCount']:
            raise RuntimeError(name+' exited or restarted')
        status=c['State'].get('Health',{}).get('Status')
        if status=='healthy':return c
        if status=='unhealthy':raise RuntimeError(name+' is unhealthy')
        time.sleep(2)
    raise RuntimeError(name+' healthcheck timed out')

def restart_argument(policy):
    name=policy['Name'] or 'no'
    return name+(':'+str(policy['MaximumRetryCount']) if name=='on-failure' and policy['MaximumRetryCount'] else '')


def activate():
    assert PLAN['source'] and len(PLAN['services'])==4
    protected=json.loads((ROOT/'protected-pre-release.json').read_text())
    assert all(inspect(n)['Id']==cid and inspect(n)['State']['Running'] for n,cid in protected.items())
    for r in PLAN['services']:
        assert inspect(r['name'])['Id']==json.loads((ROOT/(r['name']+'-before.json')).read_text())['Id']
        ready=json.loads((ROOT/(r['service']+'-ready.json')).read_text())
        old=inspect(r['name']);new=inspect(ready['id'])
        assert old['Id']==ready['old_id'] and old['State']['Running'] and new['State']['Status']=='created'
        assert mounts(new)==mounts(old)
    activated=[]
    for r in PLAN['services']:
        ready=json.loads((ROOT/(r['service']+'-ready.json')).read_text())
        old=inspect(r['name']);new=inspect(ready['id'])
        assert old['Id']==ready['old_id'] and old['State']['Running'] and new['State']['Status']=='created'
        assert mounts(new)==mounts(old)
        retired=r['name']+'-before-runtime-'+PLAN['source'][:8]
        failed=r['name']+'-runtime-failed-'+PLAN['source'][:8]
        existing=set(run('docker','ps','-a','--format','{{.Names}}').splitlines())
        assert retired not in existing and failed not in existing
        previous_policy=copy.deepcopy(old['HostConfig']['RestartPolicy'])
        try:
            run('docker','update','--restart=no',old['Id'])
            run('docker','stop','-t','60',old['Id'])
            run('docker','rename',old['Id'],retired)
            run('docker','rename',new['Id'],r['name'])
            run('docker','start',new['Id']);current=healthy(new['Id'])
            assert current['Image']==ready['image'] and current['Config']['Cmd']==r['next_cmd']
            assert current['Config']['Env']==new['Config']['Env'] and mounts(current)==mounts(old)
            for n,x in old['NetworkSettings']['Networks'].items():
                assert set(x.get('Aliases') or []) <= set(current['NetworkSettings']['Networks'][n].get('Aliases') or [])
            live_check = "import hashlib,json,pathlib; expected="+repr(r['hashes'])+"; assert all(hashlib.sha256((pathlib.Path('/app')/p).read_bytes()).hexdigest()==h for p,h in expected.items()); print('live_source_verified')"
            run('docker','exec',new['Id'],'python','-B','-c',live_check)
            run('docker','update','--restart='+restart_argument(previous_policy),new['Id'])
            assert inspect(new['Id'])['HostConfig']['RestartPolicy']==previous_policy
            save(r['service']+'-active.json',dict(id=new['Id'],image=current['Image'],started=current['State']['StartedAt'],health='healthy',restarts=current['RestartCount']))
            activated.append((r,old['Id'],new['Id'],previous_policy))
            print(json.dumps(dict(active=r['service'],started=current['State']['StartedAt'])),flush=True)
        except BaseException:
            save(r['service']+'-activation-failed.json',dict(old_id=old['Id'],new_id=new['Id']))
            if inspect(new['Id'])['State']['Running']:
                run('docker','stop','-t','30',new['Id'])
            if inspect(new['Id'])['Name'].lstrip('/')==r['name']:
                run('docker','rename',new['Id'],failed)
            if inspect(old['Id'])['Name'].lstrip('/')!=r['name']:
                run('docker','rename',old['Id'],r['name'])
            run('docker','update','--restart='+restart_argument(previous_policy),old['Id'])
            if not inspect(old['Id'])['State']['Running']:
                run('docker','start',old['Id'])
            healthy(old['Id'])
            for prior,old_id,new_id,policy in reversed(activated):
                run('docker','update','--restart=no',new_id)
                run('docker','stop','-t','30',new_id)
                run('docker','rename',new_id,prior['name']+'-runtime-failed-'+PLAN['source'][:8])
                run('docker','rename',old_id,prior['name'])
                run('docker','update','--restart='+restart_argument(policy),old_id)
                run('docker','start',old_id);healthy(old_id)
            raise

    assert all(inspect(n)['Id']==cid and inspect(n)['State']['Running'] for n,cid in protected.items())

if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('action',choices=['stage','activate']);args=cli.parse_args()
    with open('/root/trading_system/.deploy_tmp/compose_deploy.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        (stage if args.action=='stage' else activate)()
