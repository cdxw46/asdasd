package webrtcmedia

import (
	"fmt"
	"net"
	"sync"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
)

// Bridge links browser WebRTC (PCMU) to one UDP leg of smurfrelay (PCMU RTP).
type Bridge struct {
	pc        *webrtc.PeerConnection
	relayAddr *net.UDPAddr
	conn      *net.UDPConn
	outTrack  *webrtc.TrackLocalStaticRTP
	mu        sync.Mutex
	closed    bool
	wg        sync.WaitGroup
}

// NewBridge applies the browser SDP offer, returns the SDP answer and starts RTP relay.
func NewBridge(offerSDP, relayHost string, relayRTPPort int) (answerSDP string, cleanup func(), err error) {
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		return "", nil, err
	}

	outTrack, err := webrtc.NewTrackLocalStaticRTP(webrtc.RTPCodecCapability{
		MimeType:  webrtc.MimeTypePCMU,
		ClockRate: 8000,
		Channels:  1,
	}, "audio", "smurf-webrtc")
	if err != nil {
		_ = pc.Close()
		return "", nil, err
	}
	if _, err = pc.AddTrack(outTrack); err != nil {
		_ = pc.Close()
		return "", nil, err
	}

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer,
		SDP:  offerSDP,
	}); err != nil {
		_ = pc.Close()
		return "", nil, err
	}

	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		_ = pc.Close()
		return "", nil, err
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		_ = pc.Close()
		return "", nil, err
	}
	select {
	case <-gatherComplete:
	case <-time.After(8 * time.Second):
	}

	relayAddr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", relayHost, relayRTPPort))
	if err != nil {
		_ = pc.Close()
		return "", nil, err
	}
	udpConn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		_ = pc.Close()
		return "", nil, err
	}

	b := &Bridge{pc: pc, relayAddr: relayAddr, conn: udpConn, outTrack: outTrack}

	pc.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		b.wg.Add(1)
		go func() {
			defer b.wg.Done()
			for {
				pkt, _, err := track.ReadRTP()
				if err != nil {
					return
				}
				pkt.PayloadType = 0
				data, err := pkt.Marshal()
				if err != nil {
					continue
				}
				_, _ = b.conn.WriteToUDP(data, b.relayAddr)
			}
		}()
	})

	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		buf := make([]byte, 2048)
		for {
			_ = b.conn.SetReadDeadline(time.Now().Add(800 * time.Millisecond))
			n, _, err := b.conn.ReadFromUDP(buf)
			if err != nil {
				b.mu.Lock()
				done := b.closed
				b.mu.Unlock()
				if done {
					return
				}
				continue
			}
			var pkt rtp.Packet
			if err := pkt.Unmarshal(buf[:n]); err != nil {
				continue
			}
			if err := outTrack.WriteRTP(&pkt); err != nil {
				return
			}
		}
	}()

	cleanup = func() {
		b.Close()
	}
	ld := pc.LocalDescription()
	if ld == nil {
		cleanup()
		return "", nil, fmt.Errorf("no local description")
	}
	return ld.SDP, cleanup, nil
}

func (b *Bridge) Close() {
	b.mu.Lock()
	if b.closed {
		b.mu.Unlock()
		return
	}
	b.closed = true
	b.mu.Unlock()
	if b.pc != nil {
		_ = b.pc.Close()
	}
	if b.conn != nil {
		_ = b.conn.Close()
	}
	b.wg.Wait()
}
