package main

import (
	"context"
	"net"
	"strconv"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/trunk"
	"github.com/smurf/pbx/internal/webhook"
)

func (s *Server) bridgeForCallID(callID string) *callBridge {
	s.callMu.Lock()
	defer s.callMu.Unlock()
	if br, ok := s.calls[callID]; ok {
		return br
	}
	return nil
}

func (s *Server) forwardMidDialogNoResponse(ctx context.Context, m *sip.Message, transport string) {
	br := s.bridgeForCallID(m.Headers.Get("call-id"))
	if br == nil {
		return
	}
	if m.Method == "ACK" && br.voicemailRecorder != nil {
		return
	}
	cid := m.Headers.Get("call-id")
	var remote *db.Registration
	var outCallID string
	if cid == br.LegACallID {
		reg, err := s.calleeRegistrationForBridge(ctx, br)
		if err != nil {
			return
		}
		remote = reg
		outCallID = br.LegBCallID
	} else if cid == br.LegBCallID {
		if br.callerReg == nil {
			return
		}
		remote = br.callerReg
		outCallID = br.LegACallID
	} else {
		return
	}
	out := cloneRequest(m)
	out.RequestURI = remote.ContactURI
	out.Headers = sip.HeaderMap{}
	out.AddHeader("Via", s.sipViaUDP())
	out.AddHeader("Max-Forwards", "70")
	out.AddHeader("From", m.Headers.Get("from"))
	out.AddHeader("To", m.Headers.Get("to"))
	out.AddHeader("Call-ID", outCallID)
	out.AddHeader("CSeq", m.Headers.Get("cseq"))
	if len(m.Body) > 0 {
		out.Body = append([]byte(nil), m.Body...)
		out.AddHeader("Content-Type", m.Headers.Get("content-type"))
		out.AddHeader("Content-Length", strconv.Itoa(len(out.Body)))
	} else {
		out.AddHeader("Content-Length", "0")
	}
	_ = s.sendRequestFireAndForget(remote, out)
}

func (s *Server) handleMidDialog(ctx context.Context, m *sip.Message, _ string, _ string, _ *net.UDPAddr, _ *net.UDPConn, _ net.Conn) *sip.Message {
	br := s.bridgeForCallID(m.Headers.Get("call-id"))
	if br == nil {
		return sipResponse(m, 481, "Call/Transaction Does Not Exist")
	}
	cid := m.Headers.Get("call-id")
	if (m.Method == "BYE" || m.Method == "CANCEL") && br.ivrMenuSlug != "" && br.ivrWelcomeStop != nil {
		select {
		case <-br.ivrWelcomeStop:
		default:
			close(br.ivrWelcomeStop)
		}
	}
	if (m.Method == "BYE" || m.Method == "CANCEL") && br.queueMohStop != nil {
		select {
		case <-br.queueMohStop:
		default:
			close(br.queueMohStop)
		}
	}
	if (m.Method == "BYE" || m.Method == "CANCEL") && br.voicemailRecorder != nil {
		s.finalizeVoicemailDeposit(br)
		s.tearDownCall(br, br.LegACallID)
		_ = s.pool.UpdateCDREnded(ctx, br.cdrID, m.Method)
		go webhook.NotifyEnded(context.Background(), s.pool, br.cdrID)
		return sipResponse(m, 200, "OK")
	}
	if (m.Method == "BYE" || m.Method == "CANCEL") && cid == br.LegACallID && br.outboundTrunk != nil && br.trunkDialog != nil {
		_ = trunk.SendBYE(br.outboundTrunk, br.trunkDialog, s.publicIP, s.sipPort)
		s.tearDownCall(br, br.LegACallID)
		_ = s.pool.UpdateCDREnded(ctx, br.cdrID, m.Method)
		go webhook.NotifyEnded(context.Background(), s.pool, br.cdrID)
		return sipResponse(m, 200, "OK")
	}
	if (m.Method == "BYE" || m.Method == "CANCEL") && br.ivrMenuSlug != "" && br.voicemailRecorder == nil {
		s.tearDownCall(br, br.LegACallID)
		_ = s.pool.UpdateCDREnded(ctx, br.cdrID, m.Method)
		go webhook.NotifyEnded(context.Background(), s.pool, br.cdrID)
		return sipResponse(m, 200, "OK")
	}
	var remote *db.Registration
	var outCallID string
	if cid == br.LegACallID {
		reg, err := s.calleeRegistrationForBridge(ctx, br)
		if err != nil {
			return sipResponse(m, 481, "Call/Transaction Does Not Exist")
		}
		remote = reg
		outCallID = br.LegBCallID
	} else if cid == br.LegBCallID {
		if br.callerReg == nil {
			return sipResponse(m, 481, "Call/Transaction Does Not Exist")
		}
		remote = br.callerReg
		outCallID = br.LegACallID
	} else {
		return sipResponse(m, 481, "Call/Transaction Does Not Exist")
	}
	out := cloneRequest(m)
	out.RequestURI = remote.ContactURI
	out.Headers = sip.HeaderMap{}
	out.AddHeader("Via", s.sipViaUDP())
	out.AddHeader("Max-Forwards", "70")
	out.AddHeader("From", m.Headers.Get("from"))
	out.AddHeader("To", m.Headers.Get("to"))
	out.AddHeader("Call-ID", outCallID)
	out.AddHeader("CSeq", m.Headers.Get("cseq"))
	if len(m.Body) > 0 {
		out.Body = append([]byte(nil), m.Body...)
		out.AddHeader("Content-Type", m.Headers.Get("content-type"))
		out.AddHeader("Content-Length", strconv.Itoa(len(out.Body)))
	} else {
		out.AddHeader("Content-Length", "0")
	}
	respB, err := s.sendRequestToUA(ctx, remote, out)
	if err != nil || respB == nil {
		return sipResponse(m, 504, "Server Timeout")
	}
	if m.Method == "BYE" || m.Method == "CANCEL" {
		s.tearDownCall(br, br.LegACallID)
		_ = s.pool.UpdateCDREnded(ctx, br.cdrID, m.Method)
		go webhook.NotifyEnded(context.Background(), s.pool, br.cdrID)
	}
	return sipResponse(m, respB.StatusCode, respB.Reason)
}
