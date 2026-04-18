// SIP-over-WebSocket cliente puro (RFC 7118) + WebRTC.
// No depende de librerías SIP de terceros.

import { md5 } from './md5.js';

function tok(n=8) {
  const a = new Uint8Array(n);
  crypto.getRandomValues(a);
  return Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
}

function parseAuth(value) {
  const i = value.indexOf(' ');
  const params = {};
  const rest = value.slice(i+1);
  const re = /(\w+)\s*=\s*("([^"]*)"|([^,]+))/g;
  let m;
  while ((m = re.exec(rest)) != null) {
    params[m[1].toLowerCase()] = (m[3] !== undefined ? m[3] : m[4]).trim();
  }
  return params;
}

function buildHeaders(o) {
  const lines = [];
  for (const [k, v] of Object.entries(o)) {
    if (Array.isArray(v)) v.forEach(vv => lines.push(`${k}: ${vv}`));
    else lines.push(`${k}: ${v}`);
  }
  return lines.join('\r\n');
}

function mkRequest(method, ruri, headers, body='') {
  const lines = [`${method} ${ruri} SIP/2.0`, buildHeaders(headers), '', body];
  return lines.join('\r\n');
}

function parseMessage(text) {
  const sep = text.indexOf('\r\n\r\n');
  const head = sep === -1 ? text : text.slice(0, sep);
  const body = sep === -1 ? '' : text.slice(sep + 4);
  const lines = head.split(/\r?\n/);
  const first = lines.shift() || '';
  const m = first.match(/^SIP\/2\.0\s+(\d{3})\s*(.*)$/);
  let req = null, resp = null;
  if (m) {
    resp = { status: parseInt(m[1], 10), reason: m[2] };
  } else {
    const r = first.match(/^([A-Z]+)\s+(\S+)\s+SIP\/2\.0$/);
    if (r) req = { method: r[1], uri: r[2] };
  }
  const headers = {};
  let cur = null;
  for (const ln of lines) {
    if (/^[ \t]/.test(ln) && cur) { headers[cur][headers[cur].length - 1] += ' ' + ln.trim(); continue; }
    const i = ln.indexOf(':');
    if (i < 0) continue;
    const name = ln.slice(0, i).trim();
    const val = ln.slice(i + 1).trim();
    cur = name.toLowerCase();
    headers[cur] = headers[cur] || [];
    headers[cur].push(val);
  }
  return { req, resp, headers, body };
}

function getH(h, name) {
  const v = h[name.toLowerCase()];
  return v ? v[0] : null;
}

function digestResponse({ challenge, username, password, method, uri, body }) {
  const realm = challenge.realm;
  const nonce = challenge.nonce;
  const algo = (challenge.algorithm || 'MD5').toUpperCase();
  const qop = challenge.qop ? (challenge.qop.split(',').map(s=>s.trim()).includes('auth') ? 'auth' : challenge.qop.split(',')[0].trim()) : '';
  const cnonce = tok(8);
  const nc = '00000001';
  // Sólo MD5 está soportado en navegador puro (sha256 disponible vía WebCrypto pero síncrono ⇒ usamos MD5)
  const ha1 = md5(`${username}:${realm}:${password}`);
  const ha2 = md5(`${method}:${uri}`);
  let response;
  if (qop) response = md5(`${ha1}:${nonce}:${nc}:${cnonce}:${qop}:${ha2}`);
  else     response = md5(`${ha1}:${nonce}:${ha2}`);
  let auth = `Digest username="${username}", realm="${realm}", nonce="${nonce}", uri="${uri}", algorithm=${algo}, response="${response}"`;
  if (qop)   auth += `, qop=${qop}, nc=${nc}, cnonce="${cnonce}"`;
  if (challenge.opaque) auth += `, opaque="${challenge.opaque}"`;
  return auth;
}

export class SipWsPhone extends EventTarget {
  constructor({ wsUrl, realm, username, password, displayName }) {
    super();
    this.wsUrl = wsUrl;
    this.realm = realm;
    this.username = username;
    this.password = password;
    this.displayName = displayName || username;
    this.ws = null;
    this.contact = null;
    this.localTag = tok(6);
    this.callId = `${tok(10)}@web.smurf`;
    this.cseq = 0;
    this.transactions = new Map(); // branch → resolver
    this.activeCall = null;
    this.registered = false;
    this.regTimer = null;
    this.audioEl = null;
  }

  log(...a) { console.log('[SIP/WS]', ...a); this.dispatchEvent(new CustomEvent('log',{detail:a})); }

  connect() {
    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(this.wsUrl, ['sip']);
      } catch (e) { return reject(e); }
      this.ws.onopen = () => { this.log('WS abierto'); resolve(); };
      this.ws.onclose = () => { this.log('WS cerrado'); this.dispatchEvent(new Event('disconnected')); };
      this.ws.onerror = (e) => { this.log('WS error', e); reject(e); };
      this.ws.onmessage = (m) => this._onWsMessage(m.data);
    });
  }

  send(text) {
    if (this.ws.readyState !== 1) return;
    this.ws.send(text);
  }

  _viaBranch() { return 'z9hG4bK-' + tok(8); }

  _baseHeaders(method, ruri, opts={}) {
    this.cseq += 1;
    const callId = opts.callId || this.callId;
    const fromTag = opts.fromTag || this.localTag;
    const toTag = opts.toTag || '';
    const branch = this._viaBranch();
    const ip = location.hostname;
    const localContact = this.contact || `<sip:${this.username}@${ip};transport=ws;ob>`;
    const headers = {
      'Via': `SIP/2.0/WS df7jal23ls0d.invalid;branch=${branch};rport`,
      'Max-Forwards': '70',
      'From': `"${this.displayName}" <sip:${this.username}@${this.realm}>;tag=${fromTag}`,
      'To': toTag ? `<${ruri}>;tag=${toTag}` : `<${ruri}>`,
      'Call-ID': callId,
      'CSeq': `${opts.cseq || this.cseq} ${method}`,
      'Contact': localContact,
      'User-Agent': 'SMURF-Web/1.0',
      'Content-Length': '0',
    };
    return { headers, branch };
  }

  async register() {
    return this._registerOnce();
  }

  async _registerOnce(challenge=null, retryCseq=null) {
    const ruri = `sip:${this.realm}`;
    const { headers, branch } = this._baseHeaders('REGISTER', ruri);
    headers['To'] = `<sip:${this.username}@${this.realm}>`;
    headers['From'] = `"${this.displayName}" <sip:${this.username}@${this.realm}>;tag=${this.localTag}`;
    headers['Expires'] = '600';
    headers['Allow'] = 'INVITE, ACK, BYE, CANCEL, OPTIONS, INFO, MESSAGE, REFER, NOTIFY, UPDATE';
    if (retryCseq != null) headers['CSeq'] = `${retryCseq} REGISTER`;
    if (challenge) {
      headers['Authorization'] = digestResponse({
        challenge, username: this.username, password: this.password,
        method: 'REGISTER', uri: ruri, body: '',
      });
    }
    const msg = mkRequest('REGISTER', ruri, headers);
    const resp = await this._txRequest(msg, branch);
    if (resp.resp.status === 401 && !challenge) {
      const ch = parseAuth(getH(resp.headers, 'WWW-Authenticate') || '');
      return this._registerOnce(ch, this.cseq + 1);
    }
    if (resp.resp.status >= 200 && resp.resp.status < 300) {
      this.registered = true;
      this.dispatchEvent(new CustomEvent('registered', {detail: resp}));
      // Re-registrar 30s antes de expirar
      const expires = parseInt(getH(resp.headers, 'Expires') || '600', 10);
      clearTimeout(this.regTimer);
      this.regTimer = setTimeout(() => this.register().catch(()=>{}), Math.max(30, expires - 30) * 1000);
      return true;
    }
    throw new Error(`REGISTER falló: ${resp.resp.status} ${resp.resp.reason}`);
  }

  _txRequest(msg, branch) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.transactions.delete(branch); reject(new Error('timeout')); }, 32000);
      this.transactions.set(branch, (m) => {
        if (m.resp && m.resp.status >= 200) {
          clearTimeout(t); this.transactions.delete(branch); resolve(m);
        }
      });
      this.send(msg);
    });
  }

  _onWsMessage(text) {
    const msg = parseMessage(typeof text === 'string' ? text : new TextDecoder().decode(text));
    const via = getH(msg.headers, 'Via') || '';
    const branchMatch = via.match(/branch=([^;,\s]+)/);
    const branch = branchMatch ? branchMatch[1] : '';
    if (msg.resp) {
      const cb = this.transactions.get(branch);
      if (cb) cb(msg);
      // INVITE responses → puede ser de la llamada activa
      if (this.activeCall) this.activeCall._onResponse(msg);
    } else if (msg.req) {
      this._onRequest(msg);
    }
  }

  _onRequest(msg) {
    const m = msg.req.method;
    if (m === 'OPTIONS') {
      this._respond(msg, 200, 'OK', {'Allow': 'INVITE,ACK,BYE,CANCEL,OPTIONS,INFO,MESSAGE'});
      return;
    }
    if (m === 'INVITE') {
      // Llamada entrante
      this._onIncoming(msg);
      return;
    }
    if (m === 'BYE') {
      this._respond(msg, 200, 'OK');
      if (this.activeCall) this.activeCall._remoteHangup();
      return;
    }
    if (m === 'ACK') return;
    if (m === 'NOTIFY' || m === 'MESSAGE') {
      this._respond(msg, 200, 'OK');
      this.dispatchEvent(new CustomEvent('message', {detail: msg}));
      return;
    }
    this._respond(msg, 405, 'Method Not Allowed');
  }

  _respond(req, status, reason, extra={}, body='') {
    const lines = [`SIP/2.0 ${status} ${reason}`];
    for (const v of (req.headers.via || [])) lines.push(`Via: ${v}`);
    if (getH(req.headers, 'From')) lines.push(`From: ${getH(req.headers, 'From')}`);
    let to = getH(req.headers, 'To');
    if (status >= 200 && to && !/;tag=/.test(to)) to += `;tag=${tok(6)}`;
    if (to) lines.push(`To: ${to}`);
    if (getH(req.headers, 'Call-ID')) lines.push(`Call-ID: ${getH(req.headers, 'Call-ID')}`);
    if (getH(req.headers, 'CSeq')) lines.push(`CSeq: ${getH(req.headers, 'CSeq')}`);
    for (const [k, v] of Object.entries(extra)) lines.push(`${k}: ${v}`);
    lines.push(`Content-Length: ${body.length}`);
    lines.push(''); lines.push(body);
    this.send(lines.join('\r\n'));
  }

  async _onIncoming(req) {
    if (this.activeCall) {
      this._respond(req, 486, 'Busy Here');
      return;
    }
    const call = new IncomingCall(this, req);
    this.activeCall = call;
    await call.start();
    this.dispatchEvent(new CustomEvent('incoming', {detail: call}));
  }

  async call(target) {
    if (this.activeCall) throw new Error('Llamada ya en curso');
    const call = new OutgoingCall(this, target);
    this.activeCall = call;
    await call.start();
    return call;
  }

  hangup() {
    if (this.activeCall) this.activeCall.hangup();
  }
}

class CallBase extends EventTarget {
  constructor(phone) {
    super();
    this.phone = phone;
    this.pc = null;
    this.audioEl = null;
    this.localStream = null;
    this.callId = `${tok(10)}@web.smurf`;
    this.localTag = tok(6);
    this.remoteTag = '';
    this.cseq = 0;
    this.dialogReqHeaders = null;
    this.state = 'init';
  }

  setState(s) { this.state = s; this.dispatchEvent(new CustomEvent('state', {detail: s})); }

  async _setupMedia(direction='send') {
    if (!this.localStream) {
      this.localStream = await navigator.mediaDevices.getUserMedia({audio: true});
    }
    this.pc = new RTCPeerConnection({
      iceServers: [{urls: 'stun:stun.l.google.com:19302'}],
    });
    for (const t of this.localStream.getTracks()) {
      this.pc.addTrack(t, this.localStream);
    }
    this.pc.ontrack = (ev) => {
      if (!this.audioEl) {
        this.audioEl = new Audio();
        this.audioEl.autoplay = true;
        document.body.appendChild(this.audioEl);
      }
      this.audioEl.srcObject = ev.streams[0];
    };
  }

  hangup() {
    this.cseq += 1;
    const branch = 'z9hG4bK-' + tok(8);
    const remote = this._remoteTarget();
    const msg = [
      `BYE ${remote} SIP/2.0`,
      `Via: SIP/2.0/WS df7jal23ls0d.invalid;branch=${branch};rport`,
      `Max-Forwards: 70`,
      `From: ${this._fromHeader()}`,
      `To: ${this._toHeader()}`,
      `Call-ID: ${this.callId}`,
      `CSeq: ${this.cseq} BYE`,
      `User-Agent: SMURF-Web/1.0`,
      `Content-Length: 0`,
      '', '',
    ].join('\r\n');
    this.phone.send(msg);
    this._cleanup();
  }

  _remoteHangup() {
    this.setState('terminated');
    this._cleanup();
  }

  _cleanup() {
    try { if (this.pc) this.pc.close(); } catch (e) {}
    try { for (const t of (this.localStream||{getTracks:()=>[]}).getTracks()) t.stop(); } catch (e) {}
    if (this.audioEl) { try { this.audioEl.remove(); } catch (e) {} this.audioEl = null; }
    this.phone.activeCall = null;
    this.dispatchEvent(new Event('end'));
  }

  sendDtmf(digit) {
    if (!this.pc) return;
    const sender = this.pc.getSenders().find(s => s.dtmf);
    if (sender && sender.dtmf) sender.dtmf.insertDTMF(digit, 200, 70);
  }

  _fromHeader() { return ''; }
  _toHeader()   { return ''; }
  _remoteTarget() { return ''; }
  _onResponse(_) {}
}

class OutgoingCall extends CallBase {
  constructor(phone, target) { super(phone); this.target = target; }

  _fromHeader() { return `"${this.phone.displayName}" <sip:${this.phone.username}@${this.phone.realm}>;tag=${this.localTag}`; }
  _toHeader()   { return this.remoteTag ? `<sip:${this.target}@${this.phone.realm}>;tag=${this.remoteTag}` : `<sip:${this.target}@${this.phone.realm}>`; }
  _remoteTarget() { return `sip:${this.target}@${this.phone.realm}`; }

  async start() {
    await this._setupMedia('send');
    const offer = await this.pc.createOffer({offerToReceiveAudio: true});
    await this.pc.setLocalDescription(offer);
    // Esperar a que ICE termine (gathering complete)
    await new Promise(r => {
      if (this.pc.iceGatheringState === 'complete') return r();
      const t = setTimeout(r, 2000);
      this.pc.addEventListener('icegatheringstatechange', () => {
        if (this.pc.iceGatheringState === 'complete') { clearTimeout(t); r(); }
      });
    });
    this._sendInvite(this.pc.localDescription.sdp);
    this.setState('calling');
  }

  _sendInvite(sdp, authHeader=null) {
    this.cseq += 1;
    const branch = 'z9hG4bK-' + tok(8);
    const ruri = `sip:${this.target}@${this.phone.realm}`;
    const lines = [
      `INVITE ${ruri} SIP/2.0`,
      `Via: SIP/2.0/WS df7jal23ls0d.invalid;branch=${branch};rport`,
      `Max-Forwards: 70`,
      `From: ${this._fromHeader()}`,
      `To: <${ruri}>`,
      `Call-ID: ${this.callId}`,
      `CSeq: ${this.cseq} INVITE`,
      `Contact: <sip:${this.phone.username}@${location.hostname};transport=ws;ob>`,
      `Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, INFO, MESSAGE, REFER, NOTIFY`,
      `Supported: replaces, outbound`,
      `User-Agent: SMURF-Web/1.0`,
    ];
    if (authHeader) lines.push(authHeader);
    lines.push(`Content-Type: application/sdp`);
    lines.push(`Content-Length: ${sdp.length}`);
    lines.push(''); lines.push(sdp);
    this.lastInvite = { sdp, branch };
    this.phone.send(lines.join('\r\n'));
  }

  async _onResponse(msg) {
    const code = msg.resp.status;
    const cseqLine = getH(msg.headers, 'CSeq') || '';
    if (!cseqLine.toUpperCase().endsWith(' INVITE')) return;
    if (code === 100) return;
    if (code === 180 || code === 183) { this.setState('ringing'); return; }
    if (code === 401 || code === 407) {
      const challengeHeader = code === 407 ? 'Proxy-Authenticate' : 'WWW-Authenticate';
      const ch = parseAuth(getH(msg.headers, challengeHeader) || '');
      // ACK manual al 4xx
      this._ackFailure(msg);
      const ruri = `sip:${this.target}@${this.phone.realm}`;
      const auth = digestResponse({ challenge: ch, username: this.phone.username, password: this.phone.password,
                                    method: 'INVITE', uri: ruri, body: this.lastInvite.sdp });
      const headerName = code === 407 ? 'Proxy-Authorization' : 'Authorization';
      this._sendInvite(this.lastInvite.sdp, `${headerName}: ${auth}`);
      return;
    }
    if (code >= 200 && code < 300) {
      // Extraer remote tag
      const to = getH(msg.headers, 'To');
      const m = to.match(/;tag=([^,;\s]+)/i);
      if (m) this.remoteTag = m[1];
      // ACK
      this._ack(msg);
      // SDP del peer
      try { await this.pc.setRemoteDescription({type: 'answer', sdp: msg.body}); } catch (e) { console.error(e); }
      this.setState('answered');
      return;
    }
    if (code >= 300) {
      this._ackFailure(msg);
      this.setState('failed');
      this._cleanup();
    }
  }

  _ack(resp) {
    const branch = 'z9hG4bK-' + tok(8);
    const ruri = this._remoteTarget();
    const lines = [
      `ACK ${ruri} SIP/2.0`,
      `Via: SIP/2.0/WS df7jal23ls0d.invalid;branch=${branch}`,
      `Max-Forwards: 70`,
      `From: ${this._fromHeader()}`,
      `To: ${getH(resp.headers, 'To')}`,
      `Call-ID: ${this.callId}`,
      `CSeq: ${this.cseq} ACK`,
      `Content-Length: 0`, '', '',
    ];
    this.phone.send(lines.join('\r\n'));
  }

  _ackFailure(resp) {
    const ruri = `sip:${this.target}@${this.phone.realm}`;
    const lines = [
      `ACK ${ruri} SIP/2.0`,
      `Via: ${getH(resp.headers, 'Via')}`,
      `Max-Forwards: 70`,
      `From: ${getH(resp.headers, 'From')}`,
      `To: ${getH(resp.headers, 'To')}`,
      `Call-ID: ${getH(resp.headers, 'Call-ID')}`,
      `CSeq: ${this.cseq} ACK`,
      `Content-Length: 0`, '', '',
    ];
    this.phone.send(lines.join('\r\n'));
  }
}

class IncomingCall extends CallBase {
  constructor(phone, req) {
    super(phone);
    this.req = req;
    this.callId = getH(req.headers, 'Call-ID');
    const from = getH(req.headers, 'From') || '';
    const m = from.match(/<sip:([^@>]+)@/);
    this.from = m ? m[1] : 'unknown';
    this.remoteTag = (from.match(/;tag=([^,;\s]+)/) || [])[1] || '';
  }

  _fromHeader() { return getH(this.req.headers, 'From'); }
  _toHeader() {
    let to = getH(this.req.headers, 'To');
    if (!/;tag=/.test(to)) to += `;tag=${this.localTag}`;
    return to;
  }
  _remoteTarget() {
    const c = getH(this.req.headers, 'Contact') || '';
    const m = c.match(/<([^>]+)>/);
    return m ? m[1] : `sip:${this.from}@${this.phone.realm}`;
  }

  async start() {
    // 180 Ringing
    this.phone._respond(this.req, 180, 'Ringing', {'Contact': `<sip:${this.phone.username}@${location.hostname};transport=ws;ob>`});
    this.setState('ringing');
  }

  async accept() {
    await this._setupMedia('recv');
    await this.pc.setRemoteDescription({type: 'offer', sdp: this.req.body});
    const answer = await this.pc.createAnswer();
    await this.pc.setLocalDescription(answer);
    await new Promise(r => {
      if (this.pc.iceGatheringState === 'complete') return r();
      const t = setTimeout(r, 2000);
      this.pc.addEventListener('icegatheringstatechange', () => {
        if (this.pc.iceGatheringState === 'complete') { clearTimeout(t); r(); }
      });
    });
    const sdp = this.pc.localDescription.sdp;
    this.phone._respond(this.req, 200, 'OK', {
      'Contact': `<sip:${this.phone.username}@${location.hostname};transport=ws;ob>`,
      'Content-Type': 'application/sdp',
    }, sdp);
    this.setState('answered');
  }

  reject() {
    this.phone._respond(this.req, 486, 'Busy Here');
    this._cleanup();
  }
}
