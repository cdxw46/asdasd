package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sdp"
	"github.com/smurf/pbx/internal/sip"
	"github.com/smurf/pbx/internal/voicemail"
	"github.com/smurf/pbx/internal/webhook"
	"github.com/smurf/pbx/internal/wssip"
)

func parseVoicemailDeposit(to string) (ext string, ok bool) {
	if !strings.HasPrefix(to, "*") {
		return "", false
	}
	d := strings.TrimPrefix(to, "*")
	if len(d) < 1 || len(d) > 8 {
		return "", false
	}
	for _, c := range d {
		if c < '0' || c > '9' {
			return "", false
		}
	}
	return d, true
}

func (s *Server) handleInviteVoicemailDeposit(ctx context.Context, ws *wssip.Session, m *sip.Message, transport, from, mailbox, callID string, callerReg *db.Registration) *sip.Message {
	if ws != nil && isWebRTCJSONInvite(m) {
		return sipResponse(m, 501, "Voicemail WebRTC Not Implemented")
	}
	if _, err := s.pool.GetExtension(ctx, mailbox); err != nil {
		return sipResponse(m, 404, "Unknown Mailbox")
	}
	vmDir := getenv("SMURF_VOICEMAIL_DIR", "/var/lib/smurf/voicemail")
	if err := os.MkdirAll(vmDir, 0755); err != nil {
		log.Printf("vm mkdir: %v", err)
		return sipResponse(m, 500, "VM Error")
	}

	rec, err := voicemail.NewDepositRecorder(s.relayBind, vmDir)
	if err != nil {
		log.Printf("vm recorder: %v", err)
		return sipResponse(m, 500, "VM Error")
	}

	rtpA, rtpB, err := s.relay.OpenSession(callID)
	if err != nil {
		_ = rec.Close()
		return sipResponse(m, 500, "Relay Error")
	}
	cdrID, _ := s.pool.InsertCDR(ctx, callID, from, "*"+mailbox, "voicemail-deposit")

	offer := sdp.PatchMediaEndpoint(string(m.Body), s.publicIP, rtpB)
	resp200 := sipResponse(m, 200, "OK")
	resp200.Headers.Set("to", m.Headers.Get("to")+";tag="+sip.RandomNonce()[:8])
	resp200.AddHeader("Contact", s.outboundContact("vm", transport))
	resp200.Body = []byte(offer)
	resp200.AddHeader("Content-Type", "application/sdp")
	resp200.AddHeader("Content-Length", strconv.Itoa(len(resp200.Body)))

	br := &callBridge{
		LegACallID:         callID,
		LegBCallID:         callID + "-vm",
		CallerWS:           ws,
		fromExt:            from,
		toExt:              "*" + mailbox,
		cdrID:              cdrID,
		relayPorts:         [2]int{rtpA, rtpB},
		callerReg:          callerReg,
		voicemailRecorder:  rec,
		voicemailMailbox:   mailbox,
		voicemailCallerExt: from,
	}
	s.callMu.Lock()
	s.calls[callID] = br
	s.calls[br.LegBCallID] = br
	s.callMu.Unlock()

	_ = s.pool.UpdateCDRAnswered(ctx, cdrID)
	go webhook.NotifyAnswered(context.Background(), s.pool, cdrID)
	return resp200
}

func (s *Server) finalizeVoicemailDeposit(br *callBridge) {
	if br == nil || br.voicemailRecorder == nil {
		return
	}
	path := br.voicemailRecorder.Path()
	_ = br.voicemailRecorder.Close()
	br.voicemailRecorder = nil
	fi, err := os.Stat(path)
	durMs := 0
	if err == nil && fi.Size() > 44 {
		samples := (fi.Size() - 44) / 2
		durMs = int(samples * 1000 / 8000)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.pool.InsertVoicemailMessage(ctx, br.voicemailMailbox, br.voicemailCallerExt, path, durMs); err != nil {
		log.Printf("vm db: %v", err)
	}
	n, _ := s.pool.CountVoicemailMessages(ctx, br.voicemailMailbox)
	go s.sendMWINotify(br.voicemailMailbox, n)
}

func (s *Server) sendMWINotify(mailbox string, newCount int) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	reg, err := s.pool.GetRegistration(ctx, mailbox)
	if err != nil {
		return
	}
	body := fmt.Sprintf("Messages-Waiting: yes\r\nVoice-Message: %d/0\r\n", newCount)
	nfy := &sip.Message{
		StartLine: sip.StartLine{IsRequest: true, Method: "NOTIFY", RequestURI: reg.ContactURI, Proto: "SIP/2.0"},
	}
	nfy.AddHeader("Via", s.sipViaUDP())
	nfy.AddHeader("Max-Forwards", "70")
	nfy.AddHeader("From", fmt.Sprintf("<sip:smurf@%s>;tag=%s", s.realm, sip.RandomNonce()[:8]))
	nfy.AddHeader("To", fmt.Sprintf("<sip:%s@%s>", mailbox, s.realm))
	nfy.AddHeader("Call-ID", "mwi-"+sip.RandomNonce()[:12])
	nfy.AddHeader("CSeq", "1 NOTIFY")
	nfy.AddHeader("Event", "message-summary")
	nfy.AddHeader("Subscription-State", "terminated;reason=noresource")
	nfy.AddHeader("Content-Type", "application/simple-message-summary")
	nfy.AddHeader("Content-Length", strconv.Itoa(len(body)))
	nfy.Body = []byte(body)
	_ = s.sendRequestFireAndForget(reg, nfy)
}
