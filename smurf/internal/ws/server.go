package ws

 import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"smurf/internal/config"
	"smurf/internal/db"
	"smurf/internal/realtime"
	"smurf/internal/sip"
	"smurf/internal/util"
)

type SIPMessageHandler func(ctx context.Context, msg *sip.Message, reqCtx *sip.RequestContext)

type Server struct {
	cfg        *config.Config
	store      *db.Store
	logger     *util.Logger
	hub        *realtime.Hub
	onSIP      SIPMessageHandler
	mu         sync.RWMutex
	sipClients map[string]*client
}

type client struct {
	id        string
	conn      net.Conn
	remote    string
	transport string
	sendMu    sync.Mutex
}

func New(cfg *config.Config, store *db.Store, logger *util.Logger, hub *realtime.Hub, onSIP SIPMessageHandler) *Server {
	return &Server{
		cfg:        cfg,
		store:      store,
		logger:     logger,
		hub:        hub,
		onSIP:      onSIP,
		sipClients: map[string]*client{},
	}
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "websocket not supported", http.StatusUpgradeRequired)
		return
	}
	conn, _, err := hj.Hijack()
	if err != nil {
		http.Error(w, "hijack failed", http.StatusInternalServerError)
		return
	}
	go s.handleUpgradedConn(r.Context(), conn, r)
}

func (s *Server) Start(ctx context.Context) error {
	ln, err := net.Listen("tcp", s.cfg.HTTP.WS)
	if err != nil {
		return err
	}
	defer ln.Close()
	s.logger.Info("websocket listener ready", "addr", s.cfg.HTTP.WS)
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return nil
			default:
			}
			return err
		}
		go s.handleConn(ctx, conn)
	}
}

func (s *Server) handleConn(ctx context.Context, conn net.Conn) {
	defer conn.Close()
	reader := bufio.NewReader(conn)
	req, err := http.ReadRequest(reader)
	if err != nil {
		s.logger.Warn("websocket handshake read failed", "error", err)
		return
	}
	s.handleWebSocketSession(ctx, conn, req)
}

func (s *Server) handleUpgradedConn(ctx context.Context, conn net.Conn, req *http.Request) {
	defer conn.Close()
	s.handleWebSocketSession(ctx, conn, req)
}

func (s *Server) handleWebSocketSession(ctx context.Context, conn net.Conn, req *http.Request) {
	if !strings.EqualFold(req.Header.Get("Upgrade"), "websocket") || !strings.Contains(strings.ToLower(req.Header.Get("Connection")), "upgrade") {
		_, _ = io.WriteString(conn, "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
		return
	}
	wsKey := strings.TrimSpace(req.Header.Get("Sec-WebSocket-Key"))
	if wsKey == "" {
		_, _ = io.WriteString(conn, "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
		return
	}
	subprotocol := ""
	if strings.Contains(req.Header.Get("Sec-WebSocket-Protocol"), "sip") {
		subprotocol = "sip"
	}
	accept := computeAccept(wsKey)
	resp := strings.Builder{}
	resp.WriteString("HTTP/1.1 101 Switching Protocols\r\n")
	resp.WriteString("Upgrade: websocket\r\n")
	resp.WriteString("Connection: Upgrade\r\n")
	resp.WriteString("Sec-WebSocket-Accept: " + accept + "\r\n")
	if subprotocol != "" {
		resp.WriteString("Sec-WebSocket-Protocol: " + subprotocol + "\r\n")
	}
	resp.WriteString("\r\n")
	if _, err := io.WriteString(conn, resp.String()); err != nil {
		return
	}

	c := &client{
		id:        randomID(),
		conn:      conn,
		remote:    conn.RemoteAddr().String(),
		transport: "WS",
	}

	if subprotocol == "sip" {
		s.mu.Lock()
		s.sipClients[c.id] = c
		s.mu.Unlock()
		defer func() {
			s.mu.Lock()
			delete(s.sipClients, c.id)
			s.mu.Unlock()
		}()
	}
	sub := s.hub.Subscribe(c.id, "*")
	defer s.hub.Unsubscribe(c.id)
	go func() {
		for ev := range sub.Ch {
			if subprotocol == "sip" {
				continue
			}
			_ = c.writeJSON(ev)
		}
	}()

	for {
		payload, opcode, err := readFrame(conn)
		if err != nil {
			if ctx.Err() == nil && !isNetClosed(err) {
				s.logger.Warn("websocket read failed", "remote", c.remote, "error", err)
			}
			return
		}
		switch opcode {
		case 0x1:
			if subprotocol == "sip" {
				msg, err := sip.ParseMessage(payload)
				if err != nil {
					s.logger.Warn("sip over ws parse failed", "error", err)
					continue
				}
				if s.onSIP != nil {
					s.onSIP(context.Background(), msg, &sip.RequestContext{
						Transport: "WS",
						Remote:    conn.RemoteAddr(),
						LocalAddr: conn.LocalAddr().String(),
						Send: func(resp *sip.Message) error {
							return c.writeText(resp.String())
						},
					})
				}
				continue
			}
			s.handleJSONText(c, payload)
		case 0x8:
			_ = writeControlFrame(conn, 0x8, nil)
			return
		case 0x9:
			_ = writeControlFrame(conn, 0xA, payload)
		}
	}
}

func (s *Server) handleJSONText(c *client, payload []byte) {
	event := realtime.Event{}
	if err := decodeJSON(payload, &event); err != nil {
		return
	}
	if event.Type == "ping" {
		_ = c.writeJSON(realtime.Event{Type: "pong", Payload: map[string]any{"ts": time.Now().UTC()}})
	}
}

func (s *Server) BroadcastPresence(extension, status string) {
		s.hub.Publish("presence", "presence", map[string]any{
			"extension": extension,
			"status":    status,
		})
}

func (c *client) writeText(text string) error {
	c.sendMu.Lock()
	defer c.sendMu.Unlock()
	return writeTextFrame(c.conn, []byte(text))
}

func (c *client) writeJSON(v any) error {
	raw, err := encodeJSON(v)
	if err != nil {
		return err
	}
	return c.writeText(string(raw))
}

func computeAccept(key string) string {
	sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	return base64.StdEncoding.EncodeToString(sum[:])
}

func randomID() string {
	const alpha = "abcdef0123456789"
	var b strings.Builder
	for i := 0; i < 16; i++ {
		b.WriteByte(alpha[rand.Intn(len(alpha))])
	}
	return b.String()
}

func readFrame(r io.Reader) ([]byte, byte, error) {
	header := make([]byte, 2)
	if _, err := io.ReadFull(r, header); err != nil {
		return nil, 0, err
	}
	opcode := header[0] & 0x0f
	masked := (header[1] & 0x80) != 0
	length := int(header[1] & 0x7f)
	switch length {
	case 126:
		ext := make([]byte, 2)
		if _, err := io.ReadFull(r, ext); err != nil {
			return nil, 0, err
		}
		length = int(binary.BigEndian.Uint16(ext))
	case 127:
		ext := make([]byte, 8)
		if _, err := io.ReadFull(r, ext); err != nil {
			return nil, 0, err
		}
		length64 := binary.BigEndian.Uint64(ext)
		if length64 > 1<<24 {
			return nil, 0, fmt.Errorf("frame too large")
		}
		length = int(length64)
	}
	maskKey := make([]byte, 4)
	if masked {
		if _, err := io.ReadFull(r, maskKey); err != nil {
			return nil, 0, err
		}
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(r, payload); err != nil {
		return nil, 0, err
	}
	if masked {
		for i := range payload {
			payload[i] ^= maskKey[i%4]
		}
	}
	return payload, opcode, nil
}

func writeTextFrame(w io.Writer, payload []byte) error {
	return writeFrame(w, 0x1, payload)
}

func writeControlFrame(w io.Writer, opcode byte, payload []byte) error {
	return writeFrame(w, opcode, payload)
}

func writeFrame(w io.Writer, opcode byte, payload []byte) error {
	header := []byte{0x80 | opcode}
	switch {
	case len(payload) < 126:
		header = append(header, byte(len(payload)))
	case len(payload) <= 0xffff:
		header = append(header, 126, byte(len(payload)>>8), byte(len(payload)))
	default:
		header = append(header, 127, 0, 0, 0, 0, byte(len(payload)>>24), byte(len(payload)>>16), byte(len(payload)>>8), byte(len(payload)))
	}
	if _, err := w.Write(header); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}

func encodeJSON(v any) ([]byte, error) {
	return jsonMarshal(v)
}

func decodeJSON(raw []byte, v any) error {
	return jsonUnmarshal(raw, v)
}

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }
func jsonUnmarshal(raw []byte, v any) error { return json.Unmarshal(raw, v) }

func isNetClosed(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "closed network connection") || strings.Contains(s, "use of closed network connection")
}
