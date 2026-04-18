"""Smoke test del API: arranca SMURF, hace login, consulta dashboard."""
import asyncio, tempfile, os, sys
sys.path.insert(0, '/workspace/smurf')
from smurfd.server import SmurfServer
from smurfd.util.config import (SmurfConfig, SipConfig, RtpConfig, WebConfig,
                                 StorageConfig, SecurityConfig)
import smurfd.db.database as dbmod
dbmod._INSTANCE = None
tmp = tempfile.mkdtemp()
cfg = SmurfConfig(
    sip=SipConfig(udp_bind='127.0.0.1', udp_port=15091, tcp_port=0, tls_port=0,
                  ws_port=15092, wss_port=0, public_ip='127.0.0.1', realm='smurf.test'),
    rtp=RtpConfig(bind='127.0.0.1', port_min=41200, port_max=41400),
    web=WebConfig(bind='127.0.0.1', http_port=15031, https_port=0),
    storage=StorageConfig(db_path=os.path.join(tmp,'smurf.db'),
        recordings_dir=os.path.join(tmp,'rec'), voicemail_dir=os.path.join(tmp,'vm'),
        sounds_dir=os.path.join(tmp,'snd'), provisioning_dir=os.path.join(tmp,'prov'),
        log_dir=os.path.join(tmp,'log')))


async def main():
    s = SmurfServer(cfg)
    await s.start(with_api=True)
    await asyncio.sleep(2)
    import httpx
    async with httpx.AsyncClient(base_url='http://127.0.0.1:15031', timeout=5) as cli:
        print('healthz:', (await cli.get('/healthz')).json())
        print('spa /:', (await cli.get('/')).status_code)
        r = await cli.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'smurf-admin'})
        print('login:', r.status_code, r.json().get('user', {}).get('role'))
        tk = r.json()['token']
        h = {'Authorization': f'Bearer {tk}'}
        print('dashboard:', (await cli.get('/api/v1/dashboard', headers=h)).json())
        print('exts:', [x['number'] for x in (await cli.get('/api/v1/extensions', headers=h)).json()])
        print('openapi:', (await cli.get('/api/v1/openapi.json')).status_code)
    await s.stop()


asyncio.run(main())
