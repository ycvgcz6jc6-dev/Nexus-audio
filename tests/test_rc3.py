import asyncio
import importlib
import json
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'nexus_audio/app'))

@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv('NEXUS_DATA_DIR', str(tmp_path))
    import nexus_diag, main
    importlib.reload(nexus_diag)
    importlib.reload(main)
    return main, nexus_diag

def test_addon_layout():
    repo = yaml.safe_load((ROOT/'repository.yaml').read_text())
    config = yaml.safe_load((ROOT/'nexus_audio/config.yaml').read_text())
    assert repo['name'] and config['version'] == '1.0.0-rc3'
    assert config['slug'] == 'ha_audio_input_gateway'
    assert set(config['arch']) == {'amd64', 'aarch64'}
    for file in ['Dockerfile','run.sh','README.md','LICENSE','NOTICE','app/nexus-audio.jpg']:
        assert (ROOT/'nexus_audio'/file).is_file()

def test_real_sendspin_identity_and_capture(tmp_path):
    from sendspin_source import SendspinSourceBridge, SendspinClient, Roles, ClientHelloSourceSupport
    from aiosendspin.noise.pairing_token import decode_token
    bridge=SendspinSourceBridge('test','Test',tmp_path/'audio',tmp_path/'state','ws://127.0.0.1:8927/sendspin')
    async def run():
        identity=bridge._load_identity()
        assert bridge._load_identity().peer_id == identity.peer_id
        store=await bridge._prepare_pairing(identity)
        assert decode_token(bridge.pairing_token).client_id == identity.peer_id
        client=SendspinClient(identity=identity,client_name='Test',roles=[Roles.SOURCE],pairing_store=store,source_support=ClientHelloSourceSupport())
        with pytest.raises(RuntimeError, match='not connected'):
            client.create_source_capture(bridge.fmt)
        await client.disconnect()
    asyncio.run(run())

def test_spotify_arguments_and_normalization(modules, monkeypatch):
    main,_=modules
    for autoplay in [True, False]:
        worker=main.Worker({'id':'spotify','name':'Spotify','type':'spotify','spotify_autoplay':autoplay})
        calls=[]
        monkeypatch.setattr(worker,'_launch',lambda cmd,env:calls.append(cmd))
        monkeypatch.setattr(worker,'_start_sendspin',lambda *args,**kwargs:calls.append((args,kwargs)))
        monkeypatch.setattr(worker,'_start_watchdog',lambda:None)
        worker.start()
        assert worker.state == 'spotify_connect_ready'
        assert calls[0][-2:] == ['--autoplay','on' if autoplay else 'off']
        assert calls[1] == ((2,48000,16),{'input_sample_rate':44100})

def test_airplay_pcm_format(modules,monkeypatch):
    main,_=modules
    worker=main.Worker({'id':'airplay','name':'AirPlay','type':'airplay','interface':'eth0'})
    calls=[]
    monkeypatch.setattr(main,'iface_ipv4',lambda name:'192.0.2.1')
    monkeypatch.setattr(worker,'_launch',lambda *args:None)
    monkeypatch.setattr(worker,'_start_sendspin',lambda *args,**kwargs:calls.append((args,kwargs)))
    monkeypatch.setattr(worker,'_start_watchdog',lambda:None)
    worker.start()
    assert 'output_rate = 44100' in (worker.dir/'shairport-sync.conf').read_text()
    assert calls == [((2,48000,16),{'input_sample_rate':44100})]

def test_partial_pcm_frames_and_resampling(tmp_path):
    from sendspin_source import SendspinSourceBridge, PskCategory
    bridge=SendspinSourceBridge('pcm','PCM',tmp_path/'fifo',tmp_path/'state','ws://localhost',input_sample_rate=44100)
    pcm=struct.pack('<hh',1000,-1000)*4410
    chunks=iter([pcm[:1],pcm[1:3541],pcm[3541:]])
    output=[]
    class Capture:
        async def feed(self,data,**kwargs):output.append(data)
    client=SimpleNamespace(connected=True,noise_psk=SimpleNamespace(category=PskCategory.LONG_TERM),now_us=lambda:0)
    def read():
        try:return next(chunks)
        except StopIteration:bridge._stop.set();return b''
    bridge._read_chunk_nonblocking=read;bridge._capture=Capture();bridge.streaming=True
    asyncio.run(bridge._fifo_loop(client))
    assert bridge.error is None
    assert all(len(data)%4 == 0 for data in output)
    assert abs(sum(map(len,output))//4-4800) <= 2

def test_source_commands_use_sdk_payload(tmp_path):
    from sendspin_source import SendspinSourceBridge
    from aiosendspin.models.source import SourceCommandServerPayload
    bridge=SendspinSourceBridge('x','X',tmp_path/'fifo',tmp_path/'state','ws://localhost')
    seen=[]
    async def start():seen.append('start')
    async def stop():seen.append('stop')
    bridge._start_capture=start;bridge._stop_capture=stop
    async def run():
        for command in ['start','stop']:
            bridge._on_command(SimpleNamespace(source=SourceCommandServerPayload(command=command)))
            await asyncio.sleep(0)
    asyncio.run(run())
    assert seen == ['start','stop']

def test_diagnostics_redacts_source_pairing(modules,tmp_path):
    _,diag=modules
    diag.LOG_FILE.write_text('Worker started\nReceived PRIVATE\ntoken=unknown-credential\n')
    bundle=diag.build_bundle(tmp_path/'diagnostics.zip','1.0.0-rc3',{'airplay_password':'PRIVATE'},[],[{'sendspin':{'pairing_token':'PRIVATE','paired':False},'log_tail':['Received PRIVATE']}],[])
    with zipfile.ZipFile(bundle) as archive:
        data=archive.read('diagnostics.json')
        assert b'PRIVATE' not in data
        assert json.loads(data)['sources'][0]['sendspin']['paired'] is False
        logs=archive.read('logs/nexus-audio.log')
        assert b'PRIVATE' not in logs and b'unknown-credential' not in logs
        assert b'Worker started' in logs

def test_existing_worker_paths_preserved(modules,monkeypatch):
    main,_=modules
    for kind,method in [('airplay','_start_airplay'),('spotify','_start_spotify'),('dante_rx','_start_dante')]:
        worker=main.Worker({'id':kind,'name':kind,'type':kind})
        seen=[];monkeypatch.setattr(worker,method,lambda:seen.append(True));worker.start()
        assert seen == [True]
    worker=main.Worker({'id':'cast','name':'Cast','type':'cast'});worker.start()
    assert worker.state == 'experimental_not_started'

def test_sap_parse_and_sdp_cleanup(tmp_path,monkeypatch):
    from aes67_rx import SapDiscovery,Aes67Receiver
    sap=SapDiscovery('eth0','192.0.2.1')
    sap._parse(b'\x20\x00\x00\x01\xc0\x00\x02\x01application/sdp\x00v=0\ns=Test\nc=IN IP4 239.1.2.3\nm=audio 5004 RTP/AVP 96\na=rtpmap:96 L24/48000/2\n',('192.0.2.2',9875))
    assert next(iter(sap.sessions.values()))['channels'] == 2
    receiver=Aes67Receiver({'multicast_ip':'239.1.2.3'},tmp_path/'audio.pcm','192.0.2.1')
    monkeypatch.setattr('aes67_rx.subprocess.Popen',lambda *a,**kw: (_ for _ in ()).throw(OSError('test')))
    receiver.start();assert receiver.sdp_path.exists()
    receiver.stop();assert not receiver.sdp_path.exists()
