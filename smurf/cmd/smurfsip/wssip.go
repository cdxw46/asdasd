package main

import (
	"log"
	"net/http"
	"strings"

	"github.com/gorilla/websocket"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/wssip"
)

func (s *Server) startWSS(addr, certFile, keyFile string) error {
	up := websocket.Upgrader{
		ReadBufferSize:  65536,
		WriteBufferSize: 65536,
		CheckOrigin: func(r *http.Request) bool {
			return true
		},
		Subprotocols: []string{"sip"},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/sip", func(w http.ResponseWriter, r *http.Request) {
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			http.Error(w, "websocket required", http.StatusUpgradeRequired)
			return
		}
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		sess := wssip.NewSession(c)
		defer func() {
			s.clearWSRegistration(sess)
			_ = c.Close()
		}()
		for {
			_, data, err := c.ReadMessage()
			if err != nil {
				return
			}
			msg, err := sip.ParseMessage(data)
			if err != nil {
				continue
			}
			if !msg.IsRequest {
				s.routeWSResponse(msg)
				continue
			}
			raddr := c.RemoteAddr().String()
			resp := s.handleMessageWithWS(sess, msg, "ws", raddr, nil, nil, nil)
			if resp != nil {
				_ = sess.WriteText([]byte(resp.String()))
			}
		}
	})
	srv := &http.Server{
		Addr:    addr,
		Handler: mux,
	}
	log.Printf("SIP over WebSocket (WSS) listening %s path /sip (Sec-WebSocket-Protocol: sip)", addr)
	return srv.ListenAndServeTLS(certFile, keyFile)
}

func (s *Server) routeWSResponse(m *sip.Message) {
	cid := m.Headers.Get("call-id")
	if cid == "" {
		return
	}
	s.pendingMu.Lock()
	ch := s.pendingWSResp[cid]
	s.pendingMu.Unlock()
	if ch == nil {
		return
	}
	select {
	case ch <- m:
	default:
	}
}
