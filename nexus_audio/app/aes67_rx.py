import json, os, pathlib, socket, struct, subprocess, threading, time
from collections import deque

SAP_GROUP='239.255.255.255'; SAP_PORT=9875

class SapDiscovery:
    def __init__(self, interface, interface_ip):
        self.interface=interface; self.interface_ip=interface_ip; self.sessions={}; self.error=None; self._stop=threading.Event(); self._thread=None; self.session_ttl_s=30
    def start(self):
        self._stop.clear(); self._thread=threading.Thread(target=self._run,daemon=True,name=f'sap-{self.interface}'); self._thread.start()
    def stop(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=2)
    def status(self):
        now=time.time(); return {'interface':self.interface,'interface_ip':self.interface_ip,'error':self.error,'sessions':[dict(v,age_s=round(now-v['last_seen'],1)) for v in self.sessions.values()]}
    def _run(self):
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('',SAP_PORT)); s.settimeout(1)
        mreq=socket.inet_aton(SAP_GROUP)+socket.inet_aton(self.interface_ip); s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,mreq)
        while not self._stop.is_set():
            try:
                data,addr=s.recvfrom(65535); self._parse(data,addr)
            except socket.timeout:pass
            except Exception as e:self.error=str(e);time.sleep(1)
            now=time.time(); self.sessions={k:v for k,v in self.sessions.items() if now-v.get('last_seen',0)<=self.session_ttl_s}
        s.close()
    def _parse(self,data,addr):
        if len(data)<8:return
        # SAP v2: header + auth + optional payload type, followed by SDP text. Locate SDP defensively.
        p=data.find(b'v=0')
        if p<0:return
        text=data[p:].decode('utf-8','replace'); info={'origin':addr[0],'sdp':text,'last_seen':time.time()}
        for line in text.splitlines():
            line=line.strip()
            if line.startswith('s='):info['name']=line[2:]
            elif line.startswith('c=IN IP4 '):info['multicast_ip']=line.split()[-1].split('/')[0]
            elif line.startswith('m=audio '):
                x=line.split(); info['port']=int(x[1]); info['payload_type']=int(x[-1]) if x[-1].isdigit() else None
            elif line.startswith('a=rtpmap:'):
                try:
                    rhs=line.split(None,1)[1]; codec,rate,ch=(rhs.split('/')+[None,None])[:3]; info.update(codec=codec,sample_rate=int(rate),channels=int(ch or 1))
                except Exception:pass
        key=f"{info.get('name','AES67')}@{info.get('multicast_ip',addr[0])}:{info.get('port',0)}";self.sessions[key]=info

class Aes67Receiver:
    def __init__(self,cfg,fifo,interface_ip):
        self.cfg=cfg;self.fifo=str(fifo);self.interface_ip=interface_ip;self.proc=None;self.sdp_path=None;self.state='stopped';self.error=None;self.logs=deque(maxlen=80);self._stop=threading.Event();self._log_thread=None
    def start(self):
        ip=self.cfg.get('multicast_ip');port=int(self.cfg.get('rtp_port',5004));pt=int(self.cfg.get('payload_type',96));rate=int(self.cfg.get('sample_rate',48000));ch=int(self.cfg.get('channels',2));codec=self.cfg.get('aes67_codec','L24').upper()
        if not ip: self.state='waiting_stream_selection';self.error='Set multicast_ip or select a discovered SAP/SDP session';return
        enc={'L16':'pcm_s16be','L24':'pcm_s24be'}.get(codec)
        if not enc:self.state='error';self.error=f'Unsupported AES67 codec {codec}; supported L16/L24';return
        sdp=pathlib.Path(self.fifo).with_suffix('.sdp');self.sdp_path=sdp;sdp.write_text(f'''v=0\no=- 0 0 IN IP4 {self.interface_ip}\ns={self.cfg.get("name","AES67 RX")}\nc=IN IP4 {ip}\nt=0 0\nm=audio {port} RTP/AVP {pt}\na=rtpmap:{pt} {codec}/{rate}/{ch}\na=recvonly\n''')
        # ffmpeg joins the RTP multicast described by SDP and normalizes to Sendspin PCM s16le/48k.
        cmd=['ffmpeg','-hide_banner','-loglevel','warning','-protocol_whitelist','file,udp,rtp','-i',str(sdp),'-map','0:a:0','-ac',str(ch),'-ar','48000','-f','s16le','-acodec','pcm_s16le','-y',self.fifo]
        try:
            self.proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1);self.state='aes67_ready';self.error=None
            self._log_thread=threading.Thread(target=self._drain,daemon=True);self._log_thread.start()
        except Exception as e:self.state='error';self.error=str(e)
    def _drain(self):
        if not self.proc or not self.proc.stdout:return
        for l in self.proc.stdout:self.logs.append(l.rstrip())
    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:self.proc.kill()
        if self.sdp_path and self.sdp_path.exists():
            try:self.sdp_path.unlink()
            except OSError:pass
    def status(self):return {'state':self.state,'error':self.error,'pid':self.proc.pid if self.proc and self.proc.poll() is None else None,'log_tail':list(self.logs)[-20:]}
