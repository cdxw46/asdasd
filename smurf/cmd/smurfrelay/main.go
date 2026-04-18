package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"sync"
	"time"

	"github.com/pion/srtp/v3"
)

type ctlOpen struct {
	Cmd     string `json:"cmd"`
	ID      string `json:"id"`
	TapAddr string `json:"tap_addr,omitempty"`
}

type ctlOpenResp struct {
	RTPA  int    `json:"rtp_a"`
	RTPB  int    `json:"rtp_b"`
	Error string `json:"error,omitempty"`
}

type ctlClose struct {
	Cmd string `json:"cmd"`
	ID  string `json:"id"`
}

type session struct {
	id      string
	rtpA    *net.UDPConn
	rtpB    *net.UDPConn
	tapConn *net.UDPConn
	stop    chan struct{}
	wg      sync.WaitGroup
	mu      sync.Mutex
	addrA   *net.UDPAddr
	addrB   *net.UDPAddr

	decA *srtp.Context // decrypt RTP received on leg A (from caller)
	encA *srtp.Context // encrypt RTP sent toward leg A (to caller)
	decB *srtp.Context // decrypt RTP received on leg B (from callee)
	encB *srtp.Context // encrypt RTP sent toward leg B (to callee)
}

var (
	sessions   = map[string]*session{}
	sessionsMu sync.Mutex
)

func main() {
	bind := flag.String("bind", "127.0.0.1", "IP to bind RTP sockets")
	ctrl := flag.String("control", "127.0.0.1:19000", "TCP control listen address")
	flag.Parse()

	ln, err := net.Listen("tcp", *ctrl)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("smurfrelay control %s media bind %s", *ctrl, *bind)
	for {
		c, err := ln.Accept()
		if err != nil {
			log.Printf("accept: %v", err)
			continue
		}
		go handleControl(c, *bind)
	}
}

func handleControl(c net.Conn, bindIP string) {
	defer c.Close()
	dec := json.NewDecoder(bufio.NewReader(c))
	enc := json.NewEncoder(c)
	for {
		var raw map[string]any
		if err := dec.Decode(&raw); err != nil {
			return
		}
		cmd, _ := raw["cmd"].(string)
		id, _ := raw["id"].(string)
		tap, _ := raw["tap_addr"].(string)
		switch cmd {
		case "srtp":
			if id == "" {
				_ = enc.Encode(map[string]string{"error": "missing id"})
				continue
			}
			aDK, _ := raw["a_decrypt_key"].(string)
			aDS, _ := raw["a_decrypt_salt"].(string)
			aEK, _ := raw["a_encrypt_key"].(string)
			aES, _ := raw["a_encrypt_salt"].(string)
			bDK, _ := raw["b_decrypt_key"].(string)
			bDS, _ := raw["b_decrypt_salt"].(string)
			bEK, _ := raw["b_encrypt_key"].(string)
			bES, _ := raw["b_encrypt_salt"].(string)
			if err := applySRTPToSession(id, aDK, aDS, aEK, aES, bDK, bDS, bEK, bES); err != nil {
				_ = enc.Encode(map[string]string{"error": err.Error()})
				continue
			}
			_ = enc.Encode(map[string]string{"ok": "1"})
		case "open":
			if id == "" {
				_ = enc.Encode(ctlOpenResp{Error: "missing id"})
				continue
			}
			a, b, err := openSession(id, bindIP, tap)
			if err != nil {
				_ = enc.Encode(ctlOpenResp{Error: err.Error()})
				continue
			}
			_ = enc.Encode(ctlOpenResp{RTPA: a, RTPB: b})
		case "close":
			closeSession(id)
			_ = enc.Encode(map[string]string{"ok": "1"})
		default:
			_ = enc.Encode(map[string]string{"error": "unknown cmd"})
		}
	}
}

func openSession(id, bindIP, tapAddr string) (int, int, error) {
	sessionsMu.Lock()
	if _, ok := sessions[id]; ok {
		sessionsMu.Unlock()
		return 0, 0, fmt.Errorf("session exists")
	}
	s := &session{id: id, stop: make(chan struct{})}
	if tapAddr != "" {
		a, err := net.ResolveUDPAddr("udp", tapAddr)
		if err != nil {
			sessionsMu.Unlock()
			return 0, 0, err
		}
		tc, err := net.DialUDP("udp", nil, a)
		if err != nil {
			sessionsMu.Unlock()
			return 0, 0, err
		}
		s.tapConn = tc
	}
	sessions[id] = s
	sessionsMu.Unlock()

	rtpA, portA, err := listenUDP(bindIP)
	if err != nil {
		closeSession(id)
		return 0, 0, err
	}
	rtpB, portB, err := listenUDP(bindIP)
	if err != nil {
		_ = rtpA.Close()
		closeSession(id)
		return 0, 0, err
	}
	s.rtpA, s.rtpB = rtpA, rtpB

	s.wg.Add(2)
	go s.forwardLoop(rtpA, rtpB, &s.addrA, &s.addrB, true)
	go s.forwardLoop(rtpB, rtpA, &s.addrB, &s.addrA, false)

	return portA, portB, nil
}

func listenUDP(bindIP string) (*net.UDPConn, int, error) {
	ip := net.ParseIP(bindIP)
	if ip == nil {
		return nil, 0, fmt.Errorf("bad bind ip")
	}
	c, err := net.ListenUDP("udp", &net.UDPAddr{IP: ip, Port: 0})
	if err != nil {
		return nil, 0, err
	}
	return c, c.LocalAddr().(*net.UDPAddr).Port, nil
}

func (s *session) forwardLoop(in, out *net.UDPConn, srcAddr, peerAddr **net.UDPAddr, isLegA bool) {
	defer s.wg.Done()
	buf := make([]byte, 2048)
	for {
		select {
		case <-s.stop:
			return
		default:
		}
		_ = in.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
		n, raddr, err := in.ReadFromUDP(buf)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return
		}
		s.mu.Lock()
		if *srcAddr == nil {
			*srcAddr = raddr
		}
		dst := *peerAddr
		tap := s.tapConn
		var decIn, encOut *srtp.Context
		if isLegA {
			decIn = s.decA
			encOut = s.encB
		} else {
			decIn = s.decB
			encOut = s.encA
		}
		s.mu.Unlock()

		pkt := buf[:n]
		if decIn != nil {
			pt, err := decryptRTPPacket(decIn, pkt)
			if err != nil {
				continue
			}
			pkt = pt
		}
		if isLegA && tap != nil {
			_, _ = tap.Write(pkt)
		}
		if dst == nil {
			continue
		}
		outPkt := pkt
		if encOut != nil {
			ct, err := encryptRTPPacket(encOut, pkt)
			if err != nil {
				continue
			}
			outPkt = ct
		}
		_, _ = out.WriteToUDP(outPkt, dst)
	}
}

func applySRTPToSession(id, aDK, aDS, aEK, aES, bDK, bDS, bEK, bES string) error {
	sessionsMu.Lock()
	s, ok := sessions[id]
	sessionsMu.Unlock()
	if !ok || s == nil {
		return fmt.Errorf("no session")
	}
	var decA, encA, decB, encB *srtp.Context
	var err error
	if aDK != "" && aDS != "" {
		decA, err = newSRTPDecryptContext(aDK, aDS)
		if err != nil {
			return err
		}
	}
	if aEK != "" && aES != "" {
		encA, err = newSRTPEncryptContext(aEK, aES)
		if err != nil {
			return err
		}
	}
	if bDK != "" && bDS != "" {
		decB, err = newSRTPDecryptContext(bDK, bDS)
		if err != nil {
			return err
		}
	}
	if bEK != "" && bES != "" {
		encB, err = newSRTPEncryptContext(bEK, bES)
		if err != nil {
			return err
		}
	}
	s.mu.Lock()
	s.decA, s.encA, s.decB, s.encB = decA, encA, decB, encB
	s.mu.Unlock()
	return nil
}

func closeSession(id string) {
	sessionsMu.Lock()
	s, ok := sessions[id]
	if !ok {
		sessionsMu.Unlock()
		return
	}
	delete(sessions, id)
	sessionsMu.Unlock()

	close(s.stop)
	if s.rtpA != nil {
		_ = s.rtpA.Close()
	}
	if s.rtpB != nil {
		_ = s.rtpB.Close()
	}
	if s.tapConn != nil {
		_ = s.tapConn.Close()
	}
	s.wg.Wait()
}
