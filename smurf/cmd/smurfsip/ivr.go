package main

import (
	"context"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sdp"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/voicemail"
	"github.com/smurf/pbx/internal/wavplay"
	"github.com/smurf/pbx/internal/webhook"
	"github.com/smurf/pbx/internal/wssip"
)

func (s *Server) handleInviteIVR(ctx context.Context, ws *wssip.Session, m *sip.Message, transport, from, menuSlug, callID string, callerReg *db.Registration) *sip.Message {
	if ws != nil && isWebRTCJSONInvite(m) {
		return sipResponse(m, 501, "IVR WebRTC Not Implemented")
	}
	menu, err := s.pool.GetIVRMenu(ctx, menuSlug)
	if err != nil {
		return sipResponse(m, 404, "IVR Not Found")
	}
	if _, err := os.Stat(menu.WelcomeFile); err != nil {
		log.Printf("ivr welcome missing %s: %v", menu.WelcomeFile, err)
		return sipResponse(m, 503, "IVR Media Missing")
	}

	var rec *voicemail.DepositRecorder
	tapAddr := ""
	if ce, err := s.pool.GetExtension(ctx, from); err == nil && ce.RecordCalls {
		recDir := getenv("SMURF_RECORDINGS_DIR", "/var/lib/smurf/recordings")
		_ = os.MkdirAll(recDir, 0755)
		p := filepath.Join(recDir, fmt.Sprintf("rec-%s-%d.wav", callID[:12], time.Now().UnixNano()))
		r, err := voicemail.NewPCMURecorder(s.relayBind, p)
		if err == nil {
			rec = r
			host := "127.0.0.1"
			if ip := net.ParseIP(s.relayBind); ip != nil && !ip.IsUnspecified() {
				host = ip.String()
			}
			tapAddr = net.JoinHostPort(host, strconv.Itoa(rec.LocalPort()))
		}
	}

	var rtpA, rtpB int
	var err2 error
	if tapAddr != "" {
		rtpA, rtpB, err2 = s.relay.OpenSessionWithTap(callID, tapAddr)
	} else {
		rtpA, rtpB, err2 = s.relay.OpenSession(callID)
	}
	if err2 != nil {
		if rec != nil {
			_ = rec.Close()
		}
		return sipResponse(m, 500, "Relay Error")
	}
	cdrID, _ := s.pool.InsertCDR(ctx, callID, from, "ivr:"+menuSlug, "ivr")

	offer := sdp.PatchMediaEndpoint(string(m.Body), s.publicIP, rtpB)
	resp := sipResponse(m, 200, "OK")
	resp.Headers.Set("to", m.Headers.Get("to")+";tag="+sip.RandomNonce()[:8])
	resp.AddHeader("Contact", s.outboundContact("ivr", transport))
	resp.Body = []byte(offer)
	resp.AddHeader("Content-Type", "application/sdp")
	resp.AddHeader("Content-Length", strconv.Itoa(len(resp.Body)))

	stop := make(chan struct{})
	br := &callBridge{
		LegACallID:     callID,
		LegBCallID:     callID + "-ivr",
		CallerWS:       ws,
		fromExt:        from,
		toExt:          "ivr:" + menuSlug,
		cdrID:          cdrID,
		relayPorts:     [2]int{rtpA, rtpB},
		callerReg:      callerReg,
		ivrMenuSlug:    menuSlug,
		ivrWelcomeStop: stop,
		callRecording:  rec,
	}
	s.callMu.Lock()
	s.calls[callID] = br
	s.calls[br.LegBCallID] = br
	s.callMu.Unlock()

	_ = s.pool.UpdateCDRAnswered(ctx, cdrID)
	go webhook.NotifyAnswered(context.Background(), s.pool, cdrID)

	cIP, cPort := sdp.ParseRemoteMediaAddr(string(m.Body))
	if cIP != "" && cPort > 0 {
		go func() {
			time.Sleep(250 * time.Millisecond)
			_ = wavplay.StreamWAVPCMU(menu.WelcomeFile, s.relayBind, cIP, cPort, stop)
		}()
	}

	return resp
}

func (s *Server) handleINFO(ctx context.Context, ws *wssip.Session, m *sip.Message, transport string) *sip.Message {
	br := s.bridgeForCallID(m.Headers.Get("call-id"))
	if br == nil || br.ivrMenuSlug == "" {
		return sipResponse(m, 481, "Call Does Not Exist")
	}
	if br.callerReg == nil {
		return sipResponse(m, 500, "No Route")
	}
	digit := strings.TrimSpace(string(m.Body))
	if len(digit) != 1 {
		return sipResponse(m, 400, "Bad DTMF")
	}
	action, err := s.pool.GetIVROption(ctx, br.ivrMenuSlug, digit)
	if err != nil {
		return sipResponse(m, 404, "Invalid Option")
	}
	select {
	case <-br.ivrWelcomeStop:
	default:
		close(br.ivrWelcomeStop)
	}

	target := action
	if strings.HasPrefix(action, "queue:") {
		target = strings.TrimPrefix(action, "queue:")
	}
	ref := fmt.Sprintf("<sip:%s@%s>", target, s.realm)
	refReq := &sip.Message{
		StartLine: sip.StartLine{IsRequest: true, Method: "REFER", RequestURI: br.callerReg.ContactURI, Proto: "SIP/2.0"},
	}
	refReq.AddHeader("Via", s.sipViaUDP())
	refReq.AddHeader("Max-Forwards", "70")
	refReq.AddHeader("From", fmt.Sprintf("<sip:ivr@%s>", s.realm))
	refReq.AddHeader("To", fmt.Sprintf("<sip:%s@%s>", br.fromExt, s.realm))
	refReq.AddHeader("Call-ID", sip.RandomNonce())
	refReq.AddHeader("CSeq", "1 REFER")
	refReq.AddHeader("Refer-To", ref)
	refReq.AddHeader("Referred-By", fmt.Sprintf("<sip:%s@%s>", br.fromExt, s.realm))
	refReq.AddHeader("Contact", s.outboundContact("ivr", transport))
	refReq.AddHeader("Content-Length", "0")
	_ = s.sendRequestFireAndForget(br.callerReg, refReq)
	return sipResponse(m, 200, "OK")
}

func (s *Server) handleNOTIFY(ctx context.Context, m *sip.Message, transport string) *sip.Message {
	return sipResponse(m, 200, "OK")
}
