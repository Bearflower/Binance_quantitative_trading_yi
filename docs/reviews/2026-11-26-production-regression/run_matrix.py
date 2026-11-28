"""按测试文件隔离执行并保存完整清单、日志和JUnit。"""
import argparse,concurrent.futures,hashlib,json,os,subprocess,sys,time,xml.etree.ElementTree as ET
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--manifest',required=True);p.add_argument('--out',required=True);p.add_argument('--jobs',type=int,default=3);p.add_argument('--timeout',type=int,default=240);args=p.parse_args()
root=Path(args.root);out=Path(args.out);out.mkdir(parents=True,exist_ok=True);paths=json.loads(Path(args.manifest).read_text());env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parent/'localguard')+':'+str(root),PYTHONDONTWRITEBYTECODE='1',MPLBACKEND='Agg')
def run(path):
 slug=path.replace('/','__');log=out/(slug+'.log');xml=out/(slug+'.xml');start=time.monotonic();cmd=[sys.executable,'-m','pytest',path,'-q','-p','no:cacheprovider','--junitxml='+str(xml)]
 with log.open('w') as f:
  try:rc=subprocess.run(cmd,cwd=root,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=args.timeout).returncode
  except subprocess.TimeoutExpired:rc=124
 row={'path':path,'sha256':hashlib.sha256((root/path).read_bytes()).hexdigest(),'exit_code':rc,'elapsed':round(time.monotonic()-start,2),'log':str(log),'junit':str(xml)}
 if xml.exists():
  tree=ET.parse(xml).getroot();nodes=list(tree) if tree.tag=='testsuites' else [tree]
  row.update({k:sum(int(n.get(k,0)) for n in nodes) for k in ['tests','failures','errors','skipped']})
 return row
results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
 for row in pool.map(run,paths):
  results.append(row);(out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
  if row['exit_code'] or len(results)%10==0:print(json.dumps({'completed':len(results),'total':len(paths),**row},ensure_ascii=False),flush=True)
summary={'files':len(results),'failed_files':[r['path'] for r in results if r['exit_code']!=0],**{k:sum(r.get(k,0) for r in results) for k in ['tests','failures','errors','skipped']}}
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False),flush=True)
sys.exit(bool(summary['failed_files']))
