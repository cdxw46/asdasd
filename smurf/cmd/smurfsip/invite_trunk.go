package main

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sdp"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/trunk"
	"github.com/smurf/pbx/internal/webhook"
	"github.com/smurf/pbx/internal/wssip"
)

func isE164ish(s string) bool {
	t := strings.TrimSpace(s)
	t = strings.TrimPrefix(t, "+")
	if len(t) < 8 {
		return false
	}
	for _, c := range t {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func (s *Server) handleInviteThroughTrunks(ctx context.Context, ws *wssip.Session, m *sip.Message, transport, from, dest string, callID string, callerReg *db.Registration) *sip.Message {
	if ws != nil && isWebRTCJSONInvite(m) {
		return sipResponse(m, 501, "Trunk WebRTC Not Implemented")
	}
	trunks, err := s.pool.ListEnabledTrunks(ctx)
	if err != nil || len(trunks) == 0 {
		return sipResponse(m, 503, "No Trunk")
	}
	rtpA, rtpB, err := s.relay.OpenSession(callID)
	if err != nil {
		return sipResponse(m, 500, "Relay Error")
	}
	cdrID, _ := s.pool.InsertCDR(ctx, callID, from, dest, "trunk-out")
	offerToTrunk := sdp.PatchMediaEndpoint(string(m.Body), s.publicIP, rtpB)

	var last *sip.Message
	var used *db.SIPTrunk
	var dlg *trunk.Dialog

	for i := range trunks {
		t := &trunks[i]
		reqURI := fmt.Sprintf("sip:%s@%s", strings.TrimPrefix(dest, "+"), t.SipHost)
		if strings.Contains(dest, "@") {
			reqURI = "sip:" + dest
		}
		ir, err := trunk.Invite(ctx, t, s.realm, s.publicIP, s.sipPort, reqURI, offerToTrunk)
		if err != nil || ir == nil || ir.Response == nil {
			continue
		}
		if ir.Response.StatusCode >= 200 && ir.Response.StatusCode < 300 && ir.Dialog != nil {
			used = t
			last = ir.Response
			dlg = ir.Dialog
			break
		}
		last = ir.Response
	}
	if used == nil || last == nil || dlg == nil || last.StatusCode < 200 || last.StatusCode >= 300 {
		s.relay.CloseSession(callID)
		if cdrID > 0 {
			_ = s.pool.UpdateCDREnded(ctx, cdrID, "trunk-fail")
			go webhook.NotifyEnded(context.Background(), s.pool, cdrID)
		}
		if last != nil {
			return sipResponse(m, last.StatusCode, last.Reason)
		}
		return sipResponse(m, 503, "Trunk Unavailable")
	}

	_ = trunk.SendACK(used, dlg, s.publicIP, s.sipPort)

	_ = s.pool.UpdateCDRAnswered(ctx, cdrID)
	go webhook.NotifyAnswered(context.Background(), s.pool, cdrID)

	br := &callBridge{
		LegACallID:    callID,
		LegBCallID:    callID + "-trunk",
		CallerWS:      ws,
		fromExt:       from,
		toExt:         dest,
		cdrID:         cdrID,
		relayPorts:    [2]int{rtpA, rtpB},
		callerReg:     callerReg,
		outboundTrunk: used,
		trunkDialog:   dlg,
	}
	s.callMu.Lock()
	s.calls[callID] = br
	s.calls[br.LegBCallID] = br
	s.callMu.Unlock()

	answerToCaller := sdp.PatchMediaEndpoint(string(last.Body), s.publicIP, rtpA)
	resp := sipResponse(m, last.StatusCode, last.Reason)
	if t := last.Headers.Get("to"); t != "" {
		resp.Headers.Set("to", t)
	}
	resp.Body = []byte(answerToCaller)
	resp.AddHeader("Content-Type", "application/sdp")
	resp.AddHeader("Content-Length", strconv.Itoa(len(resp.Body)))
	return resp
}
