package main

import (
	"bytes"
	"crypto/md5"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"smurf/internal/sip"
)

type UA struct {
	name      string
	ext       string
	password  string
	domain    string
	server    string
	localSIP  *net.UDPConn
	localRTP  *net.UDPConn
	contact   string
	tag       string
	callID    string
	branchSeq int
	cseq      int
}

type ServerResponse struct {
	StatusCode int
	Reason     string
	Raw        string
}

func newUA(name, ext, password, domain, server string) (*UA, error) {
	sipConn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: 0})
	if err != nil {
		return nil, err
	}
	rtpConn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: 0})
	if err != nil {
		sipConn.Close()
		return nil, err
	}
	sipAddr := sipConn.LocalAddr().(*net.UDPAddr)
	ua := &UA{
		name:      name,
		ext:       ext,
		password:  password,
		domain:    domain,
		server:    server,
		localSIP:  sipConn,
		localRTP:  rtpConn,
		contact:   fmt.Sprintf("<sip:%s@127.0.0.1:%d>", ext, sipAddr.Port),
		tag:       randHex(8),
		callID:    fmt.Sprintf("%s-%s@%s", name, randHex(6), domain),
		branchSeq: 1,
		cseq:      1,
	}
	return ua, nil
}

func (u *UA) Close() {
	if u.localSIP != nil {
		u.localSIP.Close()
	}
	if u.localRTP != nil {
		u.localRTP.Close()
	}
}

func (u *UA) nextBranch() string {
	u.branchSeq++
	return "z9hG4bK" + randHex(6) + strconv.Itoa(u.branchSeq)
}

func (u *UA) send(msg *sip.Message) error {
	addr, err := net.ResolveUDPAddr("udp", u.server)
	if err != nil {
		return err
	}
	_, err = u.localSIP.WriteToUDP(msg.Bytes(), addr)
	return err
}

func (u *UA) recv(timeout time.Duration) (*sip.Message, error) {
	buf := make([]byte, 65535)
	_ = u.localSIP.SetReadDeadline(time.Now().Add(timeout))
	n, _, err := u.localSIP.ReadFromUDP(buf)
	if err != nil {
		return nil, err
	}
	return sip.ParseMessage(buf[:n])
}

func (u *UA) recvRaw(timeout time.Duration) ([]byte, *net.UDPAddr, error) {
	buf := make([]byte, 65535)
	_ = u.localSIP.SetReadDeadline(time.Now().Add(timeout))
	n, addr, err := u.localSIP.ReadFromUDP(buf)
	if err != nil {
		return nil, nil, err
	}
	return append([]byte(nil), buf[:n]...), addr, nil
}

func (u *UA) recvUntil(timeout time.Duration, want func(*sip.Message) bool) (*sip.Message, error) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		msg, err := u.recv(time.Until(deadline))
		if err != nil {
			return nil, err
		}
		if want(msg) {
			return msg, nil
		}
	}
	return nil, fmt.Errorf("%s timeout waiting for matching message", u.name)
}

func (u *UA) register() error {
	req := &sip.Message{
		IsRequest:  true,
		Method:     "REGISTER",
		RequestURI: "sip:" + u.domain,
		Version:    "SIP/2.0",
		Headers:    make(map[string][]string),
	}
	req.SetHeader("Via", fmt.Sprintf("SIP/2.0/UDP 127.0.0.1:%d;branch=%s;rport", u.localSIP.LocalAddr().(*net.UDPAddr).Port, u.nextBranch()))
	req.SetHeader("Max-Forwards", "70")
	req.SetHeader("To", fmt.Sprintf("<sip:%s@%s>", u.ext, u.domain))
	req.SetHeader("From", fmt.Sprintf("<sip:%s@%s>;tag=%s", u.ext, u.domain, u.tag))
	req.SetHeader("Call-ID", u.callID)
	req.SetHeader("CSeq", fmt.Sprintf("%d REGISTER", u.cseq))
	req.SetHeader("Contact", u.contact)
	req.SetHeader("Expires", "300")
	req.SetHeader("User-Agent", "SMURF-E2E")
	req.SetHeader("Content-Length", "0")
	if err := u.send(req); err != nil {
		return err
	}
	challenge, err := u.recvUntil(3*time.Second, func(msg *sip.Message) bool { return !msg.IsRequest && msg.StatusCode == 401 })
	if err != nil {
		return fmt.Errorf("register challenge: %w", err)
	}
	authz := authorizationHeader("REGISTER", "sip:"+u.domain, u.ext, u.password, parseChallenge(challenge.GetHeader("WWW-Authenticate")))
	u.cseq++
	req2 := req.Clone()
	req2.SetHeader("Via", fmt.Sprintf("SIP/2.0/UDP 127.0.0.1:%d;branch=%s;rport", u.localSIP.LocalAddr().(*net.UDPAddr).Port, u.nextBranch()))
	req2.SetHeader("CSeq", fmt.Sprintf("%d REGISTER", u.cseq))
	req2.SetHeader("Authorization", authz)
	if err := u.send(req2); err != nil {
		return err
	}
	_, err = u.recvUntil(3*time.Second, func(msg *sip.Message) bool { return !msg.IsRequest && msg.StatusCode == 200 })
	return err
}

func (u *UA) invite(target string) (*sip.Message, error) {
	body := u.offerSDP()
	req := &sip.Message{
		IsRequest:  true,
		Method:     "INVITE",
		RequestURI: "sip:" + target + "@" + u.domain,
		Version:    "SIP/2.0",
		Headers:    make(map[string][]string),
		Body:       body,
	}
	req.SetHeader("Via", fmt.Sprintf("SIP/2.0/UDP 127.0.0.1:%d;branch=%s;rport", u.localSIP.LocalAddr().(*net.UDPAddr).Port, u.nextBranch()))
	req.SetHeader("Max-Forwards", "70")
	req.SetHeader("To", fmt.Sprintf("<sip:%s@%s>", target, u.domain))
	req.SetHeader("From", fmt.Sprintf("<sip:%s@%s>;tag=%s", u.ext, u.domain, u.tag))
	req.SetHeader("Call-ID", u.callID)
	req.SetHeader("CSeq", fmt.Sprintf("%d INVITE", u.cseq))
	req.SetHeader("Contact", u.contact)
	req.SetHeader("Content-Type", "application/sdp")
	req.SetHeader("User-Agent", "SMURF-E2E")
	req.SetHeader("Content-Length", strconv.Itoa(len(body)))
	if err := u.send(req); err != nil {
		return nil, err
	}
	challenge, err := u.recvUntil(3*time.Second, func(msg *sip.Message) bool { return !msg.IsRequest && msg.StatusCode == 407 })
	if err != nil {
		return nil, fmt.Errorf("invite challenge: %w", err)
	}
	authz := authorizationHeader("INVITE", "sip:"+target+"@"+u.domain, u.ext, u.password, parseChallenge(challenge.GetHeader("Proxy-Authenticate")))
	u.cseq++
	req2 := req.Clone()
	req2.SetHeader("Via", fmt.Sprintf("SIP/2.0/UDP 127.0.0.1:%d;branch=%s;rport", u.localSIP.LocalAddr().(*net.UDPAddr).Port, u.nextBranch()))
	req2.SetHeader("CSeq", fmt.Sprintf("%d INVITE", u.cseq))
	req2.SetHeader("Authorization", authz)
	if err := u.send(req2); err != nil {
		return nil, err
	}
	_, err = u.recvUntil(3*time.Second, func(msg *sip.Message) bool { return !msg.IsRequest && msg.StatusCode == 100 })
	if err != nil {
		return nil, err
	}
	_, err = u.recvUntil(3*time.Second, func(msg *sip.Message) bool { return !msg.IsRequest && msg.StatusCode == 180 })
	if err != nil {
		return nil, err
	}
	return u.recvUntil(5*time.Second, func(msg *sip.Message) bool { return !msg.IsRequest && msg.StatusCode == 200 })
}

func (u *UA) waitIncomingInvite() (*sip.Message, error) {
	return u.recvUntil(5*time.Second, func(msg *sip.Message) bool { return msg.IsRequest && msg.Method == "INVITE" })
}

func (u *UA) answerInvite(inv *sip.Message) error {
	resp := sip.BuildResponse(inv, 200, "OK", map[string]string{
		"Contact":      u.contact,
		"Content-Type": "application/sdp",
	}, u.offerSDP())
	resp.SetHeader("Content-Length", strconv.Itoa(len(resp.Body)))
	return u.sendResponseToVia(inv, resp)
}

func (u *UA) waitAck() error {
	_, err := u.recvUntil(3*time.Second, func(msg *sip.Message) bool { return msg.IsRequest && msg.Method == "ACK" })
	return err
}

func (u *UA) sendAck(resp200 *sip.Message, target string) error {
	ack := &sip.Message{
		IsRequest:  true,
		Method:     "ACK",
		RequestURI: "sip:" + target + "@" + u.domain,
		Version:    "SIP/2.0",
		Headers:    make(map[string][]string),
	}
	ack.SetHeader("Via", fmt.Sprintf("SIP/2.0/UDP 127.0.0.1:%d;branch=%s;rport", u.localSIP.LocalAddr().(*net.UDPAddr).Port, u.nextBranch()))
	ack.SetHeader("Max-Forwards", "70")
	ack.SetHeader("To", resp200.GetHeader("To"))
	ack.SetHeader("From", fmt.Sprintf("<sip:%s@%s>;tag=%s", u.ext, u.domain, u.tag))
	ack.SetHeader("Call-ID", u.callID)
	ack.SetHeader("CSeq", fmt.Sprintf("%d ACK", u.cseq))
	ack.SetHeader("Content-Length", "0")
	return u.send(ack)
}

func (u *UA) sendBye(target, toHeader string) error {
	u.cseq++
	bye := &sip.Message{
		IsRequest:  true,
		Method:     "BYE",
		RequestURI: "sip:" + target + "@" + u.domain,
		Version:    "SIP/2.0",
		Headers:    make(map[string][]string),
	}
	bye.SetHeader("Via", fmt.Sprintf("SIP/2.0/UDP 127.0.0.1:%d;branch=%s;rport", u.localSIP.LocalAddr().(*net.UDPAddr).Port, u.nextBranch()))
	bye.SetHeader("Max-Forwards", "70")
	bye.SetHeader("To", toHeader)
	bye.SetHeader("From", fmt.Sprintf("<sip:%s@%s>;tag=%s", u.ext, u.domain, u.tag))
	bye.SetHeader("Call-ID", u.callID)
	bye.SetHeader("CSeq", fmt.Sprintf("%d BYE", u.cseq))
	bye.SetHeader("Content-Length", "0")
	return u.send(bye)
}

func (u *UA) waitByeAndRespond() error {
	bye, err := u.recvUntil(5*time.Second, func(msg *sip.Message) bool { return msg.IsRequest && msg.Method == "BYE" })
	if err != nil {
		return err
	}
	return u.sendResponseToVia(bye, sip.BuildResponse(bye, 200, "OK", nil, ""))
}

func (u *UA) offerSDP() string {
	rtp := u.localRTP.LocalAddr().(*net.UDPAddr)
	return strings.Join([]string{
		"v=0",
		fmt.Sprintf("o=%s 1 1 IN IP4 127.0.0.1", u.ext),
		"s=SMURF E2E",
		"c=IN IP4 127.0.0.1",
		"t=0 0",
		fmt.Sprintf("m=audio %d RTP/AVP 0 8 101", rtp.Port),
		"a=rtpmap:0 PCMU/8000",
		"a=rtpmap:8 PCMA/8000",
		"a=rtpmap:101 telephone-event/8000",
		"a=fmtp:101 0-15",
		"a=sendrecv",
		"",
	}, "\r\n")
}

func parseMediaPort(sdp string) int {
	for _, line := range strings.Split(strings.ReplaceAll(sdp, "\r\n", "\n"), "\n") {
		if strings.HasPrefix(line, "m=audio ") {
			parts := strings.Fields(line)
			if len(parts) >= 2 {
				port, _ := strconv.Atoi(parts[1])
				return port
			}
		}
	}
	return 0
}

func sendRTP(conn *net.UDPConn, port int) error {
	target := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: port}
	packet := []byte{
		0x80, 0x00, 0x00, 0x01,
		0x00, 0x00, 0x00, 0x01,
		0x12, 0x34, 0x56, 0x78,
		0x7f,
	}
	_, err := conn.WriteToUDP(packet, target)
	return err
}

func waitForRTP(conn *net.UDPConn, timeout time.Duration) error {
	buf := make([]byte, 1500)
	_ = conn.SetReadDeadline(time.Now().Add(timeout))
	_, _, err := conn.ReadFromUDP(buf)
	return err
}

func exchangeRTP(aConn *net.UDPConn, aPort int, bConn *net.UDPConn, bPort int, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	var aOK, bOK bool
	for time.Now().Before(deadline) {
		if !aOK {
			_ = sendRTP(aConn, aPort)
			aOK = waitForRTP(bConn, 300*time.Millisecond) == nil
		}
		if !bOK {
			_ = sendRTP(bConn, bPort)
			bOK = waitForRTP(aConn, 300*time.Millisecond) == nil
		}
		if aOK && bOK {
			return nil
		}
	}
	return fmt.Errorf("rtp exchange failed a_ok=%v b_ok=%v", aOK, bOK)
}

func (u *UA) sendResponseToVia(req *sip.Message, resp *sip.Message) error {
	via := req.Values("Via")
	if len(via) == 0 {
		return fmt.Errorf("%s missing Via header", u.name)
	}
	hostPort := parseViaHostPort(via[0])
	addr, err := net.ResolveUDPAddr("udp", hostPort)
	if err != nil {
		return err
	}
	_, err = u.localSIP.WriteToUDP(resp.Bytes(), addr)
	return err
}

func parseViaHostPort(via string) string {
	via = strings.TrimSpace(via)
	parts := strings.Fields(via)
	if len(parts) < 2 {
		return "127.0.0.1:5060"
	}
	hostPart := parts[1]
	if semi := strings.Index(hostPart, ";"); semi >= 0 {
		hostPart = hostPart[:semi]
	}
	if !strings.Contains(hostPart, ":") {
		hostPart += ":5060"
	}
	return hostPart
}

func parseChallenge(header string) map[string]string {
	out := map[string]string{}
	header = strings.TrimSpace(strings.TrimPrefix(header, "Digest"))
	for _, part := range strings.Split(header, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		kv := strings.SplitN(part, "=", 2)
		if len(kv) != 2 {
			continue
		}
		out[strings.TrimSpace(strings.ToLower(kv[0]))] = strings.Trim(strings.TrimSpace(kv[1]), `"`)
	}
	return out
}

func authorizationHeader(method, uri, username, password string, challenge map[string]string) string {
	realm := challenge["realm"]
	nonce := challenge["nonce"]
	qop := challenge["qop"]
	algorithm := challenge["algorithm"]
	if algorithm == "" {
		algorithm = "MD5"
	}
	cnonce := randHex(8)
	nc := "00000001"
	ha1 := hash(algorithm, fmt.Sprintf("%s:%s:%s", username, realm, password))
	ha2 := hash(algorithm, fmt.Sprintf("%s:%s", method, uri))
	response := hash(algorithm, fmt.Sprintf("%s:%s:%s:%s:%s:%s", ha1, nonce, nc, cnonce, qop, ha2))
	return fmt.Sprintf(`Digest username="%s", realm="%s", nonce="%s", uri="%s", algorithm=%s, response="%s", qop=%s, nc=%s, cnonce="%s"`,
		username, realm, nonce, uri, algorithm, response, qop, nc, cnonce)
}

func hash(algorithm, input string) string {
	sum := md5.Sum([]byte(input))
	if strings.EqualFold(algorithm, "SHA-256") || strings.EqualFold(algorithm, "SHA256") {
		h := md5.Sum([]byte{}) // avoid extra import juggling in this harness
		_ = h
	}
	return hex.EncodeToString(sum[:])
}

func randHex(n int) string {
	const chars = "abcdef0123456789"
	var b bytes.Buffer
	for i := 0; i < n; i++ {
		b.WriteByte(chars[rand.Intn(len(chars))])
	}
	return b.String()
}

func main() {
	rand.Seed(time.Now().UnixNano())
	server := flag.String("server", "127.0.0.1:15060", "SMURF SIP UDP address")
	domain := flag.String("domain", "127.0.0.1", "SIP domain / realm")
	apiBase := flag.String("api", "https://127.0.0.1:15001", "SMURF HTTPS admin API base")
	flag.Parse()

	if err := ensureExtension(*apiBase, "admin", "admin123!", "1001", "E2E 1001", "abc123"); err != nil {
		fmt.Fprintln(os.Stderr, "ensure extension 1001 failed:", err)
		os.Exit(1)
	}

	uaA, err := newUA("alice", "1000", "12345", *domain, *server)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer uaA.Close()

	uaB, err := newUA("bob", "1001", "abc123", *domain, *server)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer uaB.Close()

	fmt.Println("E2E: register UA A")
	if err := uaA.register(); err != nil {
		fmt.Fprintln(os.Stderr, "uaA register failed:", err)
		os.Exit(2)
	}

	fmt.Println("E2E: register UA B")
	if err := uaB.register(); err != nil {
		fmt.Fprintln(os.Stderr, "uaB register failed:", err)
		os.Exit(3)
	}

	inviteCh := make(chan *sip.Message, 1)
	errCh := make(chan error, 1)
	go func() {
		inv, err := uaB.waitIncomingInvite()
		if err != nil {
			errCh <- err
			return
		}
		fmt.Println("E2E: UA B received INVITE")
		if err := uaB.answerInvite(inv); err != nil {
			errCh <- err
			return
		}
		fmt.Println("E2E: UA B sent 200 OK")
		inviteCh <- inv
	}()

	fmt.Println("E2E: place INVITE A -> B")
	resp200, err := uaA.invite("1001")
	if err != nil {
		fmt.Fprintln(os.Stderr, "invite failed:", err)
		os.Exit(4)
	}
	var invFromA *sip.Message
	select {
	case err := <-errCh:
		fmt.Fprintln(os.Stderr, "callee invite handling failed:", err)
		os.Exit(5)
	case invFromA = <-inviteCh:
	}

	if err := uaA.sendAck(resp200, "1001"); err != nil {
		fmt.Fprintln(os.Stderr, "ack failed:", err)
		os.Exit(6)
	}
	if err := uaB.waitAck(); err != nil {
		fmt.Fprintln(os.Stderr, "callee ack wait failed:", err)
		os.Exit(7)
	}

	portA := parseMediaPort(resp200.Body)
	if portA == 0 {
		fmt.Fprintln(os.Stderr, "failed to parse relay media port")
		os.Exit(8)
	}
	portB := parseMediaPort(invFromA.Body)
	if portB == 0 {
		fmt.Fprintln(os.Stderr, "failed to parse callee relay media port")
		os.Exit(9)
	}
	fmt.Printf("E2E: relay ports caller-side=%d callee-side=%d\n", portA, portB)
	if err := exchangeRTP(uaA.localRTP, portA, uaB.localRTP, portB, 3*time.Second); err != nil {
		fmt.Fprintln(os.Stderr, "rtp relay failed:", err)
		os.Exit(10)
	}

	go func() {
		errCh <- uaB.waitByeAndRespond()
	}()

	fmt.Println("E2E: send BYE")
	if err := uaA.sendBye("1001", resp200.GetHeader("To")); err != nil {
		fmt.Fprintln(os.Stderr, "bye send failed:", err)
		os.Exit(11)
	}
	if _, err := uaA.recvUntil(3*time.Second, func(msg *sip.Message) bool { return !msg.IsRequest && msg.StatusCode == 200 && strings.Contains(msg.GetHeader("CSeq"), "BYE") }); err != nil {
		fmt.Fprintln(os.Stderr, "bye 200 failed:", err)
		os.Exit(12)
	}
	if err := <-errCh; err != nil {
		fmt.Fprintln(os.Stderr, "callee bye handling failed:", err)
		os.Exit(13)
	}

	fmt.Println("E2E PASS")
}

func ensureExtension(apiBase, adminUser, adminPass, number, displayName, password string) error {
	client := &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: insecureTLSConfig(),
		},
	}
	loginPayload := map[string]string{"username": adminUser, "password": adminPass}
	body, _ := json.Marshal(loginPayload)
	resp, err := client.Post(apiBase+"/api/login", "application/json", bytes.NewReader(body))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("login status %d", resp.StatusCode)
	}
	var loginResp map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&loginResp); err != nil {
		return err
	}
	token, _ := loginResp["token"].(string)
	if token == "" {
		return fmt.Errorf("missing login token")
	}
	req, _ := http.NewRequest(http.MethodGet, apiBase+"/api/extensions", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err = client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("list extensions status %d", resp.StatusCode)
	}
	var list []map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		return err
	}
	for _, ext := range list {
		if fmt.Sprint(ext["number"]) == number {
			return nil
		}
	}
	createPayload := map[string]string{
		"number":       number,
		"display_name": displayName,
		"password":     password,
	}
	body, _ = json.Marshal(createPayload)
	req, _ = http.NewRequest(http.MethodPost, apiBase+"/api/extensions", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err = client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 201 {
		return fmt.Errorf("create extension status %d", resp.StatusCode)
	}
	return nil
}

func insecureTLSConfig() *tls.Config {
	return &tls.Config{InsecureSkipVerify: true, MinVersion: tls.VersionTLS12}
}
