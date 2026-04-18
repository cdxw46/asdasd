// Cliente JS minimalista para la API REST de SMURF.
// Maneja JWT (cookie HttpOnly + token en localStorage como fallback).

const TOKEN_KEY = 'smurf_jwt';

export const api = {
  baseUrl: '/api/v1',

  get token() { return localStorage.getItem(TOKEN_KEY); },
  set token(v) { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); },

  async request(path, opts = {}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    const resp = await fetch(this.baseUrl + path, { ...opts, headers });
    if (resp.status === 401) {
      this.token = null;
      window.dispatchEvent(new CustomEvent('smurf:logout'));
      throw new Error('No autenticado');
    }
    const ct = resp.headers.get('content-type') || '';
    const body = ct.includes('application/json') ? await resp.json() : await resp.text();
    if (!resp.ok) {
      const msg = (body && body.detail) ? body.detail : (typeof body === 'string' ? body : 'Error');
      throw new Error(msg);
    }
    return body;
  },

  get(p) { return this.request(p); },
  post(p, b) { return this.request(p, { method: 'POST', body: JSON.stringify(b || {}) }); },
  put(p, b)  { return this.request(p, { method: 'PUT', body: JSON.stringify(b || {}) }); },
  del(p)     { return this.request(p, { method: 'DELETE' }); },

  async login(username, password, totp) {
    const r = await this.post('/auth/login', { username, password, totp });
    this.token = r.token;
    return r;
  },

  async logout() {
    try { await this.post('/auth/logout'); } catch (e) {}
    this.token = null;
  },

  async me() { return this.get('/auth/me'); },

  // WebSocket de eventos
  events(onEvent) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}${this.baseUrl}/ws/events?token=${encodeURIComponent(this.token || '')}`;
    const ws = new WebSocket(url);
    ws.onmessage = (m) => {
      try { onEvent(JSON.parse(m.data)); } catch (e) {}
    };
    return ws;
  },
};
