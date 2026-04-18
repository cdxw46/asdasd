package rtp

import (
	"context"
	"fmt"
	"math/rand"
	"net"
	"sync"
	"syscall"
	"time"

	"smurf/internal/config"
)

type Endpoint struct {
	Addr *net.UDPAddr
}

type Session struct {
	ID         string
	CallerConn *net.UDPConn
	CalleeConn *net.UDPConn
	CallerAddr atomicAddr
	CalleeAddr atomicAddr
	CallerPort int
	CalleePort int
	Closed     chan struct{}
}

type atomicAddr struct {
	mu   sync.RWMutex
	addr *net.UDPAddr
}

func (a *atomicAddr) Set(addr *net.UDPAddr) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.addr = addr
}

func (a *atomicAddr) Get() *net.UDPAddr {
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.addr == nil {
		return nil
	}
	cp := *a.addr
	return &cp
}

type Manager struct {
	cfg      *config.Config
	mu       sync.Mutex
	nextPort int
	sessions map[string]*Session
}

func NewManager(cfg *config.Config) *Manager {
	return &Manager{
		cfg:      cfg,
		nextPort: cfg.RTP.StartPort,
		sessions: map[string]*Session{},
	}
}

func (m *Manager) CreateRelay(callID string) (*Session, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if existing := m.sessions[callID]; existing != nil {
		return existing, nil
	}
	if m.nextPort < m.cfg.RTP.StartPort || m.nextPort+3 > m.cfg.RTP.EndPort {
		m.nextPort = m.cfg.RTP.StartPort
	}
	callerPort := m.nextPort
	calleePort := m.nextPort + 2
	m.nextPort += 4

	callerConn, err := listenUDP(m.cfg.RTP.BindIP, callerPort, m.cfg.RTP.DSCP)
	if err != nil {
		return nil, err
	}
	calleeConn, err := listenUDP(m.cfg.RTP.BindIP, calleePort, m.cfg.RTP.DSCP)
	if err != nil {
		callerConn.Close()
		return nil, err
	}

	s := &Session{
		ID:         callID,
		CallerConn: callerConn,
		CalleeConn: calleeConn,
		CallerPort: callerPort,
		CalleePort: calleePort,
		Closed:     make(chan struct{}),
	}
	m.sessions[callID] = s
	go s.pipe(callerConn, &s.CallerAddr, &s.CalleeAddr, calleeConn)
	go s.pipe(calleeConn, &s.CalleeAddr, &s.CallerAddr, callerConn)
	return s, nil
}

func (m *Manager) Get(callID string) *Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sessions[callID]
}

func (m *Manager) Delete(callID string) {
	m.mu.Lock()
	s := m.sessions[callID]
	delete(m.sessions, callID)
	m.mu.Unlock()
	if s == nil {
		return
	}
	select {
	case <-s.Closed:
	default:
		close(s.Closed)
	}
	s.CallerConn.Close()
	s.CalleeConn.Close()
}

func (m *Manager) Shutdown(ctx context.Context) error {
	done := make(chan struct{})
	go func() {
		m.mu.Lock()
		defer m.mu.Unlock()
		for id, s := range m.sessions {
			select {
			case <-s.Closed:
			default:
				close(s.Closed)
			}
			s.CallerConn.Close()
			s.CalleeConn.Close()
			delete(m.sessions, id)
		}
		close(done)
	}()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-done:
		return nil
	}
}

func (s *Session) pipe(conn *net.UDPConn, own *atomicAddr, peer *atomicAddr, peerConn *net.UDPConn) {
	buf := make([]byte, 2000)
	for {
		conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		n, addr, err := conn.ReadFromUDP(buf)
		select {
		case <-s.Closed:
			return
		default:
		}
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return
		}
		own.Set(addr)
		if target := peer.Get(); target != nil {
			_, _ = peerConn.WriteToUDP(buf[:n], target)
		}
	}
}

func (s *Session) CallerAdvertised(ip string) string { return fmt.Sprintf("%s:%d", ip, s.CallerPort) }
func (s *Session) CalleeAdvertised(ip string) string { return fmt.Sprintf("%s:%d", ip, s.CalleePort) }

func listenUDP(bindIP string, port int, dscp int) (*net.UDPConn, error) {
	pc, err := net.ListenPacket("udp", fmt.Sprintf("%s:%d", bindIP, port))
	if err != nil {
		return nil, err
	}
	conn := pc.(*net.UDPConn)
	raw, err := conn.SyscallConn()
	if err == nil {
		_ = raw.Control(func(fd uintptr) {
			_ = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, syscall.IP_TOS, dscp<<2)
		})
	}
	return conn, nil
}

func NewSSRC() uint32 {
	return rand.New(rand.NewSource(time.Now().UnixNano())).Uint32()
}
