package main

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sdp"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/webrtcmedia"
	"github.com/smurf/pbx/internal/wssip"
)

type webrtcInviteBody struct {
	Type string `json:"type"`
	SDP  string `json:"sdp"`
}

func (s *Server) handleInviteWebRTC(ctx context.Context, m *sip.Message, ws *wssip.Session, from, to, callID string) *sip.Message {
	ct := strings.ToLower(m.Headers.Get("content-type"))
	if !strings.Contains(ct, "json") {
		return sipResponse(m, 415, "Unsupported Media Type")
	}
	var wrap webrtcInviteBody
	if err := json.Unmarshal(m.Body, &wrap); err != nil || wrap.SDP == "" {
		return sipResponse(m, 400, "Bad SDP JSON")
	}
	reg, err := s.pool.GetRegistration(ctx, to)
	if err != nil {
		return sipResponse(m, 480, "Temporarily Unavailable")
	}

	rtpA, rtpB, err := s.relay.OpenSession(callID)
	if err != nil {
		return sipResponse(m, 500, "Relay Error")
	}
	cdrID, _ := s.pool.InsertCDR(ctx, callID, from, to, "webrtc-internal")

	answerSDP, cleanup, err := webrtcmedia.NewBridge(wrap.SDP, s.publicIP, rtpA)
	if err != nil {
		s.relay.CloseSession(callID)
		_ = s.pool.UpdateCDREnded(ctx, cdrID, "webrtc-fail")
		return sipResponse(m, 500, "WebRTC Error")
	}

	calleeOffer := sdp.BuildPCMU(s.publicIP, rtpB)
	out := cloneRequest(m)
	out.Method = "INVITE"
	out.RequestURI = reg.ContactURI
	out.Body = []byte(calleeOffer)
	out.Headers = sip.HeaderMap{}
	out.AddHeader("Via", s.sipViaUDP())
	out.AddHeader("Max-Forwards", "70")
	out.AddHeader("From", m.Headers.Get("from"))
	out.AddHeader("To", fmt.Sprintf("<sip:%s@%s>", to, s.realm))
	out.AddHeader("Call-ID", callID+"-b")
	out.AddHeader("CSeq", nextCSeq(m.Headers.Get("cseq"), "INVITE"))
	out.AddHeader("Contact", fmt.Sprintf("<sip:%s@%s:5060;transport=udp>", from, s.publicIP))
	out.AddHeader("Content-Type", "application/sdp")
	out.AddHeader("Content-Length", strconv.Itoa(len(out.Body)))

	respB, err := s.sendRequestToUA(ctx, reg, out)
	if err != nil || respB == nil {
		s.relay.CloseSession(callID)
		_ = s.pool.UpdateCDREnded(ctx, cdrID, "timeout")
		cleanup()
		return sipResponse(m, 504, "Server Timeout")
	}
	if respB.StatusCode >= 300 {
		s.relay.CloseSession(callID)
		_ = s.pool.UpdateCDREnded(ctx, cdrID, "sip-error")
		cleanup()
		return sipResponse(m, respB.StatusCode, respB.Reason)
	}
	if respB.StatusCode == 200 {
		_ = s.pool.UpdateCDRAnswered(ctx, cdrID)
		ack := &sip.Message{
			StartLine: sip.StartLine{IsRequest: true, Method: "ACK", RequestURI: reg.ContactURI, Proto: "SIP/2.0"},
			Headers:   sip.HeaderMap{},
		}
		ack.AddHeader("Via", s.sipViaUDP())
		ack.AddHeader("Max-Forwards", "70")
		ack.AddHeader("From", m.Headers.Get("from"))
		if t := respB.Headers.Get("to"); t != "" {
			ack.AddHeader("To", t)
		}
		ack.AddHeader("Call-ID", callID+"-b")
		ack.AddHeader("CSeq", strings.Fields(m.Headers.Get("cseq"))[0]+" ACK")
		ack.AddHeader("Content-Length", "0")
		_ = s.sendRequestFireAndForget(reg, ack)
	}

	var callerReg *db.Registration
	if cr, err := s.pool.GetRegistration(ctx, from); err == nil {
		callerReg = cr
	}
	br := &callBridge{
		LegACallID:    callID,
		LegBCallID:    callID + "-b",
		CallerWS:      ws,
		CalleeWS:      calleeWSSession(s, reg),
		fromExt:       from,
		toExt:         to,
		cdrID:         cdrID,
		relayPorts:    [2]int{rtpA, rtpB},
		webrtcCleanup: cleanup,
		callerReg:     callerReg,
	}
	s.callMu.Lock()
	s.calls[callID] = br
	s.calls[callID+"-b"] = br
	s.callMu.Unlock()

	resp := sipResponse(m, respB.StatusCode, respB.Reason)
	if t := respB.Headers.Get("to"); t != "" {
		resp.Headers.Set("to", t)
	}
	respBody, _ := json.Marshal(webrtcInviteBody{Type: "answer", SDP: answerSDP})
	resp.Body = respBody
	resp.AddHeader("Content-Type", "application/json")
	resp.AddHeader("Content-Length", strconv.Itoa(len(resp.Body)))
	return resp
}
