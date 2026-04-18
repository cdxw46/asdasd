package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sdp"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/webhook"
	"github.com/smurf/pbx/internal/webrtcmedia"
	"github.com/smurf/pbx/internal/wssip"
)

func (s *Server) handleInviteToQueue(ctx context.Context, ws *wssip.Session, m *sip.Message, transport, from, queueSlug, callID string, callerReg *db.Registration) *sip.Message {
	q, err := s.pool.GetCallQueue(ctx, queueSlug)
	if err != nil {
		return sipResponse(m, 404, "Queue Not Found")
	}
	members, err := s.pool.ListQueueMemberExtensions(ctx, queueSlug)
	if err != nil || len(members) == 0 {
		return sipResponse(m, 480, "Queue Empty")
	}

	cdrID, err := s.pool.InsertCDR(ctx, callID, from, queueSlug, "queue")
	if err != nil {
		log.Printf("cdr queue: %v", err)
	}
	_ = s.pool.SetCDRQueue(ctx, cdrID, queueSlug)

	rtpA, rtpB, err := s.relay.OpenSession(callID)
	if err != nil {
		_ = s.pool.UpdateCDREnded(ctx, cdrID, "relay-fail")
		go webhook.NotifyEnded(context.Background(), s.pool, cdrID)
		return sipResponse(m, 500, "Relay Error")
	}

	webrtc := ws != nil && isWebRTCJSONInvite(m)
	callerOfferSDP := string(m.Body)
	var webrtcAnswerSDP string
	var webrtcCleanup func()
	if webrtc {
		var wrap webrtcInviteBody
		if err := json.Unmarshal(m.Body, &wrap); err != nil || wrap.SDP == "" {
			s.relay.CloseSession(callID)
			_ = s.pool.UpdateCDREnded(ctx, cdrID, "bad-json")
			go webhook.NotifyEnded(context.Background(), s.pool, cdrID)
			return sipResponse(m, 400, "Bad SDP JSON")
		}
		callerOfferSDP = wrap.SDP
		var ans string
		ans, webrtcCleanup, err = webrtcmedia.NewBridge(wrap.SDP, s.publicIP, rtpA)
		if err != nil {
			s.relay.CloseSession(callID)
			_ = s.pool.UpdateCDREnded(ctx, cdrID, "webrtc-fail")
			go webhook.NotifyEnded(context.Background(), s.pool, cdrID)
			return sipResponse(m, 500, "WebRTC Error")
		}
		webrtcAnswerSDP = ans
	}

	br := &callBridge{
		LegACallID:    callID,
		LegBCallID:    callID + "-b",
		CallerWS:      ws,
		fromExt:       from,
		toExt:         queueSlug,
		cdrID:         cdrID,
		relayPorts:    [2]int{rtpA, rtpB},
		callerReg:     callerReg,
		webrtcCleanup: webrtcCleanup,
	}
	s.callMu.Lock()
	s.calls[callID] = br
	s.calls[callID+"-b"] = br
	s.callMu.Unlock()

	ringEach := q.RingTimeoutSec
	if ringEach <= 0 {
		ringEach = 25
	}
	offerToCallee := sdp.PatchMediaEndpoint(callerOfferSDP, s.publicIP, rtpB)
	picked, respB := s.queueSequential(ctx, m, transport, from, queueSlug, callID, members, offerToCallee, ringEach)

	if picked == nil || respB == nil {
		s.tearDownCall(br, callID)
		_ = s.pool.UpdateCDREnded(ctx, cdrID, "queue-timeout")
		go webhook.NotifyEnded(context.Background(), s.pool, cdrID)
		return sipResponse(m, 480, "Temporarily Unavailable")
	}
	if respB.StatusCode >= 300 {
		s.tearDownCall(br, callID)
		_ = s.pool.UpdateCDREnded(ctx, cdrID, fmt.Sprintf("sip-%d", respB.StatusCode))
		go webhook.NotifyEnded(context.Background(), s.pool, cdrID)
		return sipResponse(m, respB.StatusCode, respB.Reason)
	}

	legCID := callID + "-q-" + picked.Extension
	s.callMu.Lock()
	if b := s.calls[callID]; b != nil {
		b.queueCalleeExt = picked.Extension
		delete(s.calls, b.LegBCallID)
		b.LegBCallID = legCID
		s.calls[legCID] = b
	}
	s.callMu.Unlock()

	if respB.StatusCode == 200 {
		_ = s.pool.UpdateCDRAnswered(ctx, cdrID)
		go webhook.NotifyAnswered(context.Background(), s.pool, cdrID)
		ack := ackToCallee(s, picked, m, respB, legCID)
		_ = s.sendRequestFireAndForget(picked, ack)
	}

	resp := sipResponse(m, respB.StatusCode, respB.Reason)
	if t := respB.Headers.Get("to"); t != "" {
		resp.Headers.Set("to", t)
	}
	if webrtc {
		body, _ := json.Marshal(webrtcInviteBody{Type: "answer", SDP: webrtcAnswerSDP})
		resp.Body = body
		resp.AddHeader("Content-Type", "application/json")
	} else {
		answerToCaller := sdp.PatchMediaEndpoint(string(respB.Body), s.publicIP, rtpA)
		resp.Body = []byte(answerToCaller)
		resp.AddHeader("Content-Type", "application/sdp")
	}
	resp.AddHeader("Content-Length", strconv.Itoa(len(resp.Body)))
	return resp
}

func (s *Server) queueSequential(ctx context.Context, m *sip.Message, transport, from, queueSlug, callID string, members []string, offerToCallee string, ringSec int) (*db.Registration, *sip.Message) {
	offerBytes := []byte(offerToCallee)
	for _, ext := range members {
		reg, err := s.pool.GetRegistration(ctx, ext)
		if err != nil {
			continue
		}
		legCID := callID + "-q-" + ext
		out := s.buildOutboundInvite(m, transport, from, queueSlug, ext, legCID, offerBytes)
		resp, err := s.sendRequestToUAWithDeadline(ctx, reg, out, time.Duration(ringSec)*time.Second)
		if err != nil || resp == nil {
			s.cancelOutboundLeg(reg, m, from, ext, legCID, out.Headers.Get("cseq"))
			continue
		}
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return reg, resp
		}
		if resp.StatusCode >= 100 && resp.StatusCode < 200 {
			s.cancelOutboundLeg(reg, m, from, ext, legCID, out.Headers.Get("cseq"))
			continue
		}
	}
	return nil, nil
}

func (s *Server) cancelOutboundLeg(reg *db.Registration, inv *sip.Message, from, toExt, legCID, inviteCSeq string) {
	parts := strings.Fields(inviteCSeq)
	if len(parts) < 1 {
		return
	}
	n, err := strconv.Atoi(parts[0])
	if err != nil {
		return
	}
	cancel := &sip.Message{
		StartLine: sip.StartLine{IsRequest: true, Method: "CANCEL", RequestURI: fmt.Sprintf("sip:%s@%s", toExt, s.realm), Proto: "SIP/2.0"},
	}
	cancel.AddHeader("Via", s.sipViaUDP())
	cancel.AddHeader("Max-Forwards", "70")
	cancel.AddHeader("From", inv.Headers.Get("from"))
	cancel.AddHeader("To", fmt.Sprintf("<sip:%s@%s>", toExt, s.realm))
	cancel.AddHeader("Call-ID", legCID)
	cancel.AddHeader("CSeq", fmt.Sprintf("%d CANCEL", n))
	cancel.AddHeader("Content-Length", "0")
	_ = s.sendRequestFireAndForget(reg, cancel)
}

func (s *Server) buildOutboundInvite(m *sip.Message, transport, from, queueSlug, toExt, legCallID string, sdpBody []byte) *sip.Message {
	out := cloneRequest(m)
	out.Method = "INVITE"
	out.RequestURI = fmt.Sprintf("sip:%s@%s", toExt, s.realm)
	out.Body = append([]byte(nil), sdpBody...)
	out.Headers = sip.HeaderMap{}
	out.AddHeader("Via", s.sipViaUDP())
	out.AddHeader("Max-Forwards", "70")
	out.AddHeader("From", m.Headers.Get("from"))
	out.AddHeader("To", fmt.Sprintf("<sip:%s@%s>", toExt, s.realm))
	out.AddHeader("Call-ID", legCallID)
	out.AddHeader("CSeq", nextCSeq(m.Headers.Get("cseq"), "INVITE"))
	out.AddHeader("Contact", s.outboundContact(from, transport))
	out.AddHeader("X-SMURF-Queue", queueSlug)
	out.AddHeader("Content-Type", "application/sdp")
	out.AddHeader("Content-Length", strconv.Itoa(len(out.Body)))
	return out
}

func (s *Server) calleeRegistrationForBridge(ctx context.Context, br *callBridge) (*db.Registration, error) {
	if br.queueCalleeExt != "" {
		return s.pool.GetRegistration(ctx, br.queueCalleeExt)
	}
	return s.pool.GetRegistration(ctx, br.toExt)
}
