package trunk

import (
	"bytes"
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/smurf/pbx/internal/db"
	"github.com/smurf/pbx/internal/sip"
)

// Dialog holds SIP dialog identifiers after a successful INVITE.
type Dialog struct {
	RequestURI string // Request-URI for in-dialog requests (ACK/BYE)
	CallID     string
	From       string // full From header value
	To         string // full To header value (with remote tag)
	CSeq       int    // last INVITE CSeq number
}

// InviteResult is the outcome of an outbound INVITE.
type InviteResult struct {
	Response *sip.Message
	Dialog   *Dialog
}

func registerRealm(regURI, sipHost string) string {
	regURI = strings.TrimSpace(regURI)
	if strings.HasPrefix(strings.ToLower(regURI), "sip:") {
		u := strings.TrimPrefix(regURI, "sip:")
		u = strings.TrimPrefix(u, "SIP:")
		if idx := strings.Index(u, "@"); idx >= 0 && idx+1 < len(u) {
			return u[idx+1:]
		}
		if i := strings.Index(u, ";"); i >= 0 {
			u = u[:i]
		}
		if u != "" {
			return u
		}
	}
	return sipHost
}

// Register performs SIP REGISTER toward the trunk provider (digest if challenged).
func Register(ctx context.Context, t *db.SIPTrunk, _ string, publicIP string, sipPort int) error {
	regURI := t.RegisterURI
	if regURI == "" {
		regURI = fmt.Sprintf("sip:%s", t.SipHost)
	}
	realm := registerRealm(regURI, t.SipHost)
	fromUser := t.FromUser
	if fromUser == "" {
		fromUser = t.AuthUsername
	}
	if fromUser == "" {
		fromUser = t.ContactUser
	}
	contactUser := t.ContactUser
	if contactUser == "" {
		contactUser = fromUser
	}
	callID := sip.RandomNonce()
	cseq := 1
	branch := "z9hG4bK" + sip.RandomNonce()

	msg := buildRegister(t, regURI, realm, publicIP, sipPort, fromUser, contactUser, callID, cseq, branch, "")
	resp, err := roundTrip(ctx, t, msg, 12*time.Second)
	if err != nil {
		return err
	}
	if resp.StatusCode == 401 || resp.StatusCode == 407 {
		var ch string
		if resp.StatusCode == 401 {
			ch = resp.Headers.Get("www-authenticate")
		} else {
			ch = resp.Headers.Get("proxy-authenticate")
		}
		params := sip.ParseDigestHeader(ch)
		digestURI := regURI
		auth := sip.BuildAuthorizationHeader(t.AuthUsername, t.AuthPassword, "REGISTER", digestURI, "", params)
		cseq++
		branch = "z9hG4bK" + sip.RandomNonce()
		msg2 := buildRegister(t, regURI, realm, publicIP, sipPort, fromUser, contactUser, callID, cseq, branch, auth)
		resp2, err := roundTrip(ctx, t, msg2, 12*time.Second)
		if err != nil {
			return err
		}
		if resp2.StatusCode < 200 || resp2.StatusCode >= 300 {
			return fmt.Errorf("register status %d %s", resp2.StatusCode, resp2.Reason)
		}
		return nil
	}
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	return fmt.Errorf("register status %d %s", resp.StatusCode, resp.Reason)
}

func buildRegister(t *db.SIPTrunk, regURI, realm, publicIP string, sipPort int, fromUser, contactUser, callID string, cseq int, branch, auth string) string {
	to := fmt.Sprintf("<sip:%s@%s>", fromUser, realm)
	from := to + ";tag=" + sip.RandomNonce()[:8]
	contact := fmt.Sprintf("<sip:%s@%s:%d>", contactUser, publicIP, sipPort)
	var b strings.Builder
	fmt.Fprintf(&b, "REGISTER %s SIP/2.0\r\n", regURI)
	fmt.Fprintf(&b, "Via: SIP/2.0/UDP %s:%d;branch=%s\r\n", publicIP, sipPort, branch)
	b.WriteString("Max-Forwards: 70\r\n")
	fmt.Fprintf(&b, "From: %s\r\n", from)
	fmt.Fprintf(&b, "To: %s\r\n", to)
	fmt.Fprintf(&b, "Call-ID: %s\r\n", callID)
	fmt.Fprintf(&b, "CSeq: %d REGISTER\r\n", cseq)
	fmt.Fprintf(&b, "Contact: %s\r\n", contact)
	b.WriteString("Expires: 3600\r\n")
	if auth != "" {
		fmt.Fprintf(&b, "Authorization: %s\r\n", auth)
	}
	b.WriteString("Content-Length: 0\r\n\r\n")
	return b.String()
}

// Invite sends INVITE with SDP; returns 2xx response and dialog fields for ACK/BYE.
func Invite(ctx context.Context, t *db.SIPTrunk, realm, publicIP string, sipPort int, requestURI, sdpOffer string) (*InviteResult, error) {
	fromUser := t.FromUser
	if fromUser == "" {
		fromUser = t.AuthUsername
	}
	if fromUser == "" {
		fromUser = t.ContactUser
	}
	callID := sip.RandomNonce()
	cseq := 1
	branch := "z9hG4bK" + sip.RandomNonce()
	fromTag := sip.RandomNonce()[:8]
	to := fmt.Sprintf("<%s>", requestURI)
	from := fmt.Sprintf("<sip:%s@%s>;tag=%s", fromUser, realm, fromTag)
	contact := fmt.Sprintf("<sip:%s@%s:%d>", fromUser, publicIP, sipPort)

	build := func(auth string) string {
		var b strings.Builder
		fmt.Fprintf(&b, "INVITE %s SIP/2.0\r\n", requestURI)
		fmt.Fprintf(&b, "Via: SIP/2.0/UDP %s:%d;branch=%s\r\n", publicIP, sipPort, branch)
		b.WriteString("Max-Forwards: 70\r\n")
		fmt.Fprintf(&b, "From: %s\r\n", from)
		fmt.Fprintf(&b, "To: %s\r\n", to)
		fmt.Fprintf(&b, "Call-ID: %s\r\n", callID)
		fmt.Fprintf(&b, "CSeq: %d INVITE\r\n", cseq)
		fmt.Fprintf(&b, "Contact: %s\r\n", contact)
		if auth != "" {
			fmt.Fprintf(&b, "Proxy-Authorization: %s\r\n", auth)
		}
		b.WriteString("Content-Type: application/sdp\r\n")
		fmt.Fprintf(&b, "Content-Length: %d\r\n\r\n", len(sdpOffer))
		b.WriteString(sdpOffer)
		return b.String()
	}

	final, err := inviteUDPReadLoop(ctx, t, build(""), 45*time.Second)
	if err != nil {
		return nil, err
	}
	if final.StatusCode == 401 || final.StatusCode == 407 {
		var ch string
		if final.StatusCode == 401 {
			ch = final.Headers.Get("www-authenticate")
		} else {
			ch = final.Headers.Get("proxy-authenticate")
		}
		params := sip.ParseDigestHeader(ch)
		auth := sip.BuildAuthorizationHeader(t.AuthUsername, t.AuthPassword, "INVITE", requestURI, sdpOffer, params)
		cseq++
		branch = "z9hG4bK" + sip.RandomNonce()
		final, err = inviteUDPReadLoop(ctx, t, build(auth), 45*time.Second)
		if err != nil {
			return nil, err
		}
	}
	if final.StatusCode < 200 || final.StatusCode >= 300 {
		return &InviteResult{Response: final}, nil
	}
	ackURI := sipContactURI(final)
	if ackURI == "" {
		ackURI = requestURI
	}
	dlg := &Dialog{
		RequestURI: ackURI,
		CallID:     callID,
		From:       from,
		To:         final.Headers.Get("to"),
		CSeq:       cseq,
	}
	return &InviteResult{Response: final, Dialog: dlg}, nil
}

func sipContactURI(resp *sip.Message) string {
	c := resp.Headers.Get("contact")
	if c == "" {
		return ""
	}
	a := strings.Index(c, "<")
	b := strings.Index(c, ">")
	if a >= 0 && b > a {
		return strings.TrimSpace(c[a+1 : b])
	}
	p := strings.Fields(c)
	if len(p) > 0 {
		return strings.TrimSpace(p[0])
	}
	return ""
}

func inviteUDPReadLoop(ctx context.Context, t *db.SIPTrunk, payload string, total time.Duration) (*sip.Message, error) {
	addr := net.JoinHostPort(t.SipHost, strconv.Itoa(t.SipPort))
	d := net.Dialer{Timeout: 8 * time.Second}
	c, err := d.DialContext(ctx, "udp", addr)
	if err != nil {
		return nil, err
	}
	defer c.Close()
	if _, err := io.WriteString(c, payload); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(total)
	buf := make([]byte, 0, 65536)
	tmp := make([]byte, 4096)
	var last *sip.Message
	for {
		if time.Now().After(deadline) {
			if last != nil {
				return last, nil
			}
			return nil, fmt.Errorf("invite timeout")
		}
		_ = c.SetReadDeadline(time.Now().Add(2 * time.Second))
		n, err := c.Read(tmp)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				if last != nil && last.StatusCode >= 200 {
					return last, nil
				}
				continue
			}
			return nil, err
		}
		buf = append(buf, tmp[:n]...)
		for {
			idx := bytes.Index(buf, []byte("\r\n\r\n"))
			if idx < 0 {
				break
			}
			head := string(buf[:idx])
			cl := contentLengthFromHead(head)
			if len(buf) < idx+4+cl {
				break
			}
			frame := append([]byte(nil), buf[:idx+4+cl]...)
			buf = buf[idx+4+cl:]
			msg, err := sip.ParseMessage(frame)
			if err != nil {
				return nil, err
			}
			if !msg.IsRequest {
				last = msg
				if msg.StatusCode >= 200 {
					return msg, nil
				}
			}
		}
	}
}

func roundTrip(ctx context.Context, t *db.SIPTrunk, payload string, readTimeout time.Duration) (*sip.Message, error) {
	addr := net.JoinHostPort(t.SipHost, strconv.Itoa(t.SipPort))
	d := net.Dialer{Timeout: 8 * time.Second}
	var c net.Conn
	var err error
	switch t.Transport {
	case "tcp":
		c, err = d.DialContext(ctx, "tcp", addr)
	case "tls":
		raw, e := d.DialContext(ctx, "tcp", addr)
		if e != nil {
			return nil, e
		}
		c = tls.Client(raw, &tls.Config{InsecureSkipVerify: true})
	default:
		c, err = d.DialContext(ctx, "udp", addr)
	}
	if err != nil {
		return nil, err
	}
	defer c.Close()
	if _, err := io.WriteString(c, payload); err != nil {
		return nil, err
	}
	end := time.Now().Add(readTimeout)
	buf := make([]byte, 0, 65536)
	tmp := make([]byte, 4096)
	for {
		_ = c.SetReadDeadline(end)
		n, err := c.Read(tmp)
		if err != nil {
			return nil, err
		}
		buf = append(buf, tmp[:n]...)
		for {
			idx := bytes.Index(buf, []byte("\r\n\r\n"))
			if idx < 0 {
				break
			}
			head := string(buf[:idx])
			cl := contentLengthFromHead(head)
			if len(buf) < idx+4+cl {
				break
			}
			frame := append([]byte(nil), buf[:idx+4+cl]...)
			buf = buf[idx+4+cl:]
			return sip.ParseMessage(frame)
		}
	}
}

// SendACK sends ACK for established INVITE dialog.
func SendACK(t *db.SIPTrunk, dlg *Dialog, publicIP string, sipPort int) error {
	if dlg == nil {
		return fmt.Errorf("no dialog")
	}
	var b strings.Builder
	fmt.Fprintf(&b, "ACK %s SIP/2.0\r\n", dlg.RequestURI)
	fmt.Fprintf(&b, "Via: SIP/2.0/UDP %s:%d;branch=%s\r\n", publicIP, sipPort, "z9hG4bK"+sip.RandomNonce())
	b.WriteString("Max-Forwards: 70\r\n")
	fmt.Fprintf(&b, "From: %s\r\n", dlg.From)
	fmt.Fprintf(&b, "To: %s\r\n", dlg.To)
	fmt.Fprintf(&b, "Call-ID: %s\r\n", dlg.CallID)
	fmt.Fprintf(&b, "CSeq: %d ACK\r\n", dlg.CSeq)
	b.WriteString("Content-Length: 0\r\n\r\n")
	return sendRaw(t, b.String())
}

// SendBYE sends BYE on dialog.
func SendBYE(t *db.SIPTrunk, dlg *Dialog, publicIP string, sipPort int) error {
	if dlg == nil {
		return fmt.Errorf("no dialog")
	}
	var b strings.Builder
	fmt.Fprintf(&b, "BYE %s SIP/2.0\r\n", dlg.RequestURI)
	fmt.Fprintf(&b, "Via: SIP/2.0/UDP %s:%d;branch=%s\r\n", publicIP, sipPort, "z9hG4bK"+sip.RandomNonce())
	b.WriteString("Max-Forwards: 70\r\n")
	fmt.Fprintf(&b, "From: %s\r\n", dlg.From)
	fmt.Fprintf(&b, "To: %s\r\n", dlg.To)
	fmt.Fprintf(&b, "Call-ID: %s\r\n", dlg.CallID)
	fmt.Fprintf(&b, "CSeq: %d BYE\r\n", dlg.CSeq+1)
	b.WriteString("Content-Length: 0\r\n\r\n")
	return sendRaw(t, b.String())
}

func sendRaw(t *db.SIPTrunk, payload string) error {
	addr := net.JoinHostPort(t.SipHost, strconv.Itoa(t.SipPort))
	var c net.Conn
	var err error
	d := net.Dialer{Timeout: 5 * time.Second}
	switch t.Transport {
	case "tcp":
		c, err = d.Dial("tcp", addr)
	case "tls":
		raw, e := d.Dial("tcp", addr)
		if e != nil {
			return e
		}
		c = tls.Client(raw, &tls.Config{InsecureSkipVerify: true})
	default:
		c, err = d.Dial("udp", addr)
	}
	if err != nil {
		return err
	}
	defer c.Close()
	_, err = io.WriteString(c, payload)
	return err
}

func contentLengthFromHead(head string) int {
	for _, ln := range strings.Split(head, "\r\n") {
		l := strings.ToLower(ln)
		if strings.HasPrefix(l, "content-length:") {
			colon := strings.IndexByte(ln, ':')
			if colon < 0 {
				continue
			}
			v := strings.TrimSpace(ln[colon+1:])
			n, err := strconv.Atoi(v)
			if err != nil {
				return 0
			}
			return n
		}
	}
	return 0
}
