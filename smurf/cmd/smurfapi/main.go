package main

import (
	"context"
	"crypto/tls"
	_ "embed"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/smurf/pbx/internal/db"
	"golang.org/x/crypto/bcrypt"
)

// SMURF management API + minimal admin SPA on HTTPS :5001

//go:embed softphone.html
var softphoneHTML []byte

type claims struct {
	User string `json:"u"`
	Role string `json:"r"`
	jwt.RegisteredClaims
}

func main() {
	listen := flag.String("listen", getenv("SMURF_API_LISTEN", "0.0.0.0:5001"), "HTTPS listen address")
	dsn := flag.String("db", getenv("SMURF_DATABASE_URL", "postgres://smurf:smurf@127.0.0.1:5432/smurf?sslmode=disable"), "PostgreSQL DSN")
	cert := flag.String("tls-cert", getenv("SMURF_TLS_CERT", "/etc/smurf/tls.crt"), "TLS certificate")
	key := flag.String("tls-key", getenv("SMURF_TLS_KEY", "/etc/smurf/tls.key"), "TLS private key")
	jwtSecret := flag.String("jwt-secret", getenv("SMURF_JWT_SECRET", "change-me-in-production"), "JWT signing secret")
	flag.Parse()

	ctx := context.Background()
	pool, err := db.Connect(ctx, *dsn)
	if err != nil {
		log.Fatalf("db: %v", err)
	}
	defer pool.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(openAPISpec()))
	})
	mux.HandleFunc("/api/v1/auth/login", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		var body struct {
			Username string `json:"username"`
			Password string `json:"password"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
		u, err := pool.GetAdminByUsername(ctx, body.Username)
		if err != nil {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if bcrypt.CompareHashAndPassword([]byte(u.PasswordHash), []byte(body.Password)) != nil {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		tok, err := issueToken(*jwtSecret, u.Username, u.Role)
		if err != nil {
			http.Error(w, "token", http.StatusInternalServerError)
			return
		}
		writeJSON(w, map[string]any{"access_token": tok, "token_type": "Bearer", "expires_in": 86400})
	})
	mux.HandleFunc("/api/v1/extensions", func(w http.ResponseWriter, r *http.Request) {
		if !authBearer(w, r, *jwtSecret) {
			return
		}
		switch r.Method {
		case http.MethodGet:
			list, err := pool.ListExtensionsPublic(ctx)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			type row struct {
				Number        string `json:"number"`
				DisplayName   string `json:"display_name"`
				MaxConcurrent int    `json:"max_concurrent"`
			}
			out := make([]row, 0, len(list))
			for _, e := range list {
				out = append(out, row{Number: e.Number, DisplayName: e.DisplayName, MaxConcurrent: e.MaxConcurrent})
			}
			writeJSON(w, map[string]any{"extensions": out})
		case http.MethodPost:
			var body struct {
				Number        string `json:"number"`
				Secret        string `json:"secret"`
				DisplayName   string `json:"display_name"`
				MaxConcurrent int    `json:"max_concurrent"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, "bad json", http.StatusBadRequest)
				return
			}
			if body.Number == "" || body.Secret == "" {
				http.Error(w, "number and secret required", http.StatusBadRequest)
				return
			}
			if body.MaxConcurrent <= 0 {
				body.MaxConcurrent = 4
			}
			if err := pool.InsertExtension(ctx, db.Extension{
				Number:        strings.TrimSpace(body.Number),
				Secret:        body.Secret,
				DisplayName:   body.DisplayName,
				MaxConcurrent: body.MaxConcurrent,
			}); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			w.WriteHeader(http.StatusCreated)
			writeJSON(w, map[string]string{"status": "created"})
		default:
			http.Error(w, "method", http.StatusMethodNotAllowed)
		}
	})
	mux.HandleFunc("/api/v1/webhooks", func(w http.ResponseWriter, r *http.Request) {
		if !authBearer(w, r, *jwtSecret) {
			return
		}
		switch r.Method {
		case http.MethodGet:
			list, err := pool.ListWebhooks(ctx)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			type row struct {
				ID      int64    `json:"id"`
				URL     string   `json:"url"`
				Events  []string `json:"events"`
				Enabled bool     `json:"enabled"`
			}
			out := make([]row, 0, len(list))
			for _, h := range list {
				out = append(out, row{ID: h.ID, URL: h.URL, Events: h.Events, Enabled: h.Enabled})
			}
			writeJSON(w, map[string]any{"webhooks": out})
		case http.MethodPost:
			var body struct {
				URL    string   `json:"url"`
				Secret string   `json:"secret"`
				Events []string `json:"events"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, "bad json", http.StatusBadRequest)
				return
			}
			if body.URL == "" {
				http.Error(w, "url required", http.StatusBadRequest)
				return
			}
			id, err := pool.InsertWebhook(ctx, body.URL, body.Secret, body.Events)
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			w.WriteHeader(http.StatusCreated)
			writeJSON(w, map[string]any{"id": id})
		default:
			http.Error(w, "method", http.StatusMethodNotAllowed)
		}
	})
	mux.HandleFunc("/api/v1/sip-trunks", func(w http.ResponseWriter, r *http.Request) {
		if !authBearer(w, r, *jwtSecret) {
			return
		}
		switch r.Method {
		case http.MethodGet:
			list, err := pool.ListSIPTrunks(ctx)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			type row struct {
				ID        int64  `json:"id"`
				Name      string `json:"name"`
				SipHost   string `json:"sip_host"`
				SipPort   int    `json:"sip_port"`
				Transport string `json:"transport"`
				Priority  int    `json:"priority"`
				Enabled   bool   `json:"enabled"`
			}
			out := make([]row, 0, len(list))
			for _, t := range list {
				out = append(out, row{ID: t.ID, Name: t.Name, SipHost: t.SipHost, SipPort: t.SipPort, Transport: t.Transport, Priority: t.Priority, Enabled: t.Enabled})
			}
			writeJSON(w, map[string]any{"trunks": out})
		case http.MethodPost:
			var body struct {
				Name         string `json:"name"`
				SipHost      string `json:"sip_host"`
				SipPort      int    `json:"sip_port"`
				Transport    string `json:"transport"`
				AuthUsername string `json:"auth_username"`
				AuthPassword string `json:"auth_password"`
				FromUser     string `json:"from_user"`
				RegisterURI  string `json:"register_uri"`
				ContactUser  string `json:"contact_user"`
				Priority     int    `json:"priority"`
				Enabled      *bool  `json:"enabled"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, "bad json", http.StatusBadRequest)
				return
			}
			if body.Name == "" || body.SipHost == "" {
				http.Error(w, "name and sip_host required", http.StatusBadRequest)
				return
			}
			if body.SipPort == 0 {
				body.SipPort = 5060
			}
			if body.Transport == "" {
				body.Transport = "udp"
			}
			en := true
			if body.Enabled != nil {
				en = *body.Enabled
			}
			id, err := pool.InsertSIPTrunk(ctx, db.SIPTrunk{
				Name: body.Name, SipHost: body.SipHost, SipPort: body.SipPort, Transport: body.Transport,
				AuthUsername: body.AuthUsername, AuthPassword: body.AuthPassword, FromUser: body.FromUser,
				RegisterURI: body.RegisterURI, ContactUser: body.ContactUser, Priority: body.Priority, Enabled: en,
			})
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			w.WriteHeader(http.StatusCreated)
			writeJSON(w, map[string]any{"id": id})
		default:
			http.Error(w, "method", http.StatusMethodNotAllowed)
		}
	})
	mux.HandleFunc("/api/v1/sip-trunks/", func(w http.ResponseWriter, r *http.Request) {
		if !authBearer(w, r, *jwtSecret) {
			return
		}
		if r.Method != http.MethodDelete {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		idStr := strings.TrimPrefix(r.URL.Path, "/api/v1/sip-trunks/")
		id, err := strconv.ParseInt(idStr, 10, 64)
		if err != nil || id <= 0 {
			http.NotFound(w, r)
			return
		}
		if err := pool.DeleteSIPTrunk(ctx, id); err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/api/v1/voicemail/", func(w http.ResponseWriter, r *http.Request) {
		if !authBearer(w, r, *jwtSecret) {
			return
		}
		rest := strings.TrimPrefix(r.URL.Path, "/api/v1/voicemail/")
		parts := strings.Split(rest, "/")
		if len(parts) < 1 || parts[0] == "" {
			http.NotFound(w, r)
			return
		}
		mailbox := parts[0]
		if r.Method == http.MethodGet && (len(parts) == 1 || parts[1] == "") {
			list, err := pool.ListVoicemailMessages(ctx, mailbox)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			writeJSON(w, map[string]any{"messages": list})
			return
		}
		if len(parts) >= 2 && parts[1] == "download" {
			id, err := strconv.ParseInt(parts[2], 10, 64)
			if err != nil || id <= 0 {
				http.NotFound(w, r)
				return
			}
			path, err := pool.GetVoicemailPath(ctx, id, mailbox)
			if err != nil {
				http.Error(w, "not found", http.StatusNotFound)
				return
			}
			w.Header().Set("Content-Type", "audio/wav")
			http.ServeFile(w, r, path)
			return
		}
		http.Error(w, "method", http.StatusMethodNotAllowed)
	})
	mux.HandleFunc("/api/v1/webhooks/", func(w http.ResponseWriter, r *http.Request) {
		if !authBearer(w, r, *jwtSecret) {
			return
		}
		if r.Method != http.MethodDelete {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		idStr := strings.TrimPrefix(r.URL.Path, "/api/v1/webhooks/")
		id, err := strconv.ParseInt(idStr, 10, 64)
		if err != nil || id <= 0 {
			http.NotFound(w, r)
			return
		}
		if err := pool.DeleteWebhook(ctx, id); err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/api/v1/extensions/", func(w http.ResponseWriter, r *http.Request) {
		if !authBearer(w, r, *jwtSecret) {
			return
		}
		num := strings.TrimPrefix(r.URL.Path, "/api/v1/extensions/")
		if num == "" {
			http.NotFound(w, r)
			return
		}
		if r.Method != http.MethodDelete {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		if err := pool.DeleteExtension(ctx, num); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/softphone", softphonePage)
	mux.HandleFunc("/", adminSPA)

	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	srv := &http.Server{
		Addr:         *listen,
		Handler:      withCORS(mux),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 120 * time.Second,
		TLSConfig:    cfg,
	}
	log.Printf("smurfapi https %s", *listen)
	log.Fatal(srv.ListenAndServeTLS(*cert, *key))
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(true)
	_ = enc.Encode(v)
}

func issueToken(secret, user, role string) (string, error) {
	now := time.Now()
	cl := claims{
		User: user,
		Role: role,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(now.Add(24 * time.Hour)),
			IssuedAt:  jwt.NewNumericDate(now),
			Issuer:    "smurf",
		},
	}
	t := jwt.NewWithClaims(jwt.SigningMethodHS256, cl)
	return t.SignedString([]byte(secret))
}

func authBearer(w http.ResponseWriter, r *http.Request, secret string) bool {
	h := r.Header.Get("Authorization")
	const p = "Bearer "
	if !strings.HasPrefix(h, p) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return false
	}
	raw := strings.TrimSpace(h[len(p):])
	tok, err := jwt.ParseWithClaims(raw, &claims{}, func(t *jwt.Token) (any, error) {
		if t.Method != jwt.SigningMethodHS256 {
			return nil, fmt.Errorf("alg")
		}
		return []byte(secret), nil
	})
	if err != nil || !tok.Valid {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return false
	}
	return true
}

func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func softphonePage(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/softphone" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(softphoneHTML)
}

func adminSPA(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(adminHTML))
}

const adminHTML = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SMURF Admin</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem;background:#0f172a;color:#e2e8f0}
h1{font-size:1.25rem}
.card{background:#1e293b;border-radius:8px;padding:1rem;max-width:720px}
label{display:block;margin:.5rem 0 .2rem;color:#94a3b8;font-size:.85rem}
input,button{width:100%;padding:.5rem;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0}
button{background:#2563eb;border:none;cursor:pointer;margin-top:.6rem}
button.secondary{background:#475569}
table{width:100%;border-collapse:collapse;margin-top:1rem;font-size:.9rem}
th,td{padding:.4rem;border-bottom:1px solid #334155;text-align:left}
.err{color:#f87171;margin-top:.5rem;font-size:.9rem}
small{color:#64748b}
</style></head><body>
<h1>SMURF — administration</h1>
<p><small>API: <code>/api/v1</code> · OpenAPI: <code>/openapi.json</code> · WebRTC softphone: <a href="/softphone" style="color:#93c5fd">/softphone</a></small></p>
<div class="card" id="loginBox">
<h2>Sign in</h2>
<label>Username</label><input id="user" value="admin"/>
<label>Password</label><input id="pass" type="password" value="smurfadmin"/>
<button id="btnLogin">Login</button>
<p class="err" id="err"></p>
</div>
<div class="card" id="mainBox" style="display:none">
<p>Logged in as <strong id="who"></strong></p>
<button class="secondary" id="btnOut">Logout</button>
<h2>Extensions</h2>
<table><thead><tr><th>Ext</th><th>Name</th><th>Max calls</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<h3 style="margin-top:1.2rem">New extension</h3>
<label>Number</label><input id="nNum"/>
<label>Secret</label><input id="nSec"/>
<label>Display name</label><input id="nDisp"/>
<button id="btnAdd">Create</button>
<h2 style="margin-top:1.2rem">Webhooks</h2>
<table><thead><tr><th>ID</th><th>URL</th><th>Events</th><th></th></tr></thead><tbody id="whRows"></tbody></table>
<h3 style="margin-top:1rem">New webhook</h3>
<label>URL</label><input id="whUrl" placeholder="https://example.com/hook"/>
<label>Secret (HMAC)</label><input id="whSec"/>
<label>Events (comma)</label><input id="whEv" value="call.answered,call.ended"/>
<button id="btnWh">Add webhook</button>
</div>
<script>
let token=null;
const $=s=>document.querySelector(s);
async function api(path,opts={}){
  const h=opts.headers||{};
  if(token) h['Authorization']='Bearer '+token;
  const r=await fetch(path,{...opts,headers:h});
  if(!r.ok) throw new Error(await r.text());
  if(r.status===204) return null;
  return r.json();
}
$('#btnLogin').onclick=async()=>{
  $('#err').textContent='';
  try{
    const j=await api('/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:$('#user').value,password:$('#pass').value})});
    token=j.access_token;
    $('#loginBox').style.display='none';
    $('#mainBox').style.display='block';
    $('#who').textContent=$('#user').value;
    await refresh();
    await refreshWh();
  }catch(e){$('#err').textContent=e.message||String(e)}
};
$('#btnOut').onclick=()=>{token=null;location.reload()};
async function refreshWh(){
  const j=await api('/api/v1/webhooks');
  const tb=$('#whRows');tb.innerHTML='';
  for(const w of j.webhooks){
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+w.id+'</td><td style="word-break:break-all">'+w.url+'</td><td>'+(w.events||[]).join(', ')+'</td><td><button data-w="'+w.id+'">Delete</button></td>';
    tb.appendChild(tr);
  }
  tb.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
    if(!confirm('Delete webhook '+b.dataset.w+'?'))return;
    await fetch('/api/v1/webhooks/'+b.dataset.w,{method:'DELETE',headers:{Authorization:'Bearer '+token}});
    await refreshWh();
  });
}
async function refresh(){
  const j=await api('/api/v1/extensions');
  const tb=$('#rows');tb.innerHTML='';
  for(const e of j.extensions){
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+e.number+'</td><td>'+(e.display_name||'')+'</td><td>'+e.max_concurrent+'</td><td><button data-n="'+e.number+'">Delete</button></td>';
    tb.appendChild(tr);
  }
  tb.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
    if(!confirm('Delete '+b.dataset.n+'?'))return;
    await fetch('/api/v1/extensions/'+b.dataset.n,{method:'DELETE',headers:{Authorization:'Bearer '+token}});
    await refresh();
  });
}
$('#btnAdd').onclick=async()=>{
  await api('/api/v1/extensions',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({number:$('#nNum').value,secret:$('#nSec').value,display_name:$('#nDisp').value})});
  $('#nNum').value='';$('#nSec').value='';$('#nDisp').value='';
  await refresh();
};
$('#btnWh').onclick=async()=>{
  const ev=$('#whEv').value.split(',').map(s=>s.trim()).filter(Boolean);
  await api('/api/v1/webhooks',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:$('#whUrl').value,secret:$('#whSec').value,events:ev})});
  $('#whUrl').value='';$('#whSec').value='';
  await refreshWh();
};
</script></body></html>`

func openAPISpec() string {
	return `{"openapi":"3.0.3","info":{"title":"SMURF API","version":"0.1.0"},
"servers":[{"url":"/"}],
"paths":{
"/api/v1/auth/login":{"post":{"summary":"Admin login","requestBody":{"required":true,"content":{"application/json":{"schema":{"type":"object","properties":{"username":{"type":"string"},"password":{"type":"string"}}}}}},
"responses":{"200":{"description":"JWT"}}}},
"/api/v1/extensions":{"get":{"security":[{"bearerAuth":[]}],"responses":{"200":{"description":"list"}}},
"post":{"security":[{"bearerAuth":[]}],"responses":{"201":{"description":"created"}}}},
"/api/v1/extensions/{number}":{"delete":{"security":[{"bearerAuth":[]}],"parameters":[{"name":"number","in":"path","required":true,"schema":{"type":"string"}}],"responses":{"204":{"description":"deleted"}}}},
"/api/v1/webhooks":{"get":{"security":[{"bearerAuth":[]}],"responses":{"200":{"description":"list"}}},"post":{"security":[{"bearerAuth":[]}],"responses":{"201":{"description":"created"}}}},
"/api/v1/webhooks/{id}":{"delete":{"security":[{"bearerAuth":[]}],"parameters":[{"name":"id","in":"path","required":true,"schema":{"type":"integer"}}],"responses":{"204":{"description":"deleted"}}}},
"/api/v1/sip-trunks":{"get":{"security":[{"bearerAuth":[]}],"responses":{"200":{"description":"list"}}},"post":{"security":[{"bearerAuth":[]}],"responses":{"201":{"description":"created"}}}},
"/api/v1/sip-trunks/{id}":{"delete":{"security":[{"bearerAuth":[]}],"parameters":[{"name":"id","in":"path","required":true,"schema":{"type":"integer"}}],"responses":{"204":{"description":"deleted"}}}},
"/api/v1/voicemail/{mailbox}":{"get":{"security":[{"bearerAuth":[]}],"parameters":[{"name":"mailbox","in":"path","required":true,"schema":{"type":"string"}}],"responses":{"200":{"description":"messages"}}}}
},
"components":{"securitySchemes":{"bearerAuth":{"type":"http","scheme":"bearer","bearerFormat":"JWT"}}}}`
}
