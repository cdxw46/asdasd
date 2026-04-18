package sip

import (
	"bufio"
	"context"
	"crypto/md5"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/textproto"
	"strings"
	"sync"
	"time"

	"smurf/internal/auth"
	"smurf/internal/config"
	"smurf/internal/db"
	"smurf/internal/pbx"
	"smurf/internal/util"
)

type Sender func(msg *Message) error

type RequestContext struct {
	Transport string
	Remote    net.Addr
	LocalAddr string
	Send      Sender
}

type inviteDialog struct {
	CallID    string
	Caller    string
	Callee    string
	CallerCtx *RequestContext
	CalleeCtx *RequestContext
	InviteReq *Message
}

type Server struct {
	cfg       *config.Config
	store     *db.Store
	logger    *util.Logger
	pbx       *pbx.Engine
	nonceMu   sync.Mutex
	nonces    map[string]time.Time
	invitesMu sync.RWMutex
	invites   map[string]*inviteDialog
}

func NewServer(cfg *config.Config, store *db.Store, logger *util.Logger, pbxEngine *pbx.Engine) *Server {
	return &Server{
		cfg:    cfg,
		store:  store,
		logger: logger,
		pbx:    pbxEngine,
		nonces: make(map[string]time.Time),
		invites: make(map[string]*inviteDialog),
	}
}

func (s *Server) Start(ctx context.Context) error {
	errCh := make(chan error, 3)
	go s.listenUDP(ctx, errCh)
	go s.listenTCP(ctx, errCh, s.cfg.SIP.TCP, nil, "TCP")
	go s.listenTLS(ctx, errCh)
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := s.store.CleanupExpiredRegistrations(context.Background()); err != nil {
				s.logger.Error("cleanup registrations failed", "error", err)
			}
		case err := <-errCh:
			return err
		}
	}
}

func (s *Server) listenUDP(ctx context.Context, errCh chan<- error) {
	pc, err := net.ListenPacket("udp", s.cfg.SIP.UDP)
	if err != nil {
		errCh <- err
		return
	}
	defer pc.Close()
	s.logger.Info("SIP UDP listening", "addr", s.cfg.SIP.UDP)
	buf := make([]byte, 65535)
	for {
		_ = pc.SetReadDeadline(time.Now().Add(2 * time.Second))
		n, addr, err := pc.ReadFrom(buf)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				select {
				case <-ctx.Done():
					return
				default:
					continue
				}
			}
			errCh <- err
			return
		}
		data := append([]byte(nil), buf[:n]...)
		go func() {
			msg, err := ParseMessage(data)
			if err != nil {
				s.logger.Warn("failed to parse SIP UDP packet", "remote", addr.String(), "error", err)
				return
			}
			reqCtx := &RequestContext{
				Transport: "UDP",
				Remote:    addr,
				LocalAddr: pc.LocalAddr().String(),
				Send: func(resp *Message) error {
					_, err := pc.WriteTo([]byte(resp.String()), addr)
					return err
				},
			}
			s.handleMessage(context.Background(), msg, reqCtx)
		}()
	}
}

func (s *Server) listenTCP(ctx context.Context, errCh chan<- error, addr string, tlsCfg *tls.Config, transport string) {
	var ln net.Listener
	var err error
	if tlsCfg != nil {
		ln, err = tls.Listen("tcp", addr, tlsCfg)
	} else {
		ln, err = net.Listen("tcp", addr)
	}
	if err != nil {
		errCh <- err
		return
	}
	defer ln.Close()
	s.logger.Info("SIP stream listener ready", "transport", transport, "addr", addr)
	for {
		if tcpLn, ok := ln.(*net.TCPListener); ok {
			_ = tcpLn.SetDeadline(time.Now().Add(2 * time.Second))
		}
		conn, err := ln.Accept()
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				select {
				case <-ctx.Done():
					return
				default:
						continue
				}
			}
			errCh <- err
			return
		}
		go s.handleStreamConn(ctx, conn, transport)
	}
}

func (s *Server) listenTLS(ctx context.Context, errCh chan<- error) {
	if s.cfg.SIP.TLSCert == "" || s.cfg.SIP.TLSKey == "" {
		s.logger.Warn("SIP TLS disabled because certificate paths are empty")
		return
	}
	cert, err := tls.LoadX509KeyPair(s.cfg.SIP.TLSCert, s.cfg.SIP.TLSKey)
	if err != nil {
		errCh <- err
		return
	}
	s.listenTCP(ctx, errCh, s.cfg.SIP.TLS, &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{cert},
	}, "TLS")
}

func (s *Server) handleStreamConn(ctx context.Context, conn net.Conn, transport string) {
	defer conn.Close()
	reader := bufio.NewReader(conn)
	for {
		_ = conn.SetReadDeadline(time.Now().Add(2 * time.Minute))
		msg, err := ReadMessage(reader)
		if err != nil {
			if errors.Is(err, io.EOF) {
				return
			}
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				select {
				case <-ctx.Done():
					return
				default:
					continue
				}
			}
			s.logger.Warn("stream SIP read failed", "transport", transport, "remote", conn.RemoteAddr().String(), "error", err)
			return
		}
		reqCtx := &RequestContext{
			Transport: transport,
			Remote:    conn.RemoteAddr(),
			LocalAddr: conn.LocalAddr().String(),
			Send: func(resp *Message) error {
				_, err := conn.Write([]byte(resp.String()))
				return err
			},
		}
		s.handleMessage(context.Background(), msg, reqCtx)
	}
}

func (s *Server) handleMessage(ctx context.Context, msg *Message, reqCtx *RequestContext) {
	if !msg.IsRequest {
		s.handleResponse(ctx, msg)
		return
	}
	switch msg.Method {
	case "REGISTER":
		s.handleRegister(ctx, msg, reqCtx)
	case "OPTIONS":
		s.reply(reqCtx, BuildResponse(msg, 200, "OK", map[string]string{
			"Allow":  "REGISTER, INVITE, ACK, BYE, CANCEL, OPTIONS",
			"Accept": "application/sdp",
		}, ""))
	case "INVITE":
		s.handleInvite(ctx, msg, reqCtx)
	case "ACK":
		s.handleAck(ctx, msg)
	case "BYE":
		s.handleBye(ctx, msg, reqCtx)
	case "CANCEL":
		s.handleCancel(ctx, msg, reqCtx)
	default:
		s.reply(reqCtx, BuildResponse(msg, 501, "Not Implemented", nil, ""))
	}
}

func (s *Server) handleResponse(ctx context.Context, msg *Message) {
	callID := msg.GetHeader("Call-Id")
	if callID == "" {
		callID = msg.GetHeader("Call-ID")
	}
	s.invitesMu.RLock()
	dialog := s.invites[callID]
	s.invitesMu.RUnlock()
	if dialog == nil {
		return
	}
	switch {
	case msg.StatusCode >= 100 && msg.StatusCode < 200:
		if dialog.CallerCtx != nil {
			_ = dialog.CallerCtx.Send(stripTopVia(msg))
		}
	case msg.StatusCode >= 200 && msg.StatusCode < 300:
		_ = s.pbx.MarkAnswered(context.Background(), callID)
		if session := s.pbx.Get(callID); session != nil && strings.TrimSpace(msg.Body) != "" {
			msg = msg.Clone()
			msg.Body = rewriteSDPForRelay(msg.Body, s.cfg.RTP.PublicIP, session.CallerPort)
			msg.SetHeader("Content-Length", fmt.Sprintf("%d", len(msg.Body)))
		}
		if dialog.CallerCtx != nil {
			_ = dialog.CallerCtx.Send(stripTopVia(msg))
		}
	default:
		_ = s.pbx.MarkEnded(context.Background(), callID)
		if dialog.CallerCtx != nil {
			_ = dialog.CallerCtx.Send(stripTopVia(msg))
		}
		s.invitesMu.Lock()
		delete(s.invites, callID)
		s.invitesMu.Unlock()
	}
}

func (s *Server) handleRegister(ctx context.Context, msg *Message, reqCtx *RequestContext) {
	username := URIUser(firstURI(msg.GetHeader("To")))
	if username == "" {
		s.reply(reqCtx, BuildResponse(msg, 400, "Bad Request", nil, ""))
		return
	}
	ext, err := s.store.GetExtensionByNumber(ctx, username)
	if err != nil {
		s.reply(reqCtx, BuildResponse(msg, 403, "Forbidden", nil, ""))
		return
	}
	ip := util.RemoteIP(reqCtx.Remote)
	if blocked, until, err := s.store.IsBlocked(ctx, ip, username); err == nil && blocked {
		s.reply(reqCtx, BuildResponse(msg, 403, fmt.Sprintf("Blocked until %s", until.Format(time.RFC3339)), nil, ""))
		return
	}

	authz := msg.GetHeader("Authorization")
	if authz == "" {
		s.challenge(reqCtx, msg, false)
		return
	}
	params := parseDigestHeader(authz)
	if err := s.verifyDigest(msg, ext, params); err != nil {
		_, _ = s.store.RecordFailedAuth(ctx, ip, username, time.Duration(s.cfg.Security.BlockSeconds)*time.Second, s.cfg.Security.FailThreshold)
		s.reply(reqCtx, BuildResponse(msg, 403, "Forbidden", nil, ""))
		return
	}
	_ = s.store.ResetFailedAuth(ctx, ip, username)

	contact := strings.TrimSpace(msg.GetHeader("Contact"))
	if contact == "" {
		s.reply(reqCtx, BuildResponse(msg, 400, "Missing Contact", nil, ""))
		return
	}
	expires := parseExpires(msg)
	if expires == 0 || strings.Contains(strings.ToLower(contact), "expires=0") {
		_ = s.store.DeleteRegistration(ctx, username, contact, reqCtx.Transport)
	} else {
		if expires < 60 {
			expires = 60
		}
		if expires > 3600 {
			expires = 3600
		}
		if err := s.store.UpsertRegistration(ctx, username, contact, reqCtx.Remote.String(), reqCtx.Transport, time.Now().UTC().Add(time.Duration(expires)*time.Second)); err != nil {
			s.reply(reqCtx, BuildResponse(msg, 500, "Server Error", nil, ""))
			return
		}
	}

	s.reply(reqCtx, BuildResponse(msg, 200, "OK", map[string]string{
		"Contact": contact,
		"Date":    time.Now().UTC().Format(time.RFC1123),
	}, ""))
}

func (s *Server) handleInvite(ctx context.Context, msg *Message, reqCtx *RequestContext) {
	from := URIUser(firstURI(msg.GetHeader("From")))
	to := URIUser(msg.RequestURI)
	if from == "" || to == "" {
		s.reply(reqCtx, BuildResponse(msg, 400, "Bad Request", nil, ""))
		return
	}
	ext, err := s.store.GetExtensionByNumber(ctx, from)
	if err != nil {
		s.reply(reqCtx, BuildResponse(msg, 403, "Forbidden", nil, ""))
		return
	}
	authz := msg.GetHeader("Authorization")
	if authz == "" {
		s.challenge(reqCtx, msg, true)
		return
	}
	params := parseDigestHeader(authz)
	if err := s.verifyDigest(msg, ext, params); err != nil {
		_, _ = s.store.RecordFailedAuth(ctx, util.RemoteIP(reqCtx.Remote), from, time.Duration(s.cfg.Security.BlockSeconds)*time.Second, s.cfg.Security.FailThreshold)
		s.reply(reqCtx, BuildResponse(msg, 403, "Forbidden", nil, ""))
		return
	}
	_ = s.store.ResetFailedAuth(ctx, util.RemoteIP(reqCtx.Remote), from)

	regs, err := s.store.GetRegistrations(ctx, to)
	if err != nil || len(regs) == 0 {
		s.reply(reqCtx, BuildResponse(msg, 480, "Temporarily Unavailable", nil, ""))
		return
	}

	session, err := s.pbx.StartInternalCall(ctx, from, to, headerCallID(msg))
	if err != nil {
		s.reply(reqCtx, BuildResponse(msg, 500, "Server Error", nil, ""))
		return
	}
	outbound := msg.Clone()
	prependHeader(outbound, "Via", buildTopVia(s.cfg, reqCtx.Transport, reqCtx.LocalAddr))
	outbound.SetHeader("Record-Route", fmt.Sprintf("<sip:%s;lr>", s.cfg.Domain))
	outbound.Body = rewriteSDPForRelay(msg.Body, s.cfg.RTP.PublicIP, session.CalleePort)
	outbound.SetHeader("Content-Length", fmt.Sprintf("%d", len(outbound.Body)))

	s.reply(reqCtx, BuildResponse(msg, 100, "Trying", nil, ""))
	s.reply(reqCtx, BuildResponse(msg, 180, "Ringing", nil, ""))

	s.invitesMu.Lock()
	s.invites[headerCallID(msg)] = &inviteDialog{
		CallID:    headerCallID(msg),
		Caller:    from,
		Callee:    to,
		CallerCtx: reqCtx,
		InviteReq: msg.Clone(),
	}
	s.invitesMu.Unlock()

	if err := s.sendToRegistration(regs[0].Transport, normalizeSIPTarget(regs[0].SourceAddr), outbound); err != nil {
		_ = s.pbx.MarkEnded(ctx, headerCallID(msg))
		s.invitesMu.Lock()
		delete(s.invites, headerCallID(msg))
		s.invitesMu.Unlock()
		s.reply(reqCtx, BuildResponse(msg, 500, "Server Error", nil, ""))
		return
	}
}

func (s *Server) handleAck(ctx context.Context, msg *Message) {
	callID := headerCallID(msg)
	s.invitesMu.RLock()
	dialog := s.invites[callID]
	s.invitesMu.RUnlock()
	if dialog == nil {
		return
	}
	regs, err := s.store.GetRegistrations(ctx, dialog.Callee)
	if err != nil || len(regs) == 0 {
		return
	}
	outbound := stripTopVia(msg)
	_ = s.sendToRegistration(regs[0].Transport, normalizeSIPTarget(regs[0].SourceAddr), outbound)
}

func (s *Server) handleBye(ctx context.Context, msg *Message, reqCtx *RequestContext) {
	callID := headerCallID(msg)
	s.invitesMu.RLock()
	dialog := s.invites[callID]
	s.invitesMu.RUnlock()
	s.reply(reqCtx, BuildResponse(msg, 200, "OK", nil, ""))
	if dialog == nil {
		return
	}
	peer := dialog.Callee
	if URIUser(firstURI(msg.GetHeader("From"))) == dialog.Callee {
		peer = dialog.Caller
	}
	regs, err := s.store.GetRegistrations(ctx, peer)
	if err == nil && len(regs) > 0 {
		outbound := stripTopVia(msg)
		_ = s.sendToRegistration(regs[0].Transport, normalizeSIPTarget(regs[0].SourceAddr), outbound)
	}
	_ = s.pbx.MarkEnded(ctx, callID)
	s.invitesMu.Lock()
	delete(s.invites, callID)
	s.invitesMu.Unlock()
}

func (s *Server) handleCancel(ctx context.Context, msg *Message, reqCtx *RequestContext) {
	callID := headerCallID(msg)
	s.reply(reqCtx, BuildResponse(msg, 200, "OK", nil, ""))
	s.invitesMu.RLock()
	dialog := s.invites[callID]
	s.invitesMu.RUnlock()
	if dialog == nil {
		return
	}
	regs, err := s.store.GetRegistrations(ctx, dialog.Callee)
	if err == nil && len(regs) > 0 {
		outbound := stripTopVia(msg)
		_ = s.sendToRegistration(regs[0].Transport, normalizeSIPTarget(regs[0].SourceAddr), outbound)
	}
	_ = s.pbx.MarkEnded(ctx, callID)
	s.invitesMu.Lock()
	delete(s.invites, callID)
	s.invitesMu.Unlock()
}

func (s *Server) challenge(reqCtx *RequestContext, msg *Message, proxy bool) {
	code := 401
	reason := "Unauthorized"
	headerName := "WWW-Authenticate"
	if proxy {
		code = 407
		reason = "Proxy Authentication Required"
		headerName = "Proxy-Authenticate"
	}
	nonce := s.newNonce()
	s.reply(reqCtx, BuildResponse(msg, code, reason, map[string]string{
		headerName: fmt.Sprintf(`Digest realm="%s", nonce="%s", algorithm=MD5, qop="auth", stale=FALSE`, s.cfg.Realm, nonce),
	}, ""))
}

func (s *Server) verifyDigest(msg *Message, ext *db.Extension, params map[string]string) error {
	if params["username"] != ext.Number {
		return errors.New("username mismatch")
	}
	if params["realm"] != s.cfg.Realm {
		return errors.New("realm mismatch")
	}
	if !s.validNonce(params["nonce"]) {
		return errors.New("invalid nonce")
	}
	algorithm := strings.ToUpper(strings.TrimSpace(params["algorithm"]))
	if algorithm == "" {
		algorithm = "MD5"
	}
	ha1 := ext.HA1MD5
	if algorithm == "SHA-256" || algorithm == "SHA256" {
		ha1 = ext.HA1SHA256
	}
	ha2 := hashForAlgorithm(algorithm, msg.Method+":"+params["uri"])
	var expected string
	if qop := params["qop"]; qop != "" {
		expected = hashForAlgorithm(algorithm, fmt.Sprintf("%s:%s:%s:%s:%s:%s", ha1, params["nonce"], params["nc"], params["cnonce"], qop, ha2))
	} else {
		expected = hashForAlgorithm(algorithm, fmt.Sprintf("%s:%s:%s", ha1, params["nonce"], ha2))
	}
	if !strings.EqualFold(expected, params["response"]) {
		return errors.New("digest mismatch")
	}
	return nil
}

func (s *Server) newNonce() string {
	s.nonceMu.Lock()
	defer s.nonceMu.Unlock()
	nonce := auth.RandomHex(16)
	s.nonces[nonce] = time.Now().UTC().Add(time.Duration(s.cfg.SIP.NonceTTL) * time.Second)
	return nonce
}

func (s *Server) validNonce(nonce string) bool {
	s.nonceMu.Lock()
	defer s.nonceMu.Unlock()
	expiresAt, ok := s.nonces[nonce]
	if !ok {
		return false
	}
	if time.Now().UTC().After(expiresAt) {
		delete(s.nonces, nonce)
		return false
	}
	return true
}

func (s *Server) reply(reqCtx *RequestContext, msg *Message) {
	if err := reqCtx.Send(msg); err != nil {
		s.logger.Warn("failed sending SIP message", "remote", reqCtx.Remote.String(), "error", err)
	}
}

func (s *Server) sendToRegistration(transport, target string, msg *Message) error {
	switch strings.ToUpper(transport) {
	case "UDP":
		conn, err := net.Dial("udp", target)
		if err != nil {
			return err
		}
		defer conn.Close()
		_, err = conn.Write([]byte(msg.String()))
		return err
	case "TLS":
		conn, err := tls.Dial("tcp", target, &tls.Config{
			InsecureSkipVerify: true,
			ServerName:         s.cfg.Domain,
			MinVersion:         tls.VersionTLS12,
		})
		if err != nil {
			return err
		}
		defer conn.Close()
		_, err = conn.Write([]byte(msg.String()))
		return err
	default:
		conn, err := net.Dial("tcp", target)
		if err != nil {
			return err
		}
		defer conn.Close()
		_, err = conn.Write([]byte(msg.String()))
		return err
	}
}

func normalizeSIPTarget(target string) string {
	host, port, err := net.SplitHostPort(target)
	if err != nil {
		return target
	}
	return net.JoinHostPort(strings.Trim(host, "[]"), port)
}

func parseExpires(msg *Message) int {
	if exp := strings.TrimSpace(msg.GetHeader("Expires")); exp != "" {
		var v int
		fmt.Sscanf(exp, "%d", &v)
		return v
	}
	contact := strings.ToLower(msg.GetHeader("Contact"))
	if idx := strings.Index(contact, "expires="); idx >= 0 {
		var v int
		fmt.Sscanf(contact[idx+8:], "%d", &v)
		return v
	}
	return 300
}

func parseDigestHeader(value string) map[string]string {
	value = strings.TrimSpace(strings.TrimPrefix(value, "Digest"))
	out := map[string]string{}
	for _, part := range strings.Split(value, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		kv := strings.SplitN(part, "=", 2)
		if len(kv) != 2 {
			continue
		}
		out[strings.ToLower(strings.TrimSpace(kv[0]))] = strings.Trim(strings.TrimSpace(kv[1]), `"`)
	}
	return out
}

func buildTopVia(cfg *config.Config, transport, localAddr string) string {
	hostPort := strings.TrimSpace(localAddr)
	if hostPort == "" {
		host := cfg.Domain
		if host == "" {
			host = "127.0.0.1"
		}
		hostPort = host
	}
	return fmt.Sprintf("SIP/2.0/%s %s;branch=z9hG4bK%s;rport", strings.ToUpper(transport), hostPort, auth.RandomHex(6))
}

func prependHeader(msg *Message, name, value string) {
	if msg == nil {
		return
	}
	key := textproto.CanonicalMIMEHeaderKey(name)
	existing := append([]string(nil), msg.Headers[key]...)
	msg.Headers[key] = append([]string{value}, existing...)
}

func stripTopVia(msg *Message) *Message {
	if msg == nil {
		return nil
	}
	out := msg.Clone()
	via := out.Values("Via")
	if len(via) <= 1 {
		return out
	}
	key := "Via"
	out.Headers[key] = append([]string(nil), via[1:]...)
	return out
}

func hashForAlgorithm(algorithm, input string) string {
	switch algorithm {
	case "SHA-256", "SHA256":
		sum := sha256.Sum256([]byte(input))
		return hex.EncodeToString(sum[:])
	default:
		sum := md5.Sum([]byte(input))
		return hex.EncodeToString(sum[:])
	}
}

func headerCallID(msg *Message) string {
	if v := msg.GetHeader("Call-ID"); v != "" {
		return v
	}
	return msg.GetHeader("Call-Id")
}

func firstURI(value string) string {
	value = strings.TrimSpace(value)
	if i := strings.Index(value, "<"); i >= 0 {
		if j := strings.Index(value[i:], ">"); j >= 0 {
			return value[i+1 : i+j]
		}
	}
	if semi := strings.Index(value, ";"); semi >= 0 {
		return value[:semi]
	}
	return value
}

func rewriteSDPForRelay(body, relayIP string, relayPort int) string {
	if strings.TrimSpace(body) == "" {
		return body
	}
	lines := strings.Split(body, "\n")
	out := make([]string, 0, len(lines))
	for _, raw := range lines {
		line := strings.TrimRight(raw, "\r")
		switch {
		case strings.HasPrefix(line, "c="):
			out = append(out, fmt.Sprintf("c=IN IP4 %s", relayIP))
		case strings.HasPrefix(line, "m=audio "):
			parts := strings.Fields(line)
			if len(parts) >= 4 {
				parts[1] = fmt.Sprintf("%d", relayPort)
				line = strings.Join(parts, " ")
			}
			out = append(out, line)
		default:
			out = append(out, line)
		}
	}
	return strings.Join(out, "\r\n") + "\r\n"
}
