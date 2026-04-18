package main

import (
	"crypto/tls"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

var sipUpgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	CheckOrigin: func(r *http.Request) bool {
		return true
	},
}

// handleSIPWebSocketProxy upgrades the client connection and bridges to smurfsip WSS (RFC 7118 subprotocol "sip").
func handleSIPWebSocketProxy(w http.ResponseWriter, r *http.Request) {
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	upstream := getenv("SMURF_WSS_UPSTREAM", "127.0.0.1:5081")
	serverName := getenv("SMURF_WSS_TLS_SERVER_NAME", "smurf.local")
	target := "wss://" + upstream + "/sip"

	client, err := sipUpgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("sip ws upgrade (client): %v", err)
		return
	}
	defer client.Close()

	d := websocket.Dialer{
		HandshakeTimeout: 15 * time.Second,
		Subprotocols:     []string{"sip"},
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: true,
			ServerName:         serverName,
			MinVersion:         tls.VersionTLS12,
		},
	}
	backend, resp, err := d.Dial(target, nil)
	if err != nil {
		log.Printf("sip ws dial upstream %s: %v", target, err)
		if resp != nil {
			_ = resp.Body.Close()
		}
		_ = client.WriteControl(websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseTryAgainLater, "upstream unavailable"),
			time.Now().Add(5*time.Second))
		return
	}
	defer backend.Close()

	errCh := make(chan error, 2)
	copyWS := func(dst, src *websocket.Conn) {
		for {
			mt, data, err := src.ReadMessage()
			if err != nil {
				errCh <- err
				return
			}
			if err := dst.WriteMessage(mt, data); err != nil {
				errCh <- err
				return
			}
		}
	}
	go copyWS(backend, client)
	go copyWS(client, backend)
	err = <-errCh
	if err != nil && err != io.EOF && !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway, websocket.CloseNoStatusReceived) {
		log.Printf("sip ws bridge: %v", err)
	}
}
