import json, os, socket, subprocess, threading, pathlib, signal, time, math, audioop
from collections import deque
from sendspin_source import SendspinSourceBridge
from aes67_rx import SapDiscovery, Aes67Receiver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from nexus_diag import setup_logging, system_metrics, process_metrics, clock_metrics, build_bundle
OPTIONS='/data/options.json'; USER_CONFIG='/data/gateway_config.json'; RUNTIME=pathlib.Path('/data/runtime'); RUNTIME.mkdir(parents=True,exist_ok=True); VERSION='1.0.0-rc4'; LOG=setup_logging()

def options():
    # UI-managed config overrides Supervisor options after first save.
    for path in (USER_CONFIG, OPTIONS):
        try:
            with open(path) as f:return json.load(f)
        except Exception:pass
    return {'music_assistant':{},'dante':{},'sources':[]}
def save_options(cfg):
    tmp=USER_CONFIG+'.tmp'
    with open(tmp,'w') as f: json.dump(cfg,f,indent=2)
    os.replace(tmp,USER_CONFIG)
def interfaces():
    out=[]
    for _,name in socket.if_nameindex():
        try:
            data=json.loads(subprocess.check_output(['ip','-j','addr','show','dev',name],text=True))[0]
            out.append({'name':name,'ipv4':[a['local'] for a in data.get('addr_info',[]) if a.get('family')=='inet'],'up':data.get('operstate')=='UP'})
        except Exception as e:out.append({'name':name,'ipv4':[],'up':False,'error':str(e)})
    return out
def iface_ipv4(name):
    for i in interfaces():
        if i['name']==name and i['ipv4']:return i['ipv4'][0]
    return None

class Worker:
    def __init__(self,cfg,index=0):
        self.cfg=cfg;self.index=index;self.proc=None;self.state='stopped';self.error=None;self.sendspin=None;self.started_at=None;self.restarts=0;self.last_restart_at=None
        self.logs=deque(maxlen=250);self._log_thread=None;self._watch_thread=None;self._stop=threading.Event();self.aes67=None
        self.dir=RUNTIME/cfg['id'];self.dir.mkdir(parents=True,exist_ok=True);self.fifo=self.dir/'audio.pcm'
    def status(self):
        alive=bool(self.proc and self.proc.poll() is None)
        ss=self.sendspin.status() if self.sendspin else None
        return {'id':self.cfg['id'],'name':self.cfg['name'],'type':self.cfg['type'],'dante_mode':self.cfg.get('dante_mode') if self.cfg['type']=='dante_rx' else None,
          'interface':self.cfg.get('interface'),'interface_ip':iface_ipv4(self.cfg.get('interface','')),'channels':self.cfg.get('channels',2),'sample_rate':self.cfg.get('sample_rate',48000),
          'state':self.state,'process_alive':alive,'pid':self.proc.pid if alive else None,'started_at':self.started_at,'uptime_s':round(time.time()-self.started_at,1) if self.started_at else None,
          'restarts':self.restarts,'last_restart_at':self.last_restart_at,'fifo':str(self.fifo),'error':self.error,'audio':self._audio_diag(ss),
          'process_metrics':process_metrics(self.proc.pid if alive else None),'dante':self._dante_diag() if self.cfg['type']=='dante_rx' else None,'aes67':self.aes67.status() if self.aes67 else None,'sendspin':ss,'log_tail':list(self.logs)[-20:]}
    def _audio_diag(self,ss):
        if not ss:return {'present':False,'last_audio_age_s':None,'bytes_read':0,'peak_dbfs':None}
        age=(time.time()-ss['last_pcm_at']) if ss.get('last_pcm_at') else None
        return {'present':age is not None and age<2.0,'last_audio_age_s':round(age,2) if age is not None else None,'bytes_read':ss.get('bytes_read',0),'peak_dbfs':ss.get('peak_dbfs')}
    def _dante_diag(self):
        d=options().get('dante',{}); idx=self.index; clock=pathlib.Path(d.get('clock_path','/share/usrvclock'))
        return {'backend':'inferno2pipe' if self.cfg.get('dante_mode','native_dante')=='native_dante' else 'aes67','clock_path':str(clock),'clock_available':clock.exists(),
          'process_id':int(self.cfg.get('process_id',idx+1)),'alt_port':int(self.cfg.get('alt_port',14000+idx*10)),'rx_latency_ms':float(self.cfg.get('rx_latency_ms',10)),
          'device_name':self.cfg['name'],'clock_health':clock_metrics(clock),'routing':'Patch transmitter channels to this RX device in Dante Controller'}
    def _prepare_fifo(self):
        if self.fifo.exists():self.fifo.unlink()
        os.mkfifo(self.fifo)
    def _start_sendspin(self,channels=2,sample_rate=48000,bit_depth=16,input_sample_rate=None):
        ma=options().get('music_assistant',{});host=ma.get('host','');port=ma.get('port',8927)
        if not host:self.error='Music Assistant host is empty; receiver runs but Live Input is not connected';return
        self.sendspin=SendspinSourceBridge(self.cfg['id'],self.cfg['name'],self.fifo,self.dir/'sendspin',f'ws://{host}:{port}/sendspin',channels,sample_rate,bit_depth,input_sample_rate=input_sample_rate);self.sendspin.start()
    def _drain_logs(self):
        if not self.proc or not self.proc.stdout:return
        for line in self.proc.stdout:
            self.logs.append(line.rstrip());LOG.info('[%s/%s] %s',self.cfg.get('type'),self.cfg.get('id'),line.rstrip())
            if self._stop.is_set():break
    def _launch(self,cmd,env):
        LOG.info('Launching source=%s type=%s interface=%s command=%s',self.cfg.get('id'),self.cfg.get('type'),self.cfg.get('interface'),cmd[0]);self.proc=subprocess.Popen(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
        self.started_at=time.time();self._log_thread=threading.Thread(target=self._drain_logs,daemon=True,name=f'log-{self.cfg["id"]}');self._log_thread.start()
    def start(self):
        self._stop.clear();self.error=None;LOG.info('Starting source=%s name=%s type=%s',self.cfg.get('id'),self.cfg.get('name'),self.cfg.get('type'))
        if not self.cfg.get('enabled',True):self.state='disabled';return
        if self.cfg['type']=='airplay':self._start_airplay()
        elif self.cfg['type']=='dante_rx':self._start_dante()
        elif self.cfg['type']=='spotify':self._start_spotify()
        elif self.cfg['type']=='cast':self.state='experimental_not_started';self.error='Software Google Cast receiver is not enabled in this build'
    def _start_airplay(self):
        if not iface_ipv4(self.cfg.get('interface','')):self.state='error';self.error='Selected interface has no IPv4 address';return
        self._prepare_fifo();conf=self.dir/'shairport-sync.conf';pw=self.cfg.get('airplay_password')
        password=f' password = "{pw}";' if pw else ''
        conf.write_text(f'''general = {{ name = "{self.cfg['name']}"; interface = "{self.cfg['interface']}"; output_backend = "pipe";{password} }};\npipe = {{ name = "{self.fifo}"; output_rate = 48000; output_format = "S16_LE"; output_channels = 2; }};\n''')
        try:self._launch(['shairport-sync','-c',str(conf)],os.environ.copy());self.state='airplay_ready';self._start_sendspin(2,48000,16);self._start_watchdog()
        except Exception as e:self.state='error';self.error=str(e)
    def _start_spotify(self):
        # librespot discovery/Spotify Connect source. The pipe backend emits raw PCM.
        self._prepare_fifo()
        cache=self.dir/'librespot-cache'; cache.mkdir(parents=True,exist_ok=True)
        try: cache.chmod(0o700)
        except Exception: pass
        bitrate=int(self.cfg.get('spotify_bitrate',320))
        if bitrate not in (96,160,320):
            self.state='error';self.error='Spotify bitrate must be 96, 160 or 320 kb/s';return
        spotify_ip=iface_ipv4(self.cfg.get('interface',''))
        if not spotify_ip:
            self.state='error';self.error='Selected Spotify interface has no IPv4 address';return
        cmd=['librespot','--name',self.cfg['name'],'--zeroconf-interface',spotify_ip,
             '--device-type',self.cfg.get('spotify_device_type','speaker'),
             '--bitrate',str(bitrate),'--backend','pipe','--device',str(self.fifo),'--format','S16',
             '--cache',str(cache),'--system-cache',str(cache),'--zeroconf-backend','libmdns']
        if self.cfg.get('spotify_normalisation',False):cmd.append('--enable-volume-normalisation')
        if self.cfg.get('spotify_autoplay',True):cmd.append('--autoplay')
        try:
            self._launch(cmd,os.environ.copy());self.state='spotify_connect_ready'
            self._start_sendspin(2,48000,16,input_sample_rate=44100);self._start_watchdog()
        except Exception as e:self.state='error';self.error=f'librespot start failed: {e}'
    def _start_dante(self):
        if self.cfg.get('dante_mode','native_dante')=='aes67':return self._start_aes67()
        iface=self.cfg.get('interface','');ip=iface_ipv4(iface)
        if not ip:self.state='error';self.error='Selected Dante interface has no IPv4 address';return
        channels=int(self.cfg.get('channels',2));sr=int(self.cfg.get('sample_rate',48000));d=options().get('dante',{});clock=d.get('clock_path','/share/usrvclock')
        if sr!=48000:self.state='error';self.error='Music Assistant Dante RX profile currently requires 48 kHz';return
        if d.get('require_clock',True) and not pathlib.Path(clock).exists():self.state='waiting_for_ptp';self.error=f'PTP clock socket not found: {clock}. Start/configure Statime first.';return
        self._prepare_fifo();env=os.environ.copy();env.update({'INFERNO_BIND_IP':iface,'INFERNO_SAMPLE_RATE':str(sr),'INFERNO_RX_CHANNELS':str(channels),'INFERNO_TX_CHANNELS':'0',
          'INFERNO_NAME':self.cfg['name'],'INFERNO_PROCESS_ID':str(int(self.cfg.get('process_id',self.index+1))),'INFERNO_ALT_PORT':str(int(self.cfg.get('alt_port',14000+self.index*10))),
          'INFERNO_RX_LATENCY_NS':str(int(float(self.cfg.get('rx_latency_ms',10))*1_000_000)),'INFERNO_CLOCK_PATH':clock,'RUST_LOG':d.get('log_level','info')})
        try:self._launch(['inferno2pipe','--channels-count',str(channels),'--output',str(self.fifo)],env);self.state='dante_native_ready';self._start_sendspin(channels,sr,32);self._start_watchdog()
        except Exception as e:self.state='error';self.error=f'Inferno RX start failed: {e}'
    def _start_aes67(self):
        iface=self.cfg.get('interface','');ip=iface_ipv4(iface)
        if not ip:self.state='error';self.error='Selected AES67 interface has no IPv4 address';return
        self._prepare_fifo();self.aes67=Aes67Receiver(self.cfg,self.fifo,ip);self.aes67.start();self.state=self.aes67.state;self.error=self.aes67.error
        if self.state=='aes67_ready':self._start_sendspin(int(self.cfg.get('channels',2)),48000,16);self._start_watchdog()
    def _start_watchdog(self):
        if not self._watch_thread or not self._watch_thread.is_alive():self._watch_thread=threading.Thread(target=self._watchdog,daemon=True,name=f'watch-{self.cfg["id"]}');self._watch_thread.start()
    def _watchdog(self):
        while not self._stop.wait(2):
            dead=False
            if self.proc and self.proc.poll() is not None:dead=True;self.error=f'worker exited with status {self.proc.returncode}'
            if self.aes67 and self.aes67.proc and self.aes67.proc.poll() is not None:dead=True;self.error=f'AES67 ffmpeg exited with status {self.aes67.proc.returncode}'
            if dead:
                self.state='error'
                if self.cfg.get('auto_restart',True) and not self._stop.is_set():
                    self.restarts+=1;self.last_restart_at=time.time();M.request_restart(self.cfg['id'],delay=2);return
                return
    def stop(self):
        self._stop.set()
        if self.sendspin:self.sendspin.stop();self.sendspin=None
        if self.aes67:self.aes67.stop();self.aes67=None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:self.proc.kill()
        self.state='stopped';LOG.info('Stopped source=%s',self.cfg.get('id'))

class Manager:
    def __init__(self):self.workers={};self.lock=threading.Lock();self.validation=[];self.sap={}
    def _validate(self,cfgs):
        errs=[];ids=set();pids=set();ports=[]
        for i,c in enumerate(cfgs):
            if not c.get('id') or not c.get('name'):errs.append(f'source #{i+1}: id and name are required');continue
            if c.get('id') in ids:errs.append(f'duplicate source id: {c.get("id")}')
            ids.add(c.get('id'))
            if c.get('enabled',True) and c.get('type') in ('airplay','dante_rx') and not iface_ipv4(c.get('interface','')):errs.append(f'{c.get("id")}: interface {c.get("interface")} has no IPv4 address')
            if c.get('type')=='spotify' and int(c.get('spotify_bitrate',320)) not in (96,160,320):errs.append(f'{c.get("id")}: spotify_bitrate must be 96, 160 or 320')
            if c.get('type')=='dante_rx' and c.get('dante_mode','native_dante')=='native_dante':
                pid=int(c.get('process_id',i+1));port=int(c.get('alt_port',14000+i*10))
                if pid in pids:errs.append(f'duplicate Dante process_id: {pid}')
                if any(abs(port-p)<10 for p in ports):errs.append(f'Dante alt_port {port} must be >=10 away from other native RX ports')
                pids.add(pid);ports.append(port)
        return errs
    def reload(self):
        with self.lock:
            for w in self.workers.values():w.stop()
            for s in self.sap.values():s.stop()
            self.workers={};self.sap={};cfgs=options().get('sources',[]);self.validation=self._validate(cfgs)
            if self.validation:
                LOG.error('Configuration validation failed: %s',self.validation);return
            for i,cfg in enumerate(cfgs):
                w=Worker(cfg,i);self.workers[cfg['id']]=w;w.start()
                if cfg.get('type')=='dante_rx' and cfg.get('dante_mode')=='aes67':
                    iface=cfg.get('interface');ip=iface_ipv4(iface)
                    if ip and iface not in self.sap:self.sap[iface]=SapDiscovery(iface,ip);self.sap[iface].start()
    def statuses(self):return [w.status() for w in self.workers.values()]
    def sap_status(self):return [s.status() for s in self.sap.values()]
    def request_restart(self,source_id,delay=0):
        def go():
            if delay: time.sleep(delay)
            self.restart_source(source_id)
        threading.Thread(target=go,daemon=True,name=f'restart-{source_id}').start()
    def restart_source(self,source_id):
        with self.lock:
            w=self.workers.get(source_id)
            if not w:return False
            cfg=w.cfg;idx=w.index;old_restarts=w.restarts;w.stop();nw=Worker(cfg,idx);nw.restarts=old_restarts;self.workers[source_id]=nw;nw.start();return True
    def stop_source(self,source_id):
        with self.lock:
            w=self.workers.get(source_id)
            if not w:return False
            w.stop();return True
    def start_source(self,source_id):
        with self.lock:
            w=self.workers.get(source_id)
            if not w:return False
            if w.state not in ('stopped','error','waiting_for_ptp'):return True
            w.start();return True
    def save_source(self,cfg):
        allcfg=options();srcs=allcfg.setdefault('sources',[]);sid=cfg.get('id')
        if not sid:return ['id is required']
        found=False
        for i,s in enumerate(srcs):
            if s.get('id')==sid:srcs[i]=cfg;found=True;break
        if not found:srcs.append(cfg)
        errs=self._validate(srcs)
        if errs:return errs
        save_options(allcfg);self.reload();return []
    def delete_source(self,sid):
        allcfg=options();srcs=allcfg.get('sources',[]);new=[s for s in srcs if s.get('id')!=sid]
        if len(new)==len(srcs):return False
        allcfg['sources']=new;save_options(allcfg);self.reload();return True
    def select_aes67(self,sid,session):
        allcfg=options();src=next((x for x in allcfg.get('sources',[]) if x.get('id')==sid),None)
        if not src:return False
        for k,sk in [('multicast_ip','multicast_ip'),('rtp_port','port'),('payload_type','payload_type'),('sample_rate','sample_rate'),('channels','channels'),('aes67_codec','codec')]:
            if session.get(sk) is not None:src[k]=session[sk]
        save_options(allcfg);self.reload();return True
M=Manager()

HTML=r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nexus Audio</title><link rel="icon" type="image/png" href="icon.png"><style>
:root{color-scheme:dark;--bg:#071116;--panel:#0d1b22;--panel2:#102630;--line:#1c3c49;--text:#edf7fb;--muted:#8faab5;--cyan:#17d8ed;--blue:#4f6df5;--good:#43d6a2;--bad:#ff7189;--warn:#ffc857}*{box-sizing:border-box}body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;margin:0;background:radial-gradient(circle at 50% -20%,#12303a 0,#071116 38%);color:var(--text)}header{position:sticky;top:0;z-index:10;background:rgba(7,17,22,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}.head{max-width:1240px;margin:auto;padding:10px 18px;display:flex;align-items:center;gap:14px}.brand{font-size:19px;font-weight:800;letter-spacing:.4px;display:flex;align-items:center;gap:9px}.brand-logo{width:30px;height:30px;border-radius:7px;object-fit:cover}.brand b{color:var(--cyan)}.version{color:var(--muted);font-size:12px}.spacer{flex:1}button,.btn{cursor:pointer;border:1px solid #28576a;background:#12313e;color:var(--text);border-radius:10px;padding:9px 13px;font-weight:650}button:hover{border-color:var(--cyan)}button.primary{background:linear-gradient(135deg,#126d91,#344ecb);border:0}button.danger{border-color:#733544;color:#ff9cac}.lang{min-width:62px}nav{max-width:1240px;margin:auto;padding:0 18px 10px;display:flex;gap:8px;overflow:auto}.nav{background:transparent;border-color:transparent;color:var(--muted);white-space:nowrap}.nav.active{background:#102b36;color:white;border-color:#1e596b}main{max-width:1240px;margin:auto;padding:20px 18px 50px}.hero{display:grid;grid-template-columns:minmax(240px,420px) 1fr;gap:28px;align-items:center;margin:8px 0 24px}.hero img{width:100%;border-radius:18px;border:1px solid #173743;box-shadow:0 24px 70px #0008}.hero h1{font-size:clamp(30px,5vw,54px);margin:0 0 8px}.hero h1 span{color:var(--cyan)}.hero p{color:var(--muted);font-size:17px;line-height:1.55}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:14px}.card{background:linear-gradient(160deg,rgba(16,38,48,.96),rgba(10,27,34,.96));border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:0 8px 25px #0002}.card h3{margin:0 0 8px}.stat{font-size:28px;font-weight:800}.ok{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.muted{color:var(--muted)}.pill{display:inline-block;padding:4px 8px;border-radius:99px;background:#16323d;color:#a8dce8;font-size:12px;margin-right:5px}.meter{height:8px;background:#17303a;border-radius:8px;overflow:hidden;margin:10px 0}.bar{height:100%;background:linear-gradient(90deg,var(--blue),var(--cyan))}.actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#07151b;border-radius:10px;padding:10px;max-height:180px;overflow:auto}.pairbox{margin-top:10px;padding:10px;border:1px solid #28576a;border-radius:12px;background:#07151b}.pairhead{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px}.pairtoken{margin:0;max-height:none}.copyok{border-color:var(--good)!important;color:var(--good)!important}.nicselect{width:100%;margin-top:8px;background:#0b2029;color:var(--text);border:1px solid #28576a;border-radius:9px;padding:8px}.toast{position:fixed;right:18px;bottom:18px;z-index:50;background:#102630;border:1px solid #28576a;border-radius:10px;padding:10px 14px;box-shadow:0 10px 30px #0008;display:none}section{display:none}section.active{display:block}.section-title{display:flex;align-items:end;justify-content:space-between;gap:15px;margin:8px 0 15px}.section-title h2{margin:0}.help{line-height:1.65}.help h3{margin-top:25px;color:#c8f8ff}.flow{font-family:ui-monospace,monospace;background:#07151b;border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto}.source-type{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#75dceb}.empty{padding:28px;text-align:center;color:var(--muted)}@media(max-width:760px){.hero{grid-template-columns:1fr}.hero img{max-height:260px;object-fit:cover}.head{gap:8px}.brand{font-size:16px}}
</style></head><body><header><div class="head"><div class="brand"><img class="brand-logo" src="icon.png" alt=""><span>NEXUS<b>AUDIO</b></span></div><span class="version" id="v"></span><div class="spacer"></div><button class="lang" onclick="toggleLang()" id="langBtn">EN</button><button onclick="reloadGateway()" data-i="reload">Recharger</button></div><nav><button class="nav active" data-tab="overview" data-i="overview">Vue d'ensemble</button><button class="nav" data-tab="sources" data-i="sources">Sources</button><button class="nav" data-tab="aes67">AES67</button><button class="nav" data-tab="help" data-i="help">Aide</button></nav></header><main>
<section id="overview" class="active"><div class="hero"><img src="logo.jpg" alt="Nexus Audio by Mct."><div><span class="pill">HOME ASSISTANT</span><span class="pill">MUSIC ASSISTANT</span><h1>Nexus <span>Audio</span></h1><p data-i="tagline">La passerelle qui réunit AirPlay, Spotify Connect, Dante et AES67 dans Music Assistant.</p><div id="health"></div></div></div><div id="overviewGrid" class="grid"></div></section>
<section id="sources"><div class="section-title"><div><h2 data-i="sourcesTitle">Sources audio</h2><div class="muted" data-i="sourcesSub">Entrées actives et état temps réel.</div></div></div><div id="sourceGrid" class="grid"></div></section>
<section id="aes67"><div class="section-title"><div><h2>AES67 / SAP</h2><div class="muted" data-i="aesSub">Sessions réseau découvertes sur les interfaces configurées.</div></div></div><div id="sap" class="grid"></div></section>
<section id="help"><div class="card help" id="helpText"></div></section>
</main><div id="toast" class="toast"></div><script>
const T={fr:{reload:'Recharger',overview:"Vue d'ensemble",sources:'Sources',help:'Aide',tagline:'La passerelle qui réunit AirPlay, Spotify Connect, Dante et AES67 dans Music Assistant.',sourcesTitle:'Sources audio',sourcesSub:'Entrées actives et état temps réel.',aesSub:'Sessions réseau découvertes sur les interfaces configurées.',healthy:'Nexus Audio opérationnel',invalid:'Configuration invalide',audio:'Audio',present:'présent',none:'aucun',paired:'appairé',start:'Démarrer',stop:'Arrêter',restart:'Redémarrer',noSources:'Aucune source configurée.',noSap:'Aucune annonce SAP reçue.',interfaces:'Interfaces réseau',up:'actives',ma:'Music Assistant',sendspin:'Sendspin',ptp:'PTP / Statime',clock:'horloge disponible',clockMissing:'horloge absente',inputs:'Entrées',copy:'Copier',copied:'Copié !',pairingToken:'Token d’appairage',networkCard:'Carte réseau',nicSaved:'Carte réseau enregistrée',helpHtml:`<h2>Nexus Audio — Aide</h2><p><b>Nexus Audio by Mct.</b> transforme des sources audio externes en Live Inputs Sendspin pour Music Assistant. Music Assistant continue de gérer les zones, groupes, volumes et sorties.</p><div class="flow">AirPlay / Spotify Connect / Dante / AES67<br>↓<br><b>Nexus Audio</b><br>↓ PCM / Sendspin source@v1<br><b>Music Assistant</b><br>↓<br>Lecteurs MA / Spin2Dante / Dante</div><h3>AirPlay</h3><p>Shairport Sync reçoit l'audio Apple et l'envoie à Music Assistant en PCM 48 kHz / 16 bits stéréo. Pour AirPlay 2, évitez les conflits PTP/NQPTP avec la carte réseau Dante.</p><h3>Spotify Connect</h3><p>librespot fait apparaître Nexus Audio comme appareil Spotify Connect. Le téléphone ou la tablette devient une télécommande ; l'audio reste traité par le serveur. Aucun mot de passe Spotify n'est enregistré dans la configuration Nexus Audio.</p><h3>Dante RX</h3><p>Inferno reçoit le Dante natif. Choisissez la carte réseau Dante, utilisez un process_id unique et espacez les alt_port d'au moins 10. Statime doit fournir l'horloge PTP via /share/usrvclock. Le patch des canaux se fait dans Dante Controller.</p><h3>AES67</h3><p>Nexus Audio découvre les annonces SAP/SDP puis reçoit le RTP multicast L16/L24. AES67 reste séparé du moteur Dante natif.</p><h3>Sendspin / Music Assistant</h3><p>Chaque source possède une identité Sendspin indépendante. Si Music Assistant demande un appairage, le token apparaît dans la carte de la source.</p><h3>Surveillance</h3><p>Les cartes indiquent présence audio, crête dBFS, interface/IP, processus, Sendspin et erreurs. Start, Stop et Restart agissent réellement sur le worker correspondant. Le watchdog peut relancer automatiquement un worker défaillant.</p>`},en:{reload:'Reload',overview:'Overview',sources:'Sources',help:'Help',tagline:'The gateway that brings AirPlay, Spotify Connect, Dante and AES67 into Music Assistant.',sourcesTitle:'Audio sources',sourcesSub:'Active inputs and live status.',aesSub:'Network sessions discovered on configured interfaces.',healthy:'Nexus Audio operational',invalid:'Invalid configuration',audio:'Audio',present:'present',none:'none',paired:'paired',start:'Start',stop:'Stop',restart:'Restart',noSources:'No sources configured.',noSap:'No SAP announcement received.',interfaces:'Network interfaces',up:'up',ma:'Music Assistant',sendspin:'Sendspin',ptp:'PTP / Statime',clock:'clock available',clockMissing:'clock missing',inputs:'Inputs',copy:'Copy',copied:'Copied!',pairingToken:'Pairing token',networkCard:'Network interface',nicSaved:'Network interface saved',helpHtml:`<h2>Nexus Audio — Help</h2><p><b>Nexus Audio by Mct.</b> turns external audio sources into Sendspin Live Inputs for Music Assistant. Music Assistant remains responsible for zones, groups, volume and outputs.</p><div class="flow">AirPlay / Spotify Connect / Dante / AES67<br>↓<br><b>Nexus Audio</b><br>↓ PCM / Sendspin source@v1<br><b>Music Assistant</b><br>↓<br>MA players / Spin2Dante / Dante</div><h3>AirPlay</h3><p>Shairport Sync receives Apple audio and forwards 48 kHz / 16-bit stereo PCM to Music Assistant. For AirPlay 2, keep NQPTP/PTP conflicts away from the Dante interface.</p><h3>Spotify Connect</h3><p>librespot exposes Nexus Audio as a Spotify Connect device. The phone or tablet acts as a remote while audio stays on the server. Nexus Audio does not store a Spotify password in its gateway configuration.</p><h3>Dante RX</h3><p>Inferno receives native Dante. Select the Dante NIC, use a unique process_id and keep alt_port values at least 10 apart. Statime must provide the PTP clock through /share/usrvclock. Channel routing is performed in Dante Controller.</p><h3>AES67</h3><p>Nexus Audio discovers SAP/SDP announcements and receives L16/L24 multicast RTP. AES67 is kept separate from the native Dante engine.</p><h3>Sendspin / Music Assistant</h3><p>Each source has its own Sendspin identity. When Music Assistant requires pairing, the source card displays the pairing token.</p><h3>Monitoring</h3><p>Source cards report audio presence, peak dBFS, interface/IP, process, Sendspin and errors. Start, Stop and Restart control the actual worker. The watchdog can automatically restart a failed worker.</p>`}};
let lang=localStorage.getItem('nexus-lang')||'fr';const tr=k=>T[lang][k]||k;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function applyLang(){document.documentElement.lang=lang;document.querySelectorAll('[data-i]').forEach(e=>e.textContent=tr(e.dataset.i));langBtn.textContent=lang==='fr'?'EN':'FR';helpText.innerHTML=tr('helpHtml');load()};function toggleLang(){lang=lang==='fr'?'en':'fr';localStorage.setItem('nexus-lang',lang);applyLang()}
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.querySelectorAll('section').forEach(x=>x.classList.remove('active'));document.getElementById(b.dataset.tab).classList.add('active')});
async function j(u,o){let r=await fetch(u,o);return await r.json()}async function reloadGateway(){await j('api/reload',{method:'POST'});load()}async function act(a,id){await j('api/source/'+a,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});load()}
async function copyToken(btn,token){
  try{
    if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(token)}
    else{let t=document.createElement('textarea');t.value=token;t.style.position='fixed';t.style.opacity='0';document.body.appendChild(t);t.focus();t.select();document.execCommand('copy');t.remove()}
    const old=btn.textContent;btn.textContent=tr('copied');btn.classList.add('copyok');toast.textContent=tr('copied');toast.style.display='block';
    setTimeout(()=>{btn.textContent=old;btn.classList.remove('copyok');toast.style.display='none'},1600)
  }catch(e){toast.textContent='Copy error';toast.style.display='block';setTimeout(()=>toast.style.display='none',1800)}
}
let nexusInterfaces=[];
function nicOptions(current){return '<option value="">—</option>'+nexusInterfaces.map(i=>{const ips=(i.ipv4||[]).join(', ');return `<option value="${esc(i.name)}" ${i.name===current?'selected':''}>${esc(i.name)}${ips?' — '+esc(ips):''}</option>`}).join('')}
async function setNic(id,sel){const r=await j('api/source/interface',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,interface:sel.value})});toast.textContent=r.error||tr('nicSaved');toast.style.display='block';setTimeout(()=>toast.style.display='none',1800);await load()}
function sourceCard(x){let au=x.audio||{},pct=au.peak_dbfs==null?0:Math.max(0,Math.min(100,100+au.peak_dbfs));let type=x.type==='dante_rx'?(x.dante_mode==='aes67'?'AES67':'Dante RX'):x.type==='spotify'?'Spotify Connect':x.type==='airplay'?'AirPlay':x.type;return `<div class="card"><div class="source-type">${esc(type)}</div><h3>${esc(x.name)}</h3><p class="${x.error?'bad':'ok'}">● ${esc(x.state)}</p><div class="meter"><div class="bar" style="width:${pct}%"></div></div><p>${tr('audio')}: <b>${au.present?tr('present'):'—'}</b> · peak ${au.peak_dbfs??'—'} dBFS</p><p class="muted">NIC ${esc(x.interface||'—')} · ${esc(x.interface_ip||'—')}</p><label class="muted">${tr('networkCard')}</label><select class="nicselect" onchange="setNic('${esc(x.id)}',this)">${nicOptions(x.interface)}</select><p>${tr('sendspin')}: ${esc(x.sendspin?.state||'—')} ${x.sendspin?.paired?'· '+tr('paired'):''}</p>${x.dante?`<p class="muted">PTP: ${x.dante.clock_available?'✓ '+tr('clock'):'⚠ '+tr('clockMissing')}</p>`:''}${x.sendspin?.pairing_token?`<div class="pairbox"><div class="pairhead"><b>${tr('pairingToken')}</b><button onclick='copyToken(this,${JSON.stringify(x.sendspin.pairing_token)})'>${tr('copy')}</button></div><pre class="pairtoken">${esc(x.sendspin.pairing_token)}</pre></div>`:''}${x.error?'<pre>'+esc(x.error)+'</pre>':''}<div class="actions"><button onclick="act('start','${esc(x.id)}')">${tr('start')}</button><button onclick="act('stop','${esc(x.id)}')">${tr('stop')}</button><button class="primary" onclick="act('restart','${esc(x.id)}')">${tr('restart')}</button></div></div>`}
async function load(){try{let [h,s,a,n]=await Promise.all([j('health'),j('api/sources'),j('api/aes67/discovery'),j('api/interfaces')]);nexusInterfaces=n.interfaces||[];v.textContent=h.version;health.innerHTML=h.validation_errors.length?`<div class="card"><b class="bad">${tr('invalid')}</b><pre>${esc(h.validation_errors.join('\n'))}</pre></div>`:`<div class="ok">● ${tr('healthy')}</div>`;let src=s.sources||[],paired=src.filter(x=>x.sendspin?.paired).length,clock=src.some(x=>x.dante?.clock_available);overviewGrid.innerHTML=`<div class="card"><div class="muted">${tr('inputs')}</div><div class="stat">${src.length}</div><div>AirPlay · Spotify · Dante · AES67</div></div><div class="card"><div class="muted">${tr('ma')} / ${tr('sendspin')}</div><div class="stat">${paired}/${src.length}</div><div>${tr('paired')}</div></div><div class="card"><div class="muted">${tr('interfaces')}</div><div class="stat">${(n.interfaces||[]).filter(i=>i.up).length}</div><div>${tr('up')}</div></div><div class="card"><div class="muted">${tr('ptp')}</div><div class="stat ${clock?'ok':'warn'}">${clock?'✓':'—'}</div><div>${clock?tr('clock'):tr('clockMissing')}</div></div>`;sourceGrid.innerHTML=src.map(sourceCard).join('')||`<div class="card empty">${tr('noSources')}</div>`;sap.innerHTML=(a.discovery||[]).flatMap(i=>(i.sessions||[]).map(q=>`<div class="card"><div class="source-type">AES67 · ${esc(i.interface)}</div><h3>${esc(q.name||'AES67')}</h3><p>${esc(q.multicast_ip)}:${esc(q.port)}</p><p>${esc(q.codec)} · ${esc(q.sample_rate)} Hz · ${esc(q.channels)} ch</p><div class="muted">${esc(q.age_s)} s</div></div>`)).join('')||`<div class="card empty">${tr('noSap')}</div>`}catch(e){health.innerHTML=`<div class="card bad">${esc(e)}</div>`}}
applyLang();setInterval(load,3000)</script></body></html>'''

class H(BaseHTTPRequestHandler):
    def bodyj(self):
        try:return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
        except Exception:return {}
    def sendj(self,obj,status=200):
        body=json.dumps(obj,indent=2).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        p=urlparse(self.path).path
        if p=='/logo.jpg':
            fp=pathlib.Path('/app/nexus-audio.jpg');b=fp.read_bytes();self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Cache-Control','public, max-age=86400');self.send_header('Content-Length',str(len(b)));self.end_headers();return self.wfile.write(b)
        if p=='/icon.png':
            fp=pathlib.Path('/app/icon.png');b=fp.read_bytes();self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Cache-Control','public, max-age=86400');self.send_header('Content-Length',str(len(b)));self.end_headers();return self.wfile.write(b)
        if p in ('/','/index.html'):
            b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();return self.wfile.write(b)
        if p=='/health':return self.sendj({'status':'ok' if not M.validation else 'config_error','version':VERSION,'validation_errors':M.validation,'system':system_metrics()})
        if p=='/api/interfaces':return self.sendj({'interfaces':interfaces()})
        if p=='/api/sources':return self.sendj({'sources':M.statuses(),'validation_errors':M.validation})
        if p=='/api/dante':return self.sendj({'native_backend':'Inferno inferno2pipe','clock':'external usrvclock from Statime/PTP','multi_rx':True,'sources':[s for s in M.statuses() if s['type']=='dante_rx'],'validation_errors':M.validation})
        if p=='/api/aes67/discovery':return self.sendj({'discovery':M.sap_status()})
        if p=='/api/diagnostics':
            fp=build_bundle(RUNTIME/'nexus-audio-diagnostics.zip',VERSION,options(),interfaces(),M.statuses(),M.sap_status());b=fp.read_bytes();self.send_response(200);self.send_header('Content-Type','application/zip');self.send_header('Content-Disposition','attachment; filename="nexus-audio-diagnostics.zip"');self.send_header('Content-Length',str(len(b)));self.end_headers();return self.wfile.write(b)
        return self.sendj({'error':'not found'},404)
    def do_POST(self):
        p=urlparse(self.path).path;data=self.bodyj()
        if p=='/api/reload':M.reload();return self.sendj({'ok':not M.validation,'validation_errors':M.validation,'sources':M.statuses()},200 if not M.validation else 400)
        if p=='/api/source/save':
            errs=M.save_source(data);return self.sendj({'ok':not errs,'errors':errs},200 if not errs else 400)
        if p=='/api/source/delete':return self.sendj({'ok':M.delete_source(data.get('id'))})
        if p=='/api/source/start':return self.sendj({'ok':M.start_source(data.get('id'))})
        if p=='/api/source/stop':return self.sendj({'ok':M.stop_source(data.get('id'))})
        if p=='/api/source/restart':return self.sendj({'ok':M.restart_source(data.get('id'))})
        if p=='/api/source/interface':
            sid=str(data.get('id',''));nic=str(data.get('interface','')).strip()
            if not nic or not iface_ipv4(nic):return self.sendj({'error':'Selected interface has no IPv4 address'},400)
            cfg=options();found=False
            for src in cfg.get('sources',[]):
                if str(src.get('id',''))==sid:src['interface']=nic;found=True;break
            if not found:return self.sendj({'error':'source not found'},404)
            save_options(cfg);M.reload();return self.sendj({'ok':True,'id':sid,'interface':nic})
        if p=='/api/aes67/select':return self.sendj({'ok':M.select_aes67(data.get('id'),data.get('session') or {})})
        return self.sendj({'error':'not found'},404)
    def log_message(self,fmt,*args):print('[api]',fmt%args,flush=True)
def shutdown(*_):
    for w in M.workers.values():w.stop()
    for s in M.sap.values():s.stop()
    raise SystemExit(0)
if __name__=='__main__':
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown);LOG.info('Nexus Audio %s starting',VERSION);print(f'Nexus Audio {VERSION}',flush=True);M.reload();ThreadingHTTPServer(('0.0.0.0',8099),H).serve_forever()
