"""Plantillas de aprovisionamiento por fabricante.

Cada generador toma:
    vendor: yealink|snom|fanvil|grandstream|polycom|cisco
    model:  string libre
    ext:    fila SQLite con datos de la extensión
    cfg:    SmurfConfig

y devuelve el contenido del fichero de configuración como str.

Las plantillas son adaptaciones genéricas que cubren los parámetros más
habituales (registro SIP, codecs, NAT). El usuario puede afinar después
desde el panel.
"""
from __future__ import annotations

from ..util.config import SmurfConfig


def _yealink(ext, cfg: SmurfConfig) -> str:
    return f"""#!version:1.0.0.1
account.1.enable = 1
account.1.label = {ext['display_name'] or ext['number']}
account.1.display_name = {ext['display_name'] or ext['number']}
account.1.auth_name = {ext['number']}
account.1.user_name = {ext['number']}
account.1.password = {ext['sip_password']}
account.1.sip_server.1.address = {cfg.sip.public_ip or '0.0.0.0'}
account.1.sip_server.1.port = {cfg.sip.udp_port}
account.1.sip_server.1.transport_type = 0
account.1.sip_server.1.expires = 3600
account.1.outbound_proxy_enable = 0
account.1.codec.g711a.enable = 1
account.1.codec.g711u.enable = 1
account.1.codec.g722.enable = 1
account.1.nat.nat_traversal = 1
account.1.nat.rport = 1
voice.tone.country = Custom
"""


def _snom(ext, cfg: SmurfConfig) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<settings>
  <user_name idx="1" perm="">{ext['number']}</user_name>
  <user_pname idx="1" perm="">{ext['number']}</user_pname>
  <user_pass idx="1" perm="">{ext['sip_password']}</user_pass>
  <user_realname idx="1" perm="">{ext['display_name'] or ext['number']}</user_realname>
  <user_host idx="1" perm="">{cfg.sip.public_ip or '0.0.0.0'}:{cfg.sip.udp_port}</user_host>
  <user_outbound idx="1" perm=""></user_outbound>
  <user_active idx="1" perm="">on</user_active>
  <codec_priority_list perm="">PCMU,PCMA,G722</codec_priority_list>
</settings>
"""


def _fanvil(ext, cfg: SmurfConfig) -> str:
    return f"""<<VOIP CONFIG MODULE>>
SIP Line1 Enable :1
SIP Line1 Display Name :{ext['display_name'] or ext['number']}
SIP Line1 User Name :{ext['number']}
SIP Line1 Auth User :{ext['number']}
SIP Line1 Auth Password :{ext['sip_password']}
SIP Line1 SIP Server :{cfg.sip.public_ip or '0.0.0.0'}
SIP Line1 SIP Server Port :{cfg.sip.udp_port}
SIP Line1 Transport Type :0
SIP Line1 Expire Time :3600
SIP Line1 Audio Codec1 :8
SIP Line1 Audio Codec2 :0
SIP Line1 Audio Codec3 :9
"""


def _grandstream(ext, cfg: SmurfConfig) -> str:
    return f"""# Grandstream config
P271 = 1
P47 = {cfg.sip.public_ip or '0.0.0.0'}
P35 = {ext['number']}
P36 = {ext['number']}
P34 = {ext['sip_password']}
P3 = {ext['display_name'] or ext['number']}
P30 = {cfg.sip.udp_port}
P57 = 0
P57 = 8
"""


def _polycom(ext, cfg: SmurfConfig) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<polycomConfig>
  <reg reg.1.address="{ext['number']}" reg.1.label="{ext['display_name'] or ext['number']}"
       reg.1.auth.userId="{ext['number']}" reg.1.auth.password="{ext['sip_password']}"
       reg.1.server.1.address="{cfg.sip.public_ip or '0.0.0.0'}"
       reg.1.server.1.port="{cfg.sip.udp_port}" reg.1.server.1.expires="3600"/>
</polycomConfig>
"""


def _cisco(ext, cfg: SmurfConfig) -> str:
    return f"""<flat-profile>
  <Display_Name>{ext['display_name'] or ext['number']}</Display_Name>
  <User_ID>{ext['number']}</User_ID>
  <Password>{ext['sip_password']}</Password>
  <Auth_ID>{ext['number']}</Auth_ID>
  <Proxy>{cfg.sip.public_ip or '0.0.0.0'}:{cfg.sip.udp_port}</Proxy>
  <Register_Expires>3600</Register_Expires>
  <Preferred_Codec>G711u</Preferred_Codec>
</flat-profile>
"""


_GENERATORS = {
    "yealink": _yealink, "snom": _snom, "fanvil": _fanvil,
    "grandstream": _grandstream, "polycom": _polycom, "cisco": _cisco,
}


def generate_config(vendor: str, model: str, ext, cfg: SmurfConfig) -> str:
    gen = _GENERATORS.get(vendor.lower())
    if not gen:
        raise ValueError(f"Vendor no soportado: {vendor}")
    return gen(ext, cfg)
