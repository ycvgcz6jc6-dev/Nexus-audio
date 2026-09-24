import asyncio, json, pathlib, threading, time, os, errno, audioop, math

from aiosendspin.client import SendspinClient
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.source import ClientHelloSourceSupport
from aiosendspin.models.types import AudioCodec, Roles, SourceCommand
from aiosendspin.noise.keys import Identity, b64url_decode, generate_psk, psk_id_for
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_psk_token
from aiosendspin.noise.trust_store import FileClientPairingStore, PairingPsk, PskCategory

class SendspinSourceBridge:
    """Persistent Sendspin source@v1 client consuming interleaved raw PCM from a FIFO."""
    def __init__(self, source_id, name, fifo, state_dir, server_url, channels=2, sample_rate=48000, bit_depth=16, input_sample_rate=None):
        self.source_id=source_id; self.name=name; self.fifo=pathlib.Path(fifo)
        self.state_dir=pathlib.Path(state_dir); self.state_dir.mkdir(parents=True, exist_ok=True)
        self.server_url=server_url; self.channels=int(channels); self.sample_rate=int(sample_rate); self.input_sample_rate=int(input_sample_rate or sample_rate); self.bit_depth=int(bit_depth); self._rate_state=None
        self.fmt=SupportedAudioFormat(codec=AudioCodec.PCM,channels=self.channels,sample_rate=self.sample_rate,bit_depth=self.bit_depth)
        self.chunk_bytes=max(1,int(self.input_sample_rate*self.channels*(self.bit_depth//8)*.020))
        self.state='initializing'; self.error=None; self.client_id=None; self.pairing_token=None
        self.paired=False; self.streaming=False; self.connected=False; self.bytes_sent=0; self.bytes_read=0; self.last_audio_at=None; self.last_pcm_at=None; self.peak_dbfs=None
        self._thread=None; self._stop=threading.Event(); self._loop=None; self._client=None; self._capture=None; self._fd=None
    def status(self):
        return {'state':self.state,'connected':self.connected,'paired':self.paired,'streaming':self.streaming,'client_id':self.client_id,
                'pairing_token':self.pairing_token,'bytes_read':self.bytes_read,'bytes_sent':self.bytes_sent,'last_audio_at':self.last_audio_at,'last_pcm_at':self.last_pcm_at,'peak_dbfs':self.peak_dbfs,
                'error':self.error,'server_url':self.server_url,'input_format':{'sample_rate':self.input_sample_rate,'bit_depth':self.bit_depth,'channels':self.channels},'format':{'sample_rate':self.sample_rate,'bit_depth':self.bit_depth,'channels':self.channels},'resampling':self.input_sample_rate != self.sample_rate}
    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread=threading.Thread(target=self._thread_main,name=f'sendspin-{self.source_id}',daemon=True); self._thread.start()
    def stop(self):
        self._stop.set()
        if self._fd is not None:
            try: os.close(self._fd)
            except OSError: pass
            self._fd=None
        if self._loop: self._loop.call_soon_threadsafe(lambda: None)
        if self._thread: self._thread.join(timeout=4)
    def _thread_main(self):
        try: asyncio.run(self._run())
        except Exception as exc: self.error=f'bridge fatal: {exc}'; self.state='error'
    def _load_identity(self):
        p=self.state_dir/'identity.json'
        if p.exists(): return Identity.from_private_bytes(b64url_decode(json.loads(p.read_text())['private_key']))
        ident=Identity.generate(); p.write_text(json.dumps({'private_key':ident.private_b64u},indent=2)); p.chmod(0o600); return ident
    async def _prepare_pairing(self, identity):
        store=await FileClientPairingStore.open(self.state_dir/'pairing.json'); pp=await store.get_pairing_psk()
        if pp is None:
            key=generate_psk(); pp=PairingPsk(psk_id=psk_id_for(key),psk=key); await store.set_pairing_psk(pp)
        self.pairing_token=encode_psk_token(PSKPairingToken(client_id=identity.peer_id,pairing_psk=pp.psk)); return store
    async def _run(self):
        self._loop=asyncio.get_running_loop(); identity=self._load_identity(); self.client_id=identity.peer_id; store=await self._prepare_pairing(identity)
        while not self._stop.is_set():
            client=None
            try:
                self.state='connecting'; self.error=None
                client=SendspinClient(identity=identity,client_name=self.name,roles=[Roles.SOURCE],pairing_store=store,source_support=ClientHelloSourceSupport())
                self._client=client; client.add_server_command_listener(self._on_command); await client.connect(self.server_url); self.connected=True
                self.paired=bool(client.noise_psk and client.noise_psk.category is PskCategory.LONG_TERM); self.state='paired_ready' if self.paired else 'connected_waiting_pairing'
                await self._fifo_loop(client)
            except Exception as exc:
                self.error=str(exc); self.state='waiting_pairing_or_server'; self.connected=False; self.streaming=False
                await asyncio.sleep(2)
            finally:
                self.connected=False; self.streaming=False; self._capture=None; self._close_fifo()
                if client:
                    try: await client.disconnect()
                    except Exception: pass
    def _on_command(self,payload):
        source=getattr(payload,'source',None)
        if source is None:return
        cmd=getattr(source,'command',None)
        if cmd==SourceCommand.START: asyncio.get_running_loop().create_task(self._start_capture())
        elif cmd==SourceCommand.STOP: asyncio.get_running_loop().create_task(self._stop_capture())
    async def _start_capture(self):
        if not self._client or not self._client.connected or self.streaming:return
        self.paired=bool(self._client.noise_psk and self._client.noise_psk.category is PskCategory.LONG_TERM)
        if not self.paired:self.state='pairing_required';return
        try:self._capture=self._client.create_source_capture(self.fmt);await self._capture.start();self.streaming=True;self.state='streaming'
        except Exception as exc:self.error=f'start capture: {exc}';self.state='error'
    async def _stop_capture(self):
        cap=self._capture;self.streaming=False;self._capture=None
        if cap:
            try:await cap.stop()
            except Exception as exc:self.error=f'stop capture: {exc}'
        if self.connected:self.state='paired_ready' if self.paired else 'connected_waiting_pairing'
    def _close_fifo(self):
        if self._fd is not None:
            try:os.close(self._fd)
            except OSError:pass
            self._fd=None
    def _read_chunk_nonblocking(self):
        if self._fd is None:
            try:self._fd=os.open(self.fifo,os.O_RDONLY|os.O_NONBLOCK)
            except OSError:return b''
        try:return os.read(self._fd,self.chunk_bytes)
        except BlockingIOError:return b''
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN,errno.EWOULDBLOCK):self._close_fifo()
            return b''
    async def _fifo_loop(self,client):
        while not self._stop.is_set() and client.connected:
            data=self._read_chunk_nonblocking(); self.paired=bool(client.noise_psk and client.noise_psk.category is PskCategory.LONG_TERM)
            if not data: await asyncio.sleep(.005); continue
            self.bytes_read+=len(data); self.last_pcm_at=time.time()
            try:
                peak=audioop.max(data,self.bit_depth//8); full=(1 << (self.bit_depth-1))-1; self.peak_dbfs=round(20*math.log10(max(peak,1)/full),1)
            except Exception: pass
            out=data
            if self.input_sample_rate != self.sample_rate:
                try:
                    out,self._rate_state=audioop.ratecv(data,self.bit_depth//8,self.channels,self.input_sample_rate,self.sample_rate,self._rate_state)
                except Exception as exc:
                    self.error=f'resample {self.input_sample_rate}->{self.sample_rate}: {exc}'; await asyncio.sleep(.005); continue
            if self.streaming and self._capture and out:
                await self._capture.feed(out,capture_timestamp_us=client.now_us()); self.bytes_sent+=len(out); self.last_audio_at=time.time()
