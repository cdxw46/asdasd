package webrtcgw

import (
	"context"
	"fmt"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"

	"smurf/internal/config"
	"smurf/internal/pbx"
	"smurf/internal/util"
)

type BrowserSession struct {
	ID          string
	Extension   string
	CallID      string
	RemoteExt   string
	Peer        *webrtc.PeerConnection
	AudioTrack  *webrtc.TrackLocalStaticRTP
	RelayTarget *net.UDPAddr
	Closed      chan struct{}
}

type Gateway struct {
	cfg    *config.Config
	pbx    *pbx.Engine
	logger *util.Logger

	mu       sync.RWMutex
	sessions map[string]*BrowserSession
}

func New(cfg *config.Config, pbxEngine *pbx.Engine, logger *util.Logger) *Gateway {
	return &Gateway{
		cfg:      cfg,
		pbx:      pbxEngine,
		logger:   logger,
		sessions: map[string]*BrowserSession{},
	}
}

type OfferResult struct {
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
	Type      string `json:"type"`
}

func (g *Gateway) CreateOffer(ctx context.Context, sessionID, extension, remoteExt, callID string) (*OfferResult, error) {
	peer, track, err := g.newPeerConnection()
	if err != nil {
		return nil, err
	}
	session := &BrowserSession{
		ID:         sessionID,
		Extension:  extension,
		RemoteExt:  remoteExt,
		CallID:     callID,
		Peer:       peer,
		AudioTrack: track,
		Closed:     make(chan struct{}),
	}
	g.mu.Lock()
	g.sessions[sessionID] = session
	g.mu.Unlock()

	offer, err := peer.CreateOffer(nil)
	if err != nil {
		return nil, err
	}
	if err := peer.SetLocalDescription(offer); err != nil {
		return nil, err
	}

	select {
	case <-webrtc.GatheringCompletePromise(peer):
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	local := peer.LocalDescription()
	if local == nil {
		return nil, fmt.Errorf("missing local description")
	}
	return &OfferResult{
		SessionID: sessionID,
		SDP:       local.SDP,
		Type:      local.Type.String(),
	}, nil
}

func (g *Gateway) ApplyAnswer(sessionID, sdp string) error {
	session := g.Get(sessionID)
	if session == nil {
		return fmt.Errorf("browser session %s not found", sessionID)
	}
	return session.Peer.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  sdp,
	})
}

func (g *Gateway) BindCall(sessionID string) error {
	session := g.Get(sessionID)
	if session == nil {
		return fmt.Errorf("browser session %s not found", sessionID)
	}
	call := g.pbx.Get(session.CallID)
	if call == nil || call.Relay == nil {
		return fmt.Errorf("call %s not found", session.CallID)
	}
	target := &net.UDPAddr{IP: net.ParseIP(g.cfg.RTP.PublicIP), Port: call.CallerPort}
	if session.Extension == call.ToExtension {
		target = &net.UDPAddr{IP: net.ParseIP(g.cfg.RTP.PublicIP), Port: call.CalleePort}
	}
	session.RelayTarget = target
	return nil
}

func (g *Gateway) WriteRemoteRTP(sessionID string, pkt *rtp.Packet) error {
	session := g.Get(sessionID)
	if session == nil || session.AudioTrack == nil {
		return fmt.Errorf("browser session %s not found", sessionID)
	}
	return session.AudioTrack.WriteRTP(pkt)
}

func (g *Gateway) Get(sessionID string) *BrowserSession {
	g.mu.RLock()
	defer g.mu.RUnlock()
	return g.sessions[sessionID]
}

func (g *Gateway) Delete(sessionID string) {
	g.mu.Lock()
	session := g.sessions[sessionID]
	delete(g.sessions, sessionID)
	g.mu.Unlock()
	if session == nil {
		return
	}
	select {
	case <-session.Closed:
	default:
		close(session.Closed)
	}
	_ = session.Peer.Close()
}

func (g *Gateway) newPeerConnection() (*webrtc.PeerConnection, *webrtc.TrackLocalStaticRTP, error) {
	m := &webrtc.MediaEngine{}
	if err := m.RegisterDefaultCodecs(); err != nil {
		return nil, nil, err
	}
	api := webrtc.NewAPI(webrtc.WithMediaEngine(m))
	peer, err := api.NewPeerConnection(webrtc.Configuration{
		ICEServers: []webrtc.ICEServer{},
	})
	if err != nil {
		return nil, nil, err
	}

	audioTrack, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypePCMU, ClockRate: 8000, Channels: 1},
		"audio", "smurf",
	)
	if err != nil {
		_ = peer.Close()
		return nil, nil, err
	}
	if _, err := peer.AddTrack(audioTrack); err != nil {
		_ = peer.Close()
		return nil, nil, err
	}

	peer.OnTrack(func(remote *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		go g.consumeRemoteTrack(remote)
	})
	peer.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		g.logger.Info("webrtc peer state", "state", state.String())
	})
	return peer, audioTrack, nil
}

func (g *Gateway) consumeRemoteTrack(remote *webrtc.TrackRemote) {
	for {
		packet, _, err := remote.ReadRTP()
		if err != nil {
			return
		}
		_ = packet
	}
}

func BuildBrowserSDPAnswer(offer string) string {
	_ = offer
	return ""
}

func ParseAudioPort(sdp string) int {
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

func WaitUntil(timeout time.Duration, fn func() bool) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return true
		}
		time.Sleep(50 * time.Millisecond)
	}
	return false
}
