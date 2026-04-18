package wssip

import (
	"sync"

	"github.com/gorilla/websocket"
)

// Session wraps a WebSocket used as SIP transport (RFC 7118 framing: one SIP message per text frame).
type Session struct {
	c   *websocket.Conn
	Ext string // SIP user part after successful REGISTER
	mu  sync.Mutex
}

func NewSession(c *websocket.Conn) *Session {
	return &Session{c: c}
}

func (s *Session) WriteText(b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.c.WriteMessage(websocket.TextMessage, b)
}

func (s *Session) Conn() *websocket.Conn { return s.c }
