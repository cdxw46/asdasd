package pbx

import (
	"context"
	"fmt"
	"sync"
	"time"

	"smurf/internal/db"
	"smurf/internal/rtp"
	"smurf/internal/util"
)

type Store interface {
	CreateOrUpdateCDR(ctx context.Context, cdr db.CDR) error
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

