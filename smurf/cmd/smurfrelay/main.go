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
)

// SMURF RTP relay: two UDP ports per call leg; forwards RTP symmetrically
// between the first remote endpoints seen on each leg.

type ctlOpen struct {
	Cmd string `json:"cmd"`
	ID  string `json:"id"`
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
	id    string
	rtpA  *net.UDPConn
	rtpB  *net.UDPConn
	stop  chan struct{}
	wg    sync.WaitGroup

	mu    sync.Mutex
	addrA *net.UDPAddr
	addrB *net.UDPAddr
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
		switch cmd {
		case "open":
			if id == "" {
				_ = enc.Encode(ctlOpenResp{Error: "missing id"})
				continue
			}
			a, b, err := openSession(id, bindIP)
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

func openSession(id, bindIP string) (int, int, error) {
	sessionsMu.Lock()
	if _, ok := sessions[id]; ok {
		sessionsMu.Unlock()
		return 0, 0, fmt.Errorf("session exists")
	}
	s := &session{id: id, stop: make(chan struct{})}
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
	go s.forwardLoop(rtpA, rtpB, &s.addrA, &s.addrB)
	go s.forwardLoop(rtpB, rtpA, &s.addrB, &s.addrA)

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

func (s *session) forwardLoop(in, out *net.UDPConn, srcAddr, peerAddr **net.UDPAddr) {
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
		s.mu.Unlock()
		if dst == nil {
			continue
		}
		_, _ = out.WriteToUDP(buf[:n], dst)
	}
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
	s.wg.Wait()
}
