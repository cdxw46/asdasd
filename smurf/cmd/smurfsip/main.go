package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/relay"
	"github.com/smurf/pbx/internal/sdp"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/wssip"
)

// SMURF SIP edge: REGISTER with digest (MD5 + SHA-256), internal INVITE B2BUA with RTP relay.

type nonceEntry struct {
	params sip.AuthParams
	until  time.Time
}

type Server struct {
	realm     string
	publicIP  string
	sipPort   int
	relayBind string
	relayCtrl string
	pool      *db.Pool
	relay     *relay.Client

	nonceMu sync.Mutex
	nonces  map[string]nonceEntry // key: nonce hex

	// call state keyed by Call-ID of inbound leg
	callMu sync.Mutex
	calls  map[string]*callBridge

	// WebSocket SIP (RFC 7118): extension -> signaling session
	wsMu       sync.RWMutex
	wsSessions map[string]*wssip.Session

	// Outbound SIP request sent over WS; match responses by Call-ID
	pendingMu     sync.Mutex
	pendingWSResp map[string]chan *sip.Message
}

type callBridge struct {
	LegACallID string // caller dialog (e.g. WebSocket client)
	LegBCallID string // toward callee (UDP/TCP leg)
	CallerWS   *wssip.Session
	CalleeWS   *wssip.Session // if callee registered over WS
	fromExt    string
	toExt      string
	cdrID      int64
	relayPorts [2]int // A,B
	webrtcCleanup func()

	callerReg *db.Registration // cached for BYE from callee toward UDP caller
}

func main() {
	realm := flag.String("realm", getenv("SMURF_REALM", "smurf.local"), "SIP authentication realm")
	publicIP := flag.String("public-ip", getenv("SMURF_PUBLIC_IP", "127.0.0.1"), "IP advertised in SDP / Contact")
	relayBind := flag.String("relay-bind", getenv("SMURF_RELAY_BIND", "127.0.0.1"), "IP smurfrelay binds for RTP (must be reachable by phones)")
	relayCtrl := flag.String("relay-control", getenv("SMURF_RELAY_CONTROL", "127.0.0.1:19000"), "smurfrelay TCP control")
	dsn := flag.String("db", getenv("SMURF_DATABASE_URL", "postgres://smurf:smurf@127.0.0.1:5432/smurf?sslmode=disable"), "PostgreSQL DSN")
	udpAddr := flag.String("udp", getenv("SMURF_SIP_UDP", "0.0.0.0:5060"), "SIP UDP listen")
	tcpAddr := flag.String("tcp", getenv("SMURF_SIP_TCP", "0.0.0.0:5060"), "SIP TCP listen")
	tlsAddr := flag.String("tls", getenv("SMURF_SIP_TLS", ""), "SIP TLS listen (empty to disable), e.g. 0.0.0.0:5061")
	wssAddr := flag.String("wss", getenv("SMURF_SIP_WSS", "0.0.0.0:5081"), "SIP over WebSocket TLS listen (empty to disable)")
	certFile := flag.String("tls-cert", getenv("SMURF_TLS_CERT", "/etc/smurf/tls.crt"), "TLS certificate")
	keyFile := flag.String("tls-key", getenv("SMURF_TLS_KEY", "/etc/smurf/tls.key"), "TLS private key")
	flag.Parse()

	ctx := context.Background()
	pool, err := db.Connect(ctx, *dsn)
	if err != nil {
		log.Fatalf("db: %v", err)
	}
	defer pool.Close()

	sipPort := 5060
	if _, p, err := net.SplitHostPort(*udpAddr); err == nil {
		if n, err := strconv.Atoi(p); err == nil {
			sipPort = n
		}
	}

	s := &Server{
		realm:           *realm,
		publicIP:        *publicIP,
		sipPort:         sipPort,
		relayBind:       *relayBind,
		relayCtrl:       *relayCtrl,
		pool:            pool,
		relay:           relay.New(*relayCtrl),
		nonces:          map[string]nonceEntry{},
		calls:           map[string]*callBridge{},
		wsSessions:      map[string]*wssip.Session{},
		pendingWSResp:   map[string]chan *sip.Message{},
	}

	pc, err := net.ListenPacket("udp", *udpAddr)
	if err != nil {
		log.Fatalf("udp listen: %v", err)
	}
	udp := pc.(*net.UDPConn)
	log.Printf("SIP UDP %s realm=%s public=%s relay=%s", *udpAddr, *realm, *publicIP, *relayCtrl)

	go s.serveUDP(udp)

	if *wssAddr != "" {
		go func() {
			if err := s.startWSS(*wssAddr, *certFile, *keyFile); err != nil {
				log.Fatalf("sip wss: %v", err)
			}
		}()
	}

	if *tcpAddr != "" {
		go func() {
			ln, err := net.Listen("tcp", *tcpAddr)
			if err != nil {
				log.Fatalf("tcp listen: %v", err)
			}
			log.Printf("SIP TCP %s", *tcpAddr)
			for {
				c, err := ln.Accept()
				if err != nil {
					log.Printf("tcp accept: %v", err)
					continue
				}
				go s.serveTCPConn(c)
			}
		}()
	}

	if *tlsAddr != "" {
		go func() {
			cert, err := tls.LoadX509KeyPair(*certFile, *keyFile)
			if err != nil {
				log.Fatalf("tls cert: %v", err)
			}
			cfg := &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}
			ln, err := tls.Listen("tcp", *tlsAddr, cfg)
			if err != nil {
				log.Fatalf("tls listen: %v", err)
			}
			log.Printf("SIP TLS %s", *tlsAddr)
			for {
				c, err := ln.Accept()
				if err != nil {
					log.Printf("tls accept: %v", err)
					continue
				}
				go s.serveTCPConn(c)
			}
		}()
	}

	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt)
	<-ch
	_ = udp.Close()
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func (s *Server) serveUDP(c *net.UDPConn) {
	buf := make([]byte, 65535)
	for {
		n, raddr, err := c.ReadFromUDP(buf)
		if err != nil {
			return
		}
		msg, err := sip.ParseMessage(append([]byte(nil), buf[:n]...))
		if err != nil {
			continue
		}
		resp := s.handleMessageWithWS(nil, msg, "udp", raddr.String(), raddr, c, nil)
		if resp != nil {
			_, _ = c.WriteToUDP([]byte(resp.String()), raddr)
		}
	}
}

func (s *Server) serveTCPConn(c net.Conn) {
	defer c.Close()
	buf := make([]byte, 0, 65535)
	tmp := make([]byte, 4096)
	for {
		n, err := c.Read(tmp)
		if err != nil {
			return
		}
		buf = append(buf, tmp[:n]...)
		for sipMessageComplete(buf) {
			msg, err := sip.ParseMessage(buf)
			if err != nil {
				buf = buf[:0]
				break
			}
			consumed := sipConsumed(buf)
			buf = buf[consumed:]
			raddr := c.RemoteAddr().String()
			resp := s.handleMessageWithWS(nil, msg, tcpTransport(c), raddr, nil, nil, c)
			if resp != nil {
				_, _ = io.WriteString(c, resp.String())
			}
		}
	}
}

func tcpTransport(c net.Conn) string {
	if _, ok := c.(*tls.Conn); ok {
		return "tls"
	}
	return "tcp"
}

func sipMessageComplete(buf []byte) bool {
	idx := bytes.Index(buf, []byte("\r\n\r\n"))
	if idx < 0 {
		return false
	}
	head := string(buf[:idx])
	lines := strings.Split(head, "\r\n")
	cl := 0
	found := false
	for _, ln := range lines[1:] {
		if strings.HasPrefix(strings.ToLower(ln), "content-length:") {
			v := strings.TrimSpace(ln[len("content-length:"):])
			n, err := strconv.Atoi(v)
			if err != nil {
				return false
			}
			cl = n
			found = true
			break
		}
	}
	if !found {
		return true
	}
	return len(buf) >= idx+4+cl
}

func sipConsumed(buf []byte) int {
	idx := bytes.Index(buf, []byte("\r\n\r\n"))
	if idx < 0 {
		return 0
	}
	head := string(buf[:idx])
	lines := strings.Split(head, "\r\n")
	cl := 0
	found := false
	for _, ln := range lines[1:] {
		if strings.HasPrefix(strings.ToLower(ln), "content-length:") {
			v := strings.TrimSpace(ln[len("content-length:"):])
			n, err := strconv.Atoi(v)
			if err != nil {
				return idx + 4
			}
			cl = n
			found = true
			break
		}
	}
	if !found {
		return idx + 4
	}
	return idx + 4 + cl
}

func (s *Server) handleMessageWithWS(ws *wssip.Session, m *sip.Message, transport, raddr string, udpRaddr *net.UDPAddr, udpConn *net.UDPConn, tcpConn net.Conn) *sip.Message {
	ctx := context.Background()
	switch {
	case m.IsRequest && m.Method == "REGISTER":
		return s.handleRegister(ctx, ws, m, transport, raddr, udpRaddr, udpConn, tcpConn)
	case m.IsRequest && m.Method == "INVITE":
		return s.handleInvite(ctx, ws, m, transport, raddr, udpRaddr, udpConn, tcpConn)
	case m.IsRequest && m.Method == "ACK":
		s.forwardMidDialogNoResponse(ctx, m, transport)
		return nil
	case m.IsRequest && (m.Method == "BYE" || m.Method == "CANCEL"):
		return s.handleMidDialog(ctx, m, transport, raddr, udpRaddr, udpConn, tcpConn)
	case m.IsRequest && m.Method == "OPTIONS":
		return s.okOptions(m)
	default:
		if m.IsRequest {
			return sipResponse(m, 501, "Not Implemented")
		}
		return nil
	}
}

func (s *Server) sipViaUDP() string {
	return fmt.Sprintf("SIP/2.0/UDP %s:%d;branch=%s", s.publicIP, s.sipPort, sipBranch())
}

func (s *Server) outboundContact(ext, transport string) string {
	return fmt.Sprintf("<sip:%s@%s:%d;transport=%s>", ext, s.publicIP, s.sipPort, transport)
}

func isWebRTCJSONInvite(m *sip.Message) bool {
	ct := strings.ToLower(m.Headers.Get("content-type"))
	return strings.Contains(ct, "json")
}

func (s *Server) okOptions(m *sip.Message) *sip.Message {
	resp := sipResponse(m, 200, "OK")
	resp.AddHeader("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER")
	resp.AddHeader("Accept", "application/sdp")
	resp.AddHeader("Supported", "100rel, timer")
	resp.AddHeader("Content-Length", "0")
	return resp
}

func sipResponse(req *sip.Message, code int, reason string) *sip.Message {
	resp := &sip.Message{
		StartLine: sip.StartLine{IsRequest: false, Proto: "SIP/2.0", StatusCode: code, Reason: reason},
		Headers:   sip.HeaderMap{},
	}
	if via := req.Headers.All("via"); len(via) > 0 {
		for _, v := range via {
			resp.AddHeader("Via", v)
		}
	}
	if f := req.Headers.Get("from"); f != "" {
		resp.AddHeader("From", f)
	}
	if t := req.Headers.Get("to"); t != "" {
		resp.AddHeader("To", t)
	}
	if c := req.Headers.Get("call-id"); c != "" {
		resp.AddHeader("Call-ID", c)
	}
	if cs := req.Headers.Get("cseq"); cs != "" {
		resp.AddHeader("CSeq", cs)
	}
	return resp
}

func (s *Server) handleRegister(ctx context.Context, ws *wssip.Session, m *sip.Message, transport, raddr string, udpRaddr *net.UDPAddr, udpConn *net.UDPConn, tcpConn net.Conn) *sip.Message {
	ext := sipParseUser(m.Headers.Get("to"))
	if ext == "" {
		return sipResponse(m, 400, "Bad Request")
	}
	e, err := s.pool.GetExtension(ctx, ext)
	if err != nil {
		return sipResponse(m, 403, "Forbidden")
	}

	authz := m.Headers.Get("authorization")
	if authz == "" {
		return s.challenge(m, ext)
	}
	creds := sip.ParseDigestHeader(authz)
	username := creds["username"]
	if username == "" || username != ext {
		return s.challenge(m, ext)
	}
	realm := creds["realm"]
	if realm != s.realm {
		return s.challenge(m, ext)
	}
	nonce := creds["nonce"]
	s.nonceMu.Lock()
	entry, ok := s.nonces[nonce]
	s.nonceMu.Unlock()
	if !ok || time.Now().After(entry.until) {
		return s.challenge(m, ext)
	}
	digestURI := m.RequestURI
	if !verifyAuth(m.Method, digestURI, string(m.Body), entry.params, creds, username, e.Secret) {
		return s.challenge(m, ext)
	}

	contact := m.Headers.Get("contact")
	if strings.TrimSpace(contact) == "*" {
		_ = s.pool.DeleteRegistration(ctx, ext)
		resp := sipResponse(m, 200, "OK")
		resp.AddHeader("Expires", "0")
		resp.AddHeader("Content-Length", "0")
		return resp
	}
	exp := parseExpires(contact, m.Headers.Get("expires"))
	if exp <= 0 {
		return sipResponse(m, 400, "Bad Expires")
	}
	host, port := hostPortFromAddr(raddr)
	regTransport := transport
	regHost := host
	regPort := port
	regContact := contactURI(contact)
	if ws != nil {
		regTransport = "ws"
		regHost = "0.0.0.0"
		regPort = 0
		regContact = "sip:" + ext + "@wss.invalid;transport=ws"
	}
	reg := db.Registration{
		Extension:  ext,
		AOR:        "sip:" + ext + "@" + s.realm,
		ContactURI: regContact,
		RemoteIP:   regHost,
		RemotePort: regPort,
		Transport:  regTransport,
		ExpiresAt:  time.Now().Add(time.Duration(exp) * time.Second),
		CallID:     m.Headers.Get("call-id"),
		UserAgent:  m.Headers.Get("user-agent"),
	}
	if err := s.pool.UpsertRegistration(ctx, reg); err != nil {
		log.Printf("register db: %v", err)
		return sipResponse(m, 500, "Server Error")
	}
	resp := sipResponse(m, 200, "OK")
	resp.AddHeader("Expires", strconv.Itoa(exp))
	resp.AddHeader("Content-Length", "0")
	if ws != nil {
		ws.Ext = ext
		s.wsMu.Lock()
		s.wsSessions[ext] = ws
		s.wsMu.Unlock()
	}
	return resp
}

func verifyAuth(method, digestURI, body string, challenge, creds sip.AuthParams, user, pass string) bool {
	return sip.VerifyDigestResponse(method, digestURI, body, challenge, creds, user, pass)
}

func (s *Server) challenge(m *sip.Message, ext string) *sip.Message {
	nonce := sip.RandomNonce()
	params := sip.AuthParams{
		"realm":     s.realm,
		"nonce":     nonce,
		"algorithm": "SHA-256",
		"qop":       "auth",
		"opaque":    sip.RandomNonce()[:16],
	}
	s.nonceMu.Lock()
	s.nonces[nonce] = nonceEntry{params: params, until: time.Now().Add(10 * time.Minute)}
	s.nonceMu.Unlock()
	resp := sipResponse(m, 401, "Unauthorized")
	www := fmt.Sprintf(`Digest realm="%s", nonce="%s", algorithm=SHA-256, qop="auth", opaque="%s"`,
		s.realm, nonce, params["opaque"])
	resp.AddHeader("WWW-Authenticate", www)
	resp.AddHeader("Content-Length", "0")
	_ = ext
	return resp
}

func (s *Server) handleInvite(ctx context.Context, ws *wssip.Session, m *sip.Message, transport, raddr string, udpRaddr *net.UDPAddr, udpConn *net.UDPConn, tcpConn net.Conn) *sip.Message {
	from := sipParseUser(m.Headers.Get("from"))
	to := sipParseUser(m.Headers.Get("to"))
	if from == "" || to == "" {
		return sipResponse(m, 400, "Bad Request")
	}
	authz := m.Headers.Get("proxy-authorization")
	if authz == "" {
		authz = m.Headers.Get("authorization")
	}
	if authz == "" {
		return s.proxyChallenge(m)
	}
	creds := sip.ParseDigestHeader(authz)
	nonce := creds["nonce"]
	s.nonceMu.Lock()
	entry, ok := s.nonces[nonce]
	s.nonceMu.Unlock()
	if !ok || time.Now().After(entry.until) {
		return s.proxyChallenge(m)
	}
	caller, err := s.pool.GetExtension(ctx, from)
	if err != nil {
		return sipResponse(m, 403, "Forbidden")
	}
	if !verifyAuth(m.Method, m.RequestURI, string(m.Body), entry.params, creds, from, caller.Secret) {
		return s.proxyChallenge(m)
	}
	if _, err := s.pool.GetExtension(ctx, to); err != nil {
		return sipResponse(m, 404, "Not Found")
	}
	reg, err := s.pool.GetRegistration(ctx, to)
	if err != nil {
		return sipResponse(m, 480, "Temporarily Unavailable")
	}
	var callerReg *db.Registration
	if cr, err := s.pool.GetRegistration(ctx, from); err == nil {
		callerReg = cr
	}

	if ws != nil {
		s.wsMu.Lock()
		s.wsSessions[from] = ws
		s.wsMu.Unlock()
	}

	callID := m.Headers.Get("call-id")
	if ws != nil && isWebRTCJSONInvite(m) {
		return s.handleInviteWebRTC(ctx, m, ws, from, to, callID)
	}

	rtpA, rtpB, err := s.relay.OpenSession(callID)
	if err != nil {
		log.Printf("relay: %v", err)
		return sipResponse(m, 500, "Relay Error")
	}
	cdrID, err := s.pool.InsertCDR(ctx, callID, from, to, "internal")
	if err != nil {
		log.Printf("cdr: %v", err)
	}

	br := &callBridge{
		LegACallID: callID, LegBCallID: callID + "-b", CallerWS: ws, CalleeWS: calleeWSSession(s, reg),
		fromExt: from, toExt: to, cdrID: cdrID, relayPorts: [2]int{rtpA, rtpB},
		callerReg: callerReg,
	}
	s.callMu.Lock()
	s.calls[callID] = br
	s.calls[callID+"-b"] = br
	s.callMu.Unlock()

	// Patch SDP for callee offer: same codecs, our public IP, relay leg B
	offerToCallee := sdp.PatchMediaEndpoint(string(m.Body), s.publicIP, rtpB)

	out := cloneRequest(m)
	out.Method = "INVITE"
	out.RequestURI = reg.ContactURI
	out.Body = []byte(offerToCallee)
	out.Headers = sip.HeaderMap{}
	out.AddHeader("Via", s.sipViaUDP())
	out.AddHeader("Max-Forwards", "70")
	out.AddHeader("From", m.Headers.Get("from"))
	out.AddHeader("To", fmt.Sprintf("<sip:%s@%s>", to, s.realm))
	out.AddHeader("Call-ID", callID+"-b") // separate dialog towards callee
	out.AddHeader("CSeq", nextCSeq(m.Headers.Get("cseq"), "INVITE"))
	out.AddHeader("Contact", s.outboundContact(from, transport))
	out.AddHeader("Content-Type", "application/sdp")
	out.AddHeader("Content-Length", strconv.Itoa(len(out.Body)))

	respB, err := s.sendRequestToUA(ctx, reg, out)
	if err != nil || respB == nil {
		s.tearDownCall(br, callID)
		_ = s.pool.UpdateCDREnded(ctx, cdrID, "relay-fail")
		return sipResponse(m, 504, "Server Timeout")
	}
	if respB.StatusCode >= 300 {
		s.tearDownCall(br, callID)
		_ = s.pool.UpdateCDREnded(ctx, cdrID, fmt.Sprintf("sip-%d", respB.StatusCode))
		return sipResponse(m, respB.StatusCode, respB.Reason)
	}
	if respB.StatusCode == 200 {
		_ = s.pool.UpdateCDRAnswered(ctx, cdrID)
		ack := ackToCallee(s, reg, m, respB, callID)
		_ = s.sendRequestFireAndForget(reg, ack)
	}
	answerToCaller := sdp.PatchMediaEndpoint(string(respB.Body), s.publicIP, rtpA)

	resp := sipResponse(m, respB.StatusCode, respB.Reason)
	if t := respB.Headers.Get("to"); t != "" {
		resp.Headers.Set("to", t)
	}
	resp.Body = []byte(answerToCaller)
	resp.AddHeader("Content-Type", "application/sdp")
	resp.AddHeader("Content-Length", strconv.Itoa(len(resp.Body)))
	return resp
}

func (s *Server) proxyChallenge(m *sip.Message) *sip.Message {
	nonce := sip.RandomNonce()
	params := sip.AuthParams{
		"realm":     s.realm,
		"nonce":     nonce,
		"algorithm": "SHA-256",
		"qop":       "auth",
		"opaque":    sip.RandomNonce()[:16],
	}
	s.nonceMu.Lock()
	s.nonces[nonce] = nonceEntry{params: params, until: time.Now().Add(10 * time.Minute)}
	s.nonceMu.Unlock()
	resp := sipResponse(m, 407, "Proxy Authentication Required")
	pa := fmt.Sprintf(`Digest realm="%s", nonce="%s", algorithm=SHA-256, qop="auth", opaque="%s"`,
		s.realm, nonce, params["opaque"])
	resp.AddHeader("Proxy-Authenticate", pa)
	resp.AddHeader("Content-Length", "0")
	return resp
}

func cloneRequest(m *sip.Message) *sip.Message {
	return &sip.Message{StartLine: m.StartLine, Headers: sip.HeaderMap{}, Body: nil}
}

func sipBranch() string {
	return "z9hG4bK" + sip.RandomNonce()[:16]
}

func nextCSeq(cseq, method string) string {
	parts := strings.Fields(cseq)
	if len(parts) < 2 {
		return "1 INVITE"
	}
	n, err := strconv.Atoi(parts[0])
	if err != nil {
		return "1 " + method
	}
	return fmt.Sprintf("%d %s", n, method)
}

func sipParseUser(h string) string {
	// From: "Bob" <sip:1000@x>;tag=...
	i := strings.Index(strings.ToLower(h), "sip:")
	if i < 0 {
		return ""
	}
	rest := h[i+4:]
	end := strings.IndexAny(rest, "@>;")
	if end <= 0 {
		return ""
	}
	return rest[:end]
}

func hostPortFromAddr(raddr string) (host string, port int) {
	h, p, err := net.SplitHostPort(raddr)
	if err != nil {
		return raddr, 5060
	}
	port, _ = strconv.Atoi(p)
	return h, port
}

func parseExpires(contact, expHeader string) int {
	if expHeader != "" {
		if n, err := strconv.Atoi(strings.TrimSpace(expHeader)); err == nil {
			return n
		}
	}
	// Contact: <...>;expires=3600
	l := strings.ToLower(contact)
	if idx := strings.Index(l, "expires="); idx >= 0 {
		rest := contact[idx+len("expires="):]
		rest = strings.TrimSpace(rest)
		end := strings.IndexAny(rest, "; \t")
		if end < 0 {
			end = len(rest)
		}
		n, err := strconv.Atoi(rest[:end])
		if err == nil {
			return n
		}
	}
	return 3600
}

func contactURI(contact string) string {
	// take first URI in angle brackets
	a := strings.Index(contact, "<")
	b := strings.Index(contact, ">")
	if a >= 0 && b > a {
		return strings.TrimSpace(contact[a+1 : b])
	}
	return strings.Fields(contact)[0]
}
