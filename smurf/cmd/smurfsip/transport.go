package main

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"
	"time"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/wssip"
)

func (s *Server) sendRequestToUA(ctx context.Context, reg *db.Registration, req *sip.Message) (*sip.Message, error) {
	return s.sendRequestToUAWithDeadline(ctx, reg, req, 32*time.Second)
}

func (s *Server) sendRequestToUAWithDeadline(ctx context.Context, reg *db.Registration, req *sip.Message, d time.Duration) (*sip.Message, error) {
	dctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	switch reg.Transport {
	case "ws":
		return s.sendRequestOverWS(dctx, reg, req)
	default:
		return s.sendRequestUDPOrTCP(dctx, reg, req)
	}
}

func (s *Server) sendRequestFireAndForget(reg *db.Registration, req *sip.Message) error {
	switch reg.Transport {
	case "ws":
		callee := s.wsSessionFor(reg.Extension)
		if callee == nil {
			return fmt.Errorf("no ws session")
		}
		return callee.WriteText([]byte(req.String()))
	default:
		return s.sendRawUDPOrTCP(reg, req.String())
	}
}

func (s *Server) sendRawUDPOrTCP(reg *db.Registration, payload string) error {
	addr := fmt.Sprintf("%s:%d", reg.RemoteIP, reg.RemotePort)
	var c net.Conn
	var err error
	switch reg.Transport {
	case "tcp":
		c, err = net.DialTimeout("tcp", addr, 5*time.Second)
	case "tls":
		c, err = tls.Dial("tcp", addr, &tls.Config{InsecureSkipVerify: true})
	default:
		c, err = net.DialTimeout("udp", addr, 5*time.Second)
	}
	if err != nil {
		return err
	}
	defer c.Close()
	_, err = io.WriteString(c, payload)
	return err
}

func (s *Server) sendRequestUDPOrTCP(ctx context.Context, reg *db.Registration, req *sip.Message) (*sip.Message, error) {
	addr := fmt.Sprintf("%s:%d", reg.RemoteIP, reg.RemotePort)
	var c net.Conn
	var err error
	switch reg.Transport {
	case "tcp":
		c, err = net.DialTimeout("tcp", addr, 5*time.Second)
	case "tls":
		c, err = tls.Dial("tcp", addr, &tls.Config{InsecureSkipVerify: true})
	default:
		c, err = net.DialTimeout("udp", addr, 5*time.Second)
	}
	if err != nil {
		return nil, err
	}
	defer c.Close()
	if _, err := io.WriteString(c, req.String()); err != nil {
		return nil, err
	}
	buf := make([]byte, 0, 65536)
	tmp := make([]byte, 4096)
	deadline := time.Now().Add(32 * time.Second)
	if dl, ok := ctx.Deadline(); ok && dl.Before(deadline) {
		deadline = dl
	}
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}
		_ = c.SetReadDeadline(deadline)
		n, err := c.Read(tmp)
		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.Canceled) {
				return nil, ctx.Err()
			}
			return nil, err
		}
		buf = append(buf, tmp[:n]...)
		for sipMessageComplete(buf) {
			msg, err := sip.ParseMessage(buf)
			if err != nil {
				return nil, err
			}
			cons := sipConsumed(buf)
			buf = buf[cons:]
			if !msg.IsRequest && msg.StatusCode >= 200 {
				return msg, nil
			}
			if !msg.IsRequest && msg.StatusCode >= 100 && msg.StatusCode < 200 {
				continue
			}
			if !msg.IsRequest {
				return msg, nil
			}
		}
	}
}

func (s *Server) sendRequestOverWS(ctx context.Context, reg *db.Registration, req *sip.Message) (*sip.Message, error) {
	callee := s.wsSessionFor(reg.Extension)
	if callee == nil {
		return nil, fmt.Errorf("callee not connected on websocket")
	}
	cid := req.Headers.Get("call-id")
	if cid == "" {
		return nil, fmt.Errorf("missing Call-ID")
	}
	ch := make(chan *sip.Message, 32)
	s.pendingMu.Lock()
	s.pendingWSResp[cid] = ch
	s.pendingMu.Unlock()
	defer func() {
		s.pendingMu.Lock()
		delete(s.pendingWSResp, cid)
		s.pendingMu.Unlock()
	}()

	if err := callee.WriteText([]byte(req.String())); err != nil {
		return nil, err
	}
	wait := 32 * time.Second
	if dl, ok := ctx.Deadline(); ok {
		if d := time.Until(dl); d > 0 {
			wait = d
		}
	}
	deadline := time.After(wait)
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case msg := <-ch:
			if msg == nil {
				continue
			}
			if msg.IsRequest {
				continue
			}
			if msg.StatusCode >= 100 && msg.StatusCode < 200 {
				continue
			}
			if msg.StatusCode >= 200 {
				return msg, nil
			}
		case <-deadline:
			return nil, fmt.Errorf("ws response timeout")
		}
	}
}

func (s *Server) wsSessionFor(ext string) *wssip.Session {
	s.wsMu.RLock()
	defer s.wsMu.RUnlock()
	return s.wsSessions[ext]
}

func calleeWSSession(s *Server, reg *db.Registration) *wssip.Session {
	if reg == nil || reg.Transport != "ws" {
		return nil
	}
	return s.wsSessionFor(reg.Extension)
}

func (s *Server) clearWSRegistration(sess *wssip.Session) {
	if sess == nil || sess.Ext == "" {
		return
	}
	s.wsMu.Lock()
	defer s.wsMu.Unlock()
	if cur, ok := s.wsSessions[sess.Ext]; ok && cur == sess {
		delete(s.wsSessions, sess.Ext)
	}
}

func sipContactRequestURI(resp *sip.Message, fallback string) string {
	c := resp.Headers.Get("contact")
	if c == "" {
		return fallback
	}
	return contactURI(c)
}

func ackToCallee(s *Server, reg *db.Registration, inv *sip.Message, resp200 *sip.Message, legCallID string) *sip.Message {
	ru := sipContactRequestURI(resp200, reg.ContactURI)
	ack := &sip.Message{
		StartLine: sip.StartLine{IsRequest: true, Method: "ACK", RequestURI: ru, Proto: "SIP/2.0"},
		Headers:   sip.HeaderMap{},
	}
	ack.AddHeader("Via", s.sipViaUDP())
	ack.AddHeader("Max-Forwards", "70")
	ack.AddHeader("From", inv.Headers.Get("from"))
	if t := resp200.Headers.Get("to"); t != "" {
		ack.AddHeader("To", t)
	}
	ack.AddHeader("Call-ID", legCallID)
	parts := strings.Fields(inv.Headers.Get("cseq"))
	if len(parts) > 0 {
		ack.AddHeader("CSeq", parts[0]+" ACK")
	} else {
		ack.AddHeader("CSeq", "1 ACK")
	}
	ack.AddHeader("Content-Length", "0")
	return ack
}

func (s *Server) tearDownCall(br *callBridge, callID string) {
	if br == nil {
		return
	}
	s.relay.CloseSession(callID)
	s.callMu.Lock()
	delete(s.calls, br.LegACallID)
	delete(s.calls, br.LegBCallID)
	s.callMu.Unlock()
	if br.webrtcCleanup != nil {
		br.webrtcCleanup()
		br.webrtcCleanup = nil
	}
}
