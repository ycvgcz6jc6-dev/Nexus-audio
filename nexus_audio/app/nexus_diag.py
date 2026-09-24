import json, logging, logging.handlers, os, pathlib, time, zipfile

LOG_DIR=pathlib.Path(os.environ.get('NEXUS_DATA_DIR','/data'))/'logs'; LOG_DIR.mkdir(parents=True,exist_ok=True)
LOG_FILE=LOG_DIR/'nexus-audio.log'
SECRET_KEYS=('password','token','psk','secret','credential','private_key','auth')

def setup_logging():
    root=logging.getLogger('nexus'); root.setLevel(logging.DEBUG)
    if root.handlers:return root
    fmt=logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s','%Y-%m-%dT%H:%M:%S%z')
    fh=logging.handlers.RotatingFileHandler(LOG_FILE,maxBytes=5*1024*1024,backupCount=5,encoding='utf-8');fh.setFormatter(fmt);fh.setLevel(logging.DEBUG)
    sh=logging.StreamHandler();sh.setFormatter(fmt);sh.setLevel(logging.INFO)
    root.addHandler(fh);root.addHandler(sh);return root

def redact(obj):
    if isinstance(obj,dict):
        return {k:('***REDACTED***' if any(x in k.lower() for x in SECRET_KEYS) else redact(v)) for k,v in obj.items()}
    if isinstance(obj,list):return [redact(x) for x in obj]
    return obj

def system_metrics():
    out={'loadavg':None,'memory':{},'uptime_s':None}
    try:out['loadavg']=[round(x,2) for x in os.getloadavg()]
    except OSError:pass
    try:
        vals={}
        for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
            k,v=line.split(':',1); vals[k]=int(v.strip().split()[0])*1024
        total=vals.get('MemTotal',0);avail=vals.get('MemAvailable',0)
        out['memory']={'total_bytes':total,'available_bytes':avail,'used_bytes':max(0,total-avail),'used_percent':round((total-avail)*100/total,1) if total else None}
    except Exception:pass
    try:out['uptime_s']=round(float(pathlib.Path('/proc/uptime').read_text().split()[0]),1)
    except Exception:pass
    return out

def process_metrics(pid):
    if not pid:return None
    try:
        status={}
        for line in pathlib.Path(f'/proc/{pid}/status').read_text().splitlines():
            if ':' in line:
                k,v=line.split(':',1);status[k]=v.strip()
        return {'pid':pid,'rss':status.get('VmRSS'),'threads':int(status.get('Threads','0')),'fd_count':len(list(pathlib.Path(f'/proc/{pid}/fd').iterdir()))}
    except Exception:return None

def clock_metrics(path):
    p=pathlib.Path(path); now=time.time();exists=p.exists();age=None
    try:age=max(0,now-p.stat().st_mtime) if exists else None
    except Exception:pass
    return {'path':str(p),'available':exists,'mtime_age_s':round(age,2) if age is not None else None}

def build_bundle(target,version,config,interfaces,sources,sap):
    target=pathlib.Path(target); tmp=target.parent/'diag-tmp'; tmp.mkdir(parents=True,exist_ok=True)
    payload={'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'version':version,'system':system_metrics(),'interfaces':interfaces,'sources':sources,'sap':sap,'config':redact(config)}
    (tmp/'diagnostics.json').write_text(json.dumps(redact(payload),indent=2,ensure_ascii=False))
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(tmp/'diagnostics.json','diagnostics.json')
        for f in sorted(LOG_DIR.glob('nexus-audio.log*')): z.write(f,'logs/'+f.name)
    try:(tmp/'diagnostics.json').unlink();tmp.rmdir()
    except OSError:pass
    return target
