package pbx

import (
	"context"
	"fmt"
	"math/rand"
	"strings"
	"sync"
	"time"

	"smurf/internal/db"
	"smurf/internal/rtp"
	"smurf/internal/util"
)

type Store interface {
	CreateOrUpdateCDR(ctx context.Context, cdr db.CDR) error
	GetExtensionByNumber(ctx context.Context, number string) (*db.Extension, error)
	GetRingGroupByNumber(ctx context.Context, number string) (*db.RingGroup, error)
	GetQueueByNumber(ctx context.Context, number string) (*db.Queue, error)
	GetIVRByNumber(ctx context.Context, number string) (*db.IVRMenu, error)
	GetConferenceRoomByNumber(ctx context.Context, number string) (*db.ConferenceRoom, error)
}

type RouteKind string

const (
	RouteExtension  RouteKind = "extension"
	RouteRingGroup  RouteKind = "ring_group"
	RouteQueue      RouteKind = "queue"
	RouteIVR        RouteKind = "ivr"
	RouteConference RouteKind = "conference"
)

type RouteDecision struct {
	Kind         RouteKind         `json:"kind"`
	Target       string            `json:"target"`
	DisplayName  string            `json:"display_name,omitempty"`
	Members      []string          `json:"members,omitempty"`
	Targets      []string          `json:"targets,omitempty"`
	Strategy     string            `json:"strategy,omitempty"`
	Announcement string            `json:"announcement,omitempty"`
	Metadata     map[string]string `json:"metadata,omitempty"`
}

type CallSession struct {
	CallID        string    `json:"call_id"`
	FromExtension string    `json:"from_extension"`
	ToExtension   string    `json:"to_extension"`
	State         string    `json:"state"`
	StartedAt     time.Time `json:"started_at"`
	AnsweredAt    time.Time `json:"answered_at,omitempty"`
	EndedAt       time.Time `json:"ended_at,omitempty"`
	CallerPort    int       `json:"caller_port"`
	CalleePort    int       `json:"callee_port"`
	Relay         *rtp.Session
}

type Engine struct {
	store Store
	relay *rtp.Manager
	log   *util.Logger

	mu    sync.RWMutex
	calls map[string]*CallSession
}

func New(store Store, relay *rtp.Manager, log *util.Logger) *Engine {
	return &Engine{
		store: store,
		relay: relay,
		log:   log,
		calls: make(map[string]*CallSession),
	}
}

func (e *Engine) StartInternalCall(ctx context.Context, fromExt, toExt, callID string) (*CallSession, error) {
	e.mu.Lock()
	if existing := e.calls[callID]; existing != nil {
		e.mu.Unlock()
		return existing, nil
	}
	e.mu.Unlock()

	relay, err := e.relay.CreateRelay(callID)
	if err != nil {
		return nil, err
	}

	call := &CallSession{
		CallID:        callID,
		FromExtension: fromExt,
		ToExtension:   toExt,
		State:         "ringing",
		StartedAt:     time.Now().UTC(),
		CallerPort:    relay.CallerPort,
		CalleePort:    relay.CalleePort,
		Relay:         relay,
	}

	e.mu.Lock()
	e.calls[callID] = call
	e.mu.Unlock()

	if err := e.store.CreateOrUpdateCDR(ctx, db.CDR{
		CallID:        call.CallID,
		FromExtension: call.FromExtension,
		ToExtension:   call.ToExtension,
		State:         call.State,
		StartedAt:     call.StartedAt,
	}); err != nil {
		e.relay.Delete(callID)
		e.mu.Lock()
		delete(e.calls, callID)
		e.mu.Unlock()
		return nil, err
	}

	e.log.Info("call started", "call_id", callID, "from", fromExt, "to", toExt)
	return call, nil
}

func (e *Engine) Get(callID string) *CallSession {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.calls[callID]
}

func (e *Engine) MarkAnswered(ctx context.Context, callID string) error {
	e.mu.Lock()
	call := e.calls[callID]
	if call == nil {
		e.mu.Unlock()
		return fmt.Errorf("call %s not found", callID)
	}
	if call.AnsweredAt.IsZero() {
		call.AnsweredAt = time.Now().UTC()
	}
	call.State = "answered"
	cdr := db.CDR{
		CallID:        call.CallID,
		FromExtension: call.FromExtension,
		ToExtension:   call.ToExtension,
		State:         call.State,
		StartedAt:     call.StartedAt,
		AnsweredAt:    call.AnsweredAt,
		DurationSec:   int(time.Since(call.StartedAt).Seconds()),
	}
	e.mu.Unlock()
	return e.store.CreateOrUpdateCDR(ctx, cdr)
}

func (e *Engine) MarkEnded(ctx context.Context, callID string) error {
	e.mu.Lock()
	call := e.calls[callID]
	if call == nil {
		e.mu.Unlock()
		return nil
	}
	delete(e.calls, callID)
	call.EndedAt = time.Now().UTC()
	if call.State == "" || call.State == "ringing" {
		call.State = "completed"
	}
	if call.AnsweredAt.IsZero() && call.State == "completed" {
		call.State = "cancelled"
	}
	cdr := db.CDR{
		CallID:        call.CallID,
		FromExtension: call.FromExtension,
		ToExtension:   call.ToExtension,
		State:         call.State,
		StartedAt:     call.StartedAt,
		AnsweredAt:    call.AnsweredAt,
		EndedAt:       call.EndedAt,
		DurationSec:   int(call.EndedAt.Sub(call.StartedAt).Seconds()),
	}
	e.mu.Unlock()

	e.relay.Delete(callID)
	e.log.Info("call ended", "call_id", callID, "state", call.State)
	return e.store.CreateOrUpdateCDR(ctx, cdr)
}

func (e *Engine) Snapshot() []*CallSession {
	e.mu.RLock()
	defer e.mu.RUnlock()
	out := make([]*CallSession, 0, len(e.calls))
	for _, call := range e.calls {
		cp := *call
		cp.Relay = nil
		out = append(out, &cp)
	}
	return out
}

func (e *Engine) Stats() map[string]any {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return map[string]any{
		"active_calls": len(e.calls),
	}
}

func (e *Engine) ResolveRoute(ctx context.Context, fromExt, dialed string) (*RouteDecision, error) {
	dialed = strings.TrimSpace(dialed)
	if dialed == "" {
		return nil, fmt.Errorf("empty dialed target")
	}
	if ext, err := e.store.GetExtensionByNumber(ctx, dialed); err == nil && ext != nil {
		return &RouteDecision{
			Kind:        RouteExtension,
			Target:      ext.Number,
			DisplayName: ext.DisplayName,
		}, nil
	}
	if rg, err := e.store.GetRingGroupByNumber(ctx, dialed); err == nil && rg != nil {
		members := filterOutTarget(append([]string(nil), rg.Members...), fromExt)
		return &RouteDecision{
			Kind:        RouteRingGroup,
			Target:      rg.Extension,
			DisplayName: rg.Name,
			Targets:     members,
		}, nil
	}
	if q, err := e.store.GetQueueByNumber(ctx, dialed); err == nil && q != nil {
		members := filterOutTarget(append([]string(nil), q.Agents...), fromExt)
		return &RouteDecision{
			Kind:        RouteQueue,
			Target:      q.Extension,
			DisplayName: q.Name,
			Targets:     orderQueueTargets(members, q.Strategy),
			Strategy:    q.Strategy,
		}, nil
	}
	if ivr, err := e.store.GetIVRByNumber(ctx, dialed); err == nil && ivr != nil {
		targets := []string{}
		metadata := map[string]string{}
		if ivr.DefaultTarget != "" {
			targets = append(targets, ivr.DefaultTarget)
			metadata["default"] = ivr.DefaultTarget
		}
		return &RouteDecision{
			Kind:         RouteIVR,
			Target:       ivr.Extension,
			DisplayName:  ivr.Name,
			Targets:      targets,
			Announcement: ivr.Greeting,
			Metadata:     metadata,
		}, nil
	}
	if conf, err := e.store.GetConferenceRoomByNumber(ctx, dialed); err == nil && conf != nil {
		return &RouteDecision{
			Kind:        RouteConference,
			Target:      conf.Extension,
			DisplayName: conf.Name,
		}, nil
	}
	return nil, fmt.Errorf("no route found for %s", dialed)
}

func orderQueueTargets(targets []string, strategy string) []string {
	if len(targets) <= 1 {
		return targets
	}
	out := append([]string(nil), targets...)
	switch strings.ToLower(strings.TrimSpace(strategy)) {
	case "random":
		rand.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	case "round-robin":
		// Keep stored order for now; this is the stable baseline.
	default:
		// Fallback keeps stored order for least-busy/priority until richer stats exist.
	}
	return out
}

func filterOutTarget(targets []string, exclude string) []string {
	exclude = strings.TrimSpace(exclude)
	if exclude == "" {
		return append([]string(nil), targets...)
	}
	out := make([]string, 0, len(targets))
	for _, target := range targets {
		if strings.TrimSpace(target) != exclude {
			out = append(out, target)
		}
	}
	return out
}

