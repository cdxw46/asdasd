const api = {
  token: null,
  baseHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    return headers;
  },
  async request(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: { ...this.baseHeaders(), ...(options.headers || {}) },
    });
    if (res.status === 401) {
      throw new Error("No autorizado");
    }
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const payload = await res.json();
        if (payload.detail) msg = payload.detail;
      } catch (_e) {}
      throw new Error(msg);
    }
    return res.json();
  },
};

function byId(id) {
  return document.getElementById(id);
}

function setStatus(text, type = "info") {
  const el = byId("status");
  el.textContent = text;
  el.className = type;
}

async function login() {
  const username = byId("username").value.trim();
  const password = byId("password").value;
  const otp_code = byId("otp").value.trim();
  try {
    const payload = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, otp_code: otp_code || null }),
    }).then((r) => r.json().then((j) => ({ status: r.status, body: j })));
    if (payload.status !== 200) {
      throw new Error(payload.body.detail || "Error de login");
    }
    api.token = payload.body.access_token;
    localStorage.setItem("smurf_token", api.token);
    setStatus("Autenticado", "ok");
    byId("auth-panel").classList.add("hidden");
    byId("dashboard-panel").classList.remove("hidden");
    await refreshAll();
  } catch (err) {
    setStatus(`Login fallido: ${err.message}`, "error");
  }
}

async function refreshDashboard() {
  const data = await api.request("/api/v1/dashboard");
  byId("kpi-active-calls").textContent = data.active_calls ?? 0;
  byId("kpi-registered").textContent = data.registered_extensions ?? 0;
  byId("kpi-trunks").textContent = data.active_trunks ?? 0;
  byId("kpi-calls-today").textContent = data.calls_today ?? 0;

  const calls = data.active_calls_detail || [];
  byId("active-calls").textContent = JSON.stringify(calls, null, 2);
}

async function refreshExtensions() {
  const data = await api.request("/api/v1/extensions");
  byId("extensions").textContent = JSON.stringify(data.items || [], null, 2);
}

async function refreshCdr() {
  const data = await api.request("/api/v1/cdr?limit=100");
  byId("cdr").textContent = JSON.stringify(data.items || [], null, 2);
}

async function refreshPresence() {
  const data = await api.request("/api/v1/presence");
  byId("presence").textContent = JSON.stringify(data.items || [], null, 2);
}

async function refreshAll() {
  try {
    await Promise.all([
      refreshDashboard(),
      refreshExtensions(),
      refreshCdr(),
      refreshPresence(),
    ]);
    setStatus("Datos actualizados", "ok");
  } catch (err) {
    setStatus(`Error refrescando panel: ${err.message}`, "error");
  }
}

async function createExtension() {
  const extension = byId("new-extension").value.trim();
  const display_name = byId("new-display").value.trim();
  const auth_username = byId("new-auth-user").value.trim() || extension;
  const auth_password = byId("new-auth-pass").value.trim();
  try {
    await api.request("/api/v1/extensions", {
      method: "POST",
      body: JSON.stringify({
        extension,
        display_name,
        auth_username,
        auth_password,
        voicemail_pin: "1234",
        max_calls: 3,
        role: "user",
      }),
    });
    setStatus(`Extensión ${extension} creada`, "ok");
    await refreshExtensions();
  } catch (err) {
    setStatus(`Error creando extensión: ${err.message}`, "error");
  }
}

async function sendChat() {
  const from_ext = byId("chat-from").value.trim();
  const to_ext = byId("chat-to").value.trim();
  const message = byId("chat-message").value.trim();
  try {
    await api.request("/api/v1/chat/send", {
      method: "POST",
      body: JSON.stringify({ from_ext, to_ext, message }),
    });
    const history = await api.request(
      `/api/v1/chat/history?ext_a=${encodeURIComponent(
        from_ext
      )}&ext_b=${encodeURIComponent(to_ext)}&limit=50`
    );
    byId("chat-history").textContent = JSON.stringify(history.items || [], null, 2);
    setStatus("Mensaje enviado", "ok");
  } catch (err) {
    setStatus(`Chat error: ${err.message}`, "error");
  }
}

async function setPresence() {
  const extension = byId("presence-ext").value.trim();
  const status = byId("presence-status").value.trim();
  const note = byId("presence-note").value.trim();
  try {
    await api.request("/api/v1/presence/set", {
      method: "POST",
      body: JSON.stringify({ extension, status, note }),
    });
    await refreshPresence();
    setStatus("Presencia actualizada", "ok");
  } catch (err) {
    setStatus(`Error presencia: ${err.message}`, "error");
  }
}

async function exportCsv() {
  const res = await fetch("/api/v1/cdr/export/csv", {
    headers: api.baseHeaders(),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "smurf_cdr.csv";
  a.click();
  URL.revokeObjectURL(url);
}

async function exportExcel() {
  const res = await fetch("/api/v1/cdr/export/excel", {
    headers: api.baseHeaders(),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "smurf_cdr.xlsx";
  a.click();
  URL.revokeObjectURL(url);
}

function restoreTokenFromStorage() {
  const token = localStorage.getItem("smurf_token");
  if (token) {
    api.token = token;
    byId("auth-panel").classList.add("hidden");
    byId("dashboard-panel").classList.remove("hidden");
    refreshAll().catch(() => {
      localStorage.removeItem("smurf_token");
      api.token = null;
      byId("auth-panel").classList.remove("hidden");
      byId("dashboard-panel").classList.add("hidden");
    });
  }
}

const softphone = {
  ws: null,
  cseq: 1,
  callId: null,
  fromTag: `${Math.random().toString(16).slice(2, 10)}`,
  toTag: null,
  branch() {
    return `z9hG4bK-${Math.random().toString(16).slice(2, 12)}`;
  },
  nextCseq() {
    this.cseq += 1;
    return this.cseq;
  },
};

function softphoneLog(text) {
  const out = byId("softphone-log");
  out.textContent += `${new Date().toISOString()} ${text}\n`;
  out.scrollTop = out.scrollHeight;
}

function sipMessage(method, uri, headers, body = "") {
  const lines = [`${method} ${uri} SIP/2.0`];
  for (const [k, v] of Object.entries(headers)) {
    lines.push(`${k}: ${v}`);
  }
  lines.push(`Content-Length: ${new TextEncoder().encode(body).length}`);
  lines.push("");
  lines.push(body);
  return lines.join("\r\n");
}

function parseSipField(raw, field) {
  const re = new RegExp(`^${field}:\\s*(.+)$`, "im");
  const m = raw.match(re);
  return m ? m[1].trim() : "";
}

function setupSoftphoneStub() {
  const registerBtn = byId("sp-register");
  const callBtn = byId("sp-call");
  const hangupBtn = byId("sp-hangup");

  registerBtn.addEventListener("click", () => {
    const ext = byId("sp-ext").value.trim();
    const pass = byId("sp-pass").value.trim();
    const domain = byId("sp-domain").value.trim();
    let wsUrl = byId("sp-ws").value.trim();
    if (!wsUrl) {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      wsUrl = `${proto}://${location.hostname}:5062`;
      byId("sp-ws").value = wsUrl;
    }
    if (!ext || !pass || !domain) {
      softphoneLog("Faltan datos de registro SIP.");
      return;
    }
    if (softphone.ws && softphone.ws.readyState === WebSocket.OPEN) {
      softphoneLog("Softphone ya conectado.");
      return;
    }
    softphone.ws = new WebSocket(wsUrl, "sip");
    softphone.ws.onopen = () => {
      softphoneLog(`WS conectado a ${wsUrl}`);
      const branch = softphone.branch();
      const callId = `reg-${Date.now()}-${ext}@${domain}`;
      const fromTag = softphone.fromTag;
      const uri = `sip:${domain}`;
      const msg = sipMessage("REGISTER", uri, {
        Via: `SIP/2.0/WS webclient;branch=${branch};rport`,
        "Max-Forwards": "70",
        To: `<sip:${ext}@${domain}>`,
        From: `<sip:${ext}@${domain}>;tag=${fromTag}`,
        "Call-ID": callId,
        CSeq: `${softphone.nextCseq()} REGISTER`,
        Contact: `<sip:${ext}@webclient;transport=ws>`,
        Expires: "300",
        "User-Agent": "SMURF-WebPhone/1.0",
      });
      softphone.ws.send(msg);
      softphoneLog("REGISTER enviado.");
    };
    softphone.ws.onmessage = async (ev) => {
      const raw = typeof ev.data === "string" ? ev.data : await ev.data.text();
      const statusLine = raw.split(/\r?\n/, 1)[0];
      softphoneLog(`RX ${statusLine}`);

      if (statusLine.includes(" 401 ")) {
        const extNow = byId("sp-ext").value.trim();
        const passNow = byId("sp-pass").value.trim();
        const domainNow = byId("sp-domain").value.trim();
        const www = parseSipField(raw, "WWW-Authenticate");
        const nonceMatch = www.match(/nonce=\"([^\"]+)\"/i);
        const realmMatch = www.match(/realm=\"([^\"]+)\"/i);
        const algoMatch = www.match(/algorithm=([A-Za-z0-9-]+)/i);
        const nonce = nonceMatch ? nonceMatch[1] : "";
        const realm = realmMatch ? realmMatch[1] : domainNow;
        const algorithm = algoMatch ? algoMatch[1] : "MD5";
        const uri = `sip:${domainNow}`;
        const cnonce = Math.random().toString(16).slice(2, 10);
        const nc = "00000001";
        const qop = "auth";
        const ha1Raw = `${extNow}:${realm}:${passNow}`;
        const ha2Raw = `REGISTER:${uri}`;
        let ha1 = "";
        let ha2 = "";
        if (algorithm.toUpperCase() === "SHA-256") {
          ha1 = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(ha1Raw)))).map((b) => b.toString(16).padStart(2, "0")).join("");
          ha2 = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(ha2Raw)))).map((b) => b.toString(16).padStart(2, "0")).join("");
        } else {
          ha1 = Array.from(new Uint8Array(await crypto.subtle.digest("MD5", new TextEncoder().encode(ha1Raw)))).map((b) => b.toString(16).padStart(2, "0")).join("");
          ha2 = Array.from(new Uint8Array(await crypto.subtle.digest("MD5", new TextEncoder().encode(ha2Raw)))).map((b) => b.toString(16).padStart(2, "0")).join("");
        }
        const respRaw = `${ha1}:${nonce}:${nc}:${cnonce}:${qop}:${ha2}`;
        let response = "";
        if (algorithm.toUpperCase() === "SHA-256") {
          response = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(respRaw)))).map((b) => b.toString(16).padStart(2, "0")).join("");
        } else {
          response = Array.from(new Uint8Array(await crypto.subtle.digest("MD5", new TextEncoder().encode(respRaw)))).map((b) => b.toString(16).padStart(2, "0")).join("");
        }
        const authHeader = `Digest username="${extNow}", realm="${realm}", nonce="${nonce}", uri="${uri}", response="${response}", algorithm=${algorithm}, qop=${qop}, nc=${nc}, cnonce="${cnonce}"`;
        const msg = sipMessage("REGISTER", uri, {
          Via: `SIP/2.0/WS webclient;branch=${softphone.branch()};rport`,
          "Max-Forwards": "70",
          To: `<sip:${extNow}@${domainNow}>`,
          From: `<sip:${extNow}@${domainNow}>;tag=${softphone.fromTag}`,
          "Call-ID": `reg-${Date.now()}-${extNow}@${domainNow}`,
          CSeq: `${softphone.nextCseq()} REGISTER`,
          Contact: `<sip:${extNow}@webclient;transport=ws>`,
          Expires: "300",
          Authorization: authHeader,
          "User-Agent": "SMURF-WebPhone/1.0",
        });
        softphone.ws.send(msg);
        softphoneLog("REGISTER autenticado enviado.");
      }
      if (statusLine.includes(" 200 ")) {
        softphoneLog("Registro SIP completado.");
      }
    };
    softphone.ws.onclose = () => {
      softphoneLog("WS SIP desconectado.");
      softphone.ws = null;
    };
    softphone.ws.onerror = () => {
      softphoneLog("Error en WS SIP.");
    };
  });

  callBtn.addEventListener("click", async () => {
    const target = byId("sp-target").value.trim();
    const ext = byId("sp-ext").value.trim();
    const domain = byId("sp-domain").value.trim();
    if (!api.token) {
      softphoneLog("Necesitas sesión API para originar llamada.");
      return;
    }
    if (!target || !ext) {
      softphoneLog("Falta destino o extensión de origen.");
      return;
    }
    try {
      const call = await api.request("/api/v1/calls/originate", {
        method: "POST",
        body: JSON.stringify({ from_ext: ext, to_ext: target }),
      });
      softphone.callId = call.call_id;
      softphoneLog(`Llamada originada vía PBX call_id=${softphone.callId}`);
      if (softphone.ws && softphone.ws.readyState === WebSocket.OPEN) {
        const body = [
          "v=0",
          "o=smurf 0 0 IN IP4 127.0.0.1",
          "s=SMURF WebPhone",
          "c=IN IP4 127.0.0.1",
          "t=0 0",
          "m=audio 40000 RTP/AVP 0 101",
          "a=rtpmap:0 PCMU/8000",
          "a=rtpmap:101 telephone-event/8000",
        ].join("\r\n");
        const invite = sipMessage(`INVITE`, `sip:${target}@${domain}`, {
          Via: `SIP/2.0/WS webclient;branch=${softphone.branch()};rport`,
          "Max-Forwards": "70",
          To: `<sip:${target}@${domain}>`,
          From: `<sip:${ext}@${domain}>;tag=${softphone.fromTag}`,
          "Call-ID": softphone.callId,
          CSeq: `${softphone.nextCseq()} INVITE`,
          Contact: `<sip:${ext}@webclient;transport=ws>`,
          "Content-Type": "application/sdp",
          "User-Agent": "SMURF-WebPhone/1.0",
        }, body);
        softphone.ws.send(invite);
        softphoneLog("INVITE SIP enviado.");
      }
    } catch (err) {
      softphoneLog(`Error al originar llamada: ${err.message}`);
    }
  });

  hangupBtn.addEventListener("click", () => {
    const ext = byId("sp-ext").value.trim();
    const domain = byId("sp-domain").value.trim();
    if (!softphone.ws || softphone.ws.readyState !== WebSocket.OPEN || !softphone.callId) {
      softphoneLog("No hay llamada activa para colgar.");
      return;
    }
    const bye = sipMessage("BYE", `sip:${domain}`, {
      Via: `SIP/2.0/WS webclient;branch=${softphone.branch()};rport`,
      "Max-Forwards": "70",
      To: `<sip:${domain}>`,
      From: `<sip:${ext}@${domain}>;tag=${softphone.fromTag}`,
      "Call-ID": softphone.callId,
      CSeq: `${softphone.nextCseq()} BYE`,
      Contact: `<sip:${ext}@webclient;transport=ws>`,
      "User-Agent": "SMURF-WebPhone/1.0",
    });
    softphone.ws.send(bye);
    softphoneLog("BYE enviado.");
  });

  byId("sp-register").textContent = "Conectar SIP WS";
  byId("sp-call").textContent = "Llamar";
  byId("sp-hangup").textContent = "Colgar";
}

function setupPwa() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}

function setupEvents() {
  byId("btn-login").addEventListener("click", login);
  byId("btn-refresh").addEventListener("click", refreshAll);
  byId("btn-create-ext").addEventListener("click", createExtension);
  byId("btn-chat-send").addEventListener("click", sendChat);
  byId("btn-presence").addEventListener("click", setPresence);
  byId("btn-export-csv").addEventListener("click", exportCsv);
  byId("btn-export-xlsx").addEventListener("click", exportExcel);
  byId("btn-logout").addEventListener("click", () => {
    localStorage.removeItem("smurf_token");
    api.token = null;
    byId("auth-panel").classList.remove("hidden");
    byId("dashboard-panel").classList.add("hidden");
    setStatus("Sesión cerrada", "info");
  });
}

function main() {
  setupEvents();
  setupSoftphoneStub();
  setupPwa();
  restoreTokenFromStorage();
}

window.addEventListener("DOMContentLoaded", main);
