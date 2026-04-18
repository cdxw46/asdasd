package voicemail

import (
	"encoding/binary"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"github.com/pion/rtp"

	"github.com/smurf/pbx/internal/audio"
)

// DepositRecorder receives RTP (PCMU PT=0) on a UDP socket and writes an 8 kHz mono WAV file on Close.
type DepositRecorder struct {
	conn    *net.UDPConn
	path    string
	mu      sync.Mutex
	samples []int16
	stop    chan struct{}
	done    chan struct{}
	wg      sync.WaitGroup
	maxDur  time.Duration
	started time.Time
}

// NewPCMURecorder writes to an explicit WAV path (PCMU RTP in).
func NewPCMURecorder(bindIP, wavPath string) (*DepositRecorder, error) {
	if err := os.MkdirAll(filepath.Dir(wavPath), 0755); err != nil {
		return nil, err
	}
	ip := net.ParseIP(bindIP)
	if ip == nil {
		return nil, fmt.Errorf("bad bind ip")
	}
	c, err := net.ListenUDP("udp", &net.UDPAddr{IP: ip, Port: 0})
	if err != nil {
		return nil, err
	}
	r := &DepositRecorder{
		conn:   c,
		path:   wavPath,
		stop:   make(chan struct{}),
		done:   make(chan struct{}),
		maxDur: 60 * time.Minute,
	}
	r.wg.Add(1)
	go r.loop()
	return r, nil
}

func NewDepositRecorder(bindIP string, outDir string) (*DepositRecorder, error) {
	if err := os.MkdirAll(outDir, 0755); err != nil {
		return nil, err
	}
	ip := net.ParseIP(bindIP)
	if ip == nil {
		return nil, fmt.Errorf("bad bind ip")
	}
	c, err := net.ListenUDP("udp", &net.UDPAddr{IP: ip, Port: 0})
	if err != nil {
		return nil, err
	}
	fn := filepath.Join(outDir, fmt.Sprintf("vm-%d.wav", time.Now().UnixNano()))
	r := &DepositRecorder{
		conn:   c,
		path:   fn,
		stop:   make(chan struct{}),
		done:   make(chan struct{}),
		maxDur: 3 * time.Minute,
	}
	r.wg.Add(1)
	go r.loop()
	return r, nil
}

func (r *DepositRecorder) LocalPort() int {
	return r.conn.LocalAddr().(*net.UDPAddr).Port
}

func (r *DepositRecorder) LocalAddr() string {
	a := r.conn.LocalAddr().(*net.UDPAddr)
	return net.JoinHostPort(a.IP.String(), strconv.Itoa(a.Port))
}

func (r *DepositRecorder) Path() string { return r.path }

func (r *DepositRecorder) loop() {
	defer r.wg.Done()
	defer close(r.done)
	r.started = time.Now()
	buf := make([]byte, 2048)
	for {
		select {
		case <-r.stop:
			return
		default:
		}
		_ = r.conn.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
		n, _, err := r.conn.ReadFromUDP(buf)
		if err != nil {
			if time.Since(r.started) > r.maxDur {
				return
			}
			continue
		}
		var pkt rtp.Packet
		if err := pkt.Unmarshal(buf[:n]); err != nil {
			continue
		}
		if pkt.PayloadType != 0 || len(pkt.Payload) == 0 {
			continue
		}
		r.mu.Lock()
		for _, b := range pkt.Payload {
			r.samples = append(r.samples, audio.ULawDecode(b))
		}
		r.mu.Unlock()
	}
}

func (r *DepositRecorder) Close() error {
	close(r.stop)
	<-r.done
	_ = r.conn.Close()
	r.mu.Lock()
	s := r.samples
	r.mu.Unlock()
	return writeWAV8kMono(r.path, s)
}

func writeWAV8kMono(path string, samples []int16) error {
	const rate = 8000
	const bits = 16
	dataBytes := len(samples) * 2
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	riffSize := 36 + dataBytes
	_, _ = f.Write([]byte("RIFF"))
	_ = binary.Write(f, binary.LittleEndian, uint32(riffSize))
	_, _ = f.Write([]byte("WAVEfmt "))
	_ = binary.Write(f, binary.LittleEndian, uint32(16))
	_ = binary.Write(f, binary.LittleEndian, uint16(1))
	_ = binary.Write(f, binary.LittleEndian, uint16(1))
	_ = binary.Write(f, binary.LittleEndian, uint32(rate))
	_ = binary.Write(f, binary.LittleEndian, uint32(rate*bits/8))
	_ = binary.Write(f, binary.LittleEndian, uint16(bits/8))
	_ = binary.Write(f, binary.LittleEndian, uint16(bits))
	_, _ = f.Write([]byte("data"))
	_ = binary.Write(f, binary.LittleEndian, uint32(dataBytes))
	for _, s := range samples {
		_ = binary.Write(f, binary.LittleEndian, s)
	}
	return nil
}
