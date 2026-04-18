package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/smurf/pbx/internal/db"
)

// Deliver posts JSON to each enabled webhook that subscribes to event (async goroutines per URL).
func Deliver(ctx context.Context, pool *db.Pool, event string, payload map[string]any) {
	hooks, err := pool.ListWebhooks(ctx)
	if err != nil || len(hooks) == 0 {
		return
	}
	body, err := json.Marshal(map[string]any{"event": event, "payload": payload})
	if err != nil {
		return
	}
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	for _, h := range hooks {
		if !h.Enabled || h.URL == "" {
			continue
		}
		if !containsEvent(h.Events, event) {
			continue
		}
		hCopy := h
		bodyCopy := append([]byte(nil), body...)
		go postOne(hCopy.URL, hCopy.Secret, event, ts, bodyCopy)
	}
}

func containsEvent(events []string, e string) bool {
	for _, x := range events {
		if strings.EqualFold(strings.TrimSpace(x), e) {
			return true
		}
	}
	return false
}

func postOne(url, secret, event, ts string, body []byte) {
	sig := sign(secret, ts, body)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Smurf-Event", event)
	req.Header.Set("Smurf-Timestamp", ts)
	req.Header.Set("Smurf-Signature", "sha256="+sig)
	client := &http.Client{Timeout: 8 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("webhook: %s: %v", url, err)
		return
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode >= 300 {
		log.Printf("webhook: %s status %s", url, resp.Status)
	}
}

func sign(secret, ts string, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(ts + "."))
	_, _ = mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

// NotifyAnswered loads CDR and sends call.answered.
func NotifyAnswered(ctx context.Context, pool *db.Pool, cdrID int64) {
	row, err := pool.GetCDR(ctx, cdrID)
	if err != nil {
		return
	}
	Deliver(ctx, pool, "call.answered", cdrPayload(row))
}

// NotifyEnded loads CDR after hangup and sends call.ended.
func NotifyEnded(ctx context.Context, pool *db.Pool, cdrID int64) {
	row, err := pool.GetCDR(ctx, cdrID)
	if err != nil {
		return
	}
	Deliver(ctx, pool, "call.ended", cdrPayload(row))
}

func cdrPayload(r *db.CDRRow) map[string]any {
	m := map[string]any{
		"cdr_id":     r.ID,
		"call_id":    r.CallID,
		"from_ext":   r.FromExt,
		"to_ext":     r.ToExt,
		"direction":  r.Direction,
		"started_at": r.StartedAt.UTC().Format(time.RFC3339),
	}
	if r.QueueSlug != "" {
		m["queue_slug"] = r.QueueSlug
	}
	if r.AnsweredAt.Valid {
		m["answered_at"] = r.AnsweredAt.Time.UTC().Format(time.RFC3339)
	}
	if r.EndedAt.Valid {
		m["ended_at"] = r.EndedAt.Time.UTC().Format(time.RFC3339)
	}
	if r.DurationSec.Valid {
		m["duration_sec"] = r.DurationSec.Int32
	}
	if r.HangupCause.Valid {
		m["hangup_cause"] = r.HangupCause.String
	}
	return m
}
