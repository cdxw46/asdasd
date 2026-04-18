package db

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Pool struct {
	*pgxpool.Pool
}

func Connect(ctx context.Context, dsn string) (*Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 32
	cfg.MinConns = 2
	cfg.MaxConnLifetime = time.Hour
	p, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if err := p.Ping(ctx); err != nil {
		p.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	return &Pool{p}, nil
}

type Extension struct {
	Number        string
	Secret        string
	DisplayName   string
	MaxConcurrent int
}

func (p *Pool) GetExtension(ctx context.Context, number string) (*Extension, error) {
	var e Extension
	err := p.QueryRow(ctx, `
		SELECT number, secret, display_name, max_concurrent
		FROM extensions WHERE number = $1
	`, number).Scan(&e.Number, &e.Secret, &e.DisplayName, &e.MaxConcurrent)
	if err != nil {
		return nil, err
	}
	return &e, nil
}

type Registration struct {
	Extension  string
	AOR        string
	ContactURI string
	RemoteIP   string
	RemotePort int
	Transport  string
	ExpiresAt  time.Time
	CallID     string
	UserAgent  string
}

func (p *Pool) UpsertRegistration(ctx context.Context, r Registration) error {
	_, err := p.Exec(ctx, `
		INSERT INTO registrations (extension, aor, contact_uri, remote_ip, remote_port, transport, expires_at, call_id, user_agent, updated_at)
		VALUES ($1,$2,$3,$4::inet,$5,$6,$7,$8,$9, now())
		ON CONFLICT (extension) DO UPDATE SET
			aor = EXCLUDED.aor,
			contact_uri = EXCLUDED.contact_uri,
			remote_ip = EXCLUDED.remote_ip,
			remote_port = EXCLUDED.remote_port,
			transport = EXCLUDED.transport,
			expires_at = EXCLUDED.expires_at,
			call_id = EXCLUDED.call_id,
			user_agent = EXCLUDED.user_agent,
			updated_at = now()
	`, r.Extension, r.AOR, r.ContactURI, r.RemoteIP, r.RemotePort, r.Transport, r.ExpiresAt, r.CallID, r.UserAgent)
	return err
}

func (p *Pool) DeleteRegistration(ctx context.Context, extension string) error {
	_, err := p.Exec(ctx, `DELETE FROM registrations WHERE extension = $1`, extension)
	return err
}

func (p *Pool) GetRegistration(ctx context.Context, extension string) (*Registration, error) {
	var r Registration
	err := p.QueryRow(ctx, `
		SELECT extension, aor, contact_uri, host(remote_ip)::text, remote_port, transport, expires_at, call_id, user_agent
		FROM registrations WHERE extension = $1 AND expires_at > now()
	`, extension).Scan(&r.Extension, &r.AOR, &r.ContactURI, &r.RemoteIP, &r.RemotePort, &r.Transport, &r.ExpiresAt, &r.CallID, &r.UserAgent)
	if err != nil {
		return nil, err
	}
	return &r, nil
}

func (p *Pool) ListExtensions(ctx context.Context) ([]Extension, error) {
	rows, err := p.Query(ctx, `SELECT number, secret, display_name, max_concurrent FROM extensions ORDER BY number`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Extension
	for rows.Next() {
		var e Extension
		if err := rows.Scan(&e.Number, &e.Secret, &e.DisplayName, &e.MaxConcurrent); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

type ExtensionPublic struct {
	Number        string
	DisplayName   string
	MaxConcurrent int
}

func (p *Pool) ListExtensionsPublic(ctx context.Context) ([]ExtensionPublic, error) {
	rows, err := p.Query(ctx, `SELECT number, display_name, max_concurrent FROM extensions ORDER BY number`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ExtensionPublic
	for rows.Next() {
		var e ExtensionPublic
		if err := rows.Scan(&e.Number, &e.DisplayName, &e.MaxConcurrent); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

func (p *Pool) InsertExtension(ctx context.Context, e Extension) error {
	_, err := p.Exec(ctx, `
		INSERT INTO extensions (number, secret, display_name, max_concurrent)
		VALUES ($1,$2,$3,$4)
	`, e.Number, e.Secret, e.DisplayName, e.MaxConcurrent)
	return err
}

func (p *Pool) DeleteExtension(ctx context.Context, number string) error {
	_, err := p.Exec(ctx, `DELETE FROM extensions WHERE number = $1`, number)
	return err
}

type AdminUser struct {
	Username     string
	PasswordHash string
	Role         string
}

func (p *Pool) GetAdminByUsername(ctx context.Context, username string) (*AdminUser, error) {
	var u AdminUser
	err := p.QueryRow(ctx, `SELECT username, password_hash, role FROM admin_users WHERE username = $1`, username).
		Scan(&u.Username, &u.PasswordHash, &u.Role)
	if err != nil {
		return nil, err
	}
	return &u, nil
}

func (p *Pool) InsertCDR(ctx context.Context, callID, fromExt, toExt, direction string) (int64, error) {
	var id int64
	err := p.QueryRow(ctx, `
		INSERT INTO cdr (call_id, from_ext, to_ext, direction)
		VALUES ($1,$2,$3,$4) RETURNING id
	`, callID, fromExt, toExt, direction).Scan(&id)
	return id, err
}

func (p *Pool) UpdateCDRAnswered(ctx context.Context, id int64) error {
	_, err := p.Exec(ctx, `UPDATE cdr SET answered_at = now() WHERE id = $1 AND answered_at IS NULL`, id)
	return err
}

func (p *Pool) UpdateCDREnded(ctx context.Context, id int64, cause string) error {
	_, err := p.Exec(ctx, `
		UPDATE cdr SET
			ended_at = now(),
			duration_sec = CASE WHEN answered_at IS NOT NULL THEN GREATEST(0, EXTRACT(EPOCH FROM (now() - answered_at))::int) ELSE NULL END,
			hangup_cause = $2
		WHERE id = $1
	`, id, cause)
	return err
}

func (p *Pool) SetCDRQueue(ctx context.Context, id int64, queueSlug string) error {
	_, err := p.Exec(ctx, `UPDATE cdr SET queue_slug = $2 WHERE id = $1`, id, queueSlug)
	return err
}

type CDRRow struct {
	ID          int64
	CallID      string
	FromExt     string
	ToExt       string
	Direction   string
	QueueSlug   string
	StartedAt   time.Time
	AnsweredAt  sql.NullTime
	EndedAt     sql.NullTime
	DurationSec sql.NullInt32
	HangupCause sql.NullString
}

func (p *Pool) GetCDR(ctx context.Context, id int64) (*CDRRow, error) {
	var r CDRRow
	var fromExt, toExt, queueSlug sql.NullString
	err := p.QueryRow(ctx, `
		SELECT id, call_id, from_ext, to_ext, direction, queue_slug,
			started_at, answered_at, ended_at, duration_sec, hangup_cause
		FROM cdr WHERE id = $1
	`, id).Scan(&r.ID, &r.CallID, &fromExt, &toExt, &r.Direction, &queueSlug,
		&r.StartedAt, &r.AnsweredAt, &r.EndedAt, &r.DurationSec, &r.HangupCause)
	if err != nil {
		return nil, err
	}
	if fromExt.Valid {
		r.FromExt = fromExt.String
	}
	if toExt.Valid {
		r.ToExt = toExt.String
	}
	if queueSlug.Valid {
		r.QueueSlug = queueSlug.String
	}
	return &r, nil
}

// --- Call queues ---

type CallQueue struct {
	Slug            string
	Name            string
	Strategy        string
	RingTimeoutSec  int
}

func (p *Pool) GetCallQueue(ctx context.Context, slug string) (*CallQueue, error) {
	var q CallQueue
	err := p.QueryRow(ctx, `
		SELECT slug, name, strategy, ring_timeout_sec FROM call_queues WHERE slug = $1
	`, slug).Scan(&q.Slug, &q.Name, &q.Strategy, &q.RingTimeoutSec)
	if err != nil {
		return nil, err
	}
	return &q, nil
}

func (p *Pool) ListQueueMemberExtensions(ctx context.Context, slug string) ([]string, error) {
	rows, err := p.Query(ctx, `
		SELECT m.extension_number
		FROM call_queue_members m
		WHERE m.queue_slug = $1
		ORDER BY m.position, m.extension_number
	`, slug)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var ext string
		if err := rows.Scan(&ext); err != nil {
			return nil, err
		}
		out = append(out, ext)
	}
	return out, rows.Err()
}

// --- Webhooks ---

type Webhook struct {
	ID      int64
	URL     string
	Secret  string
	Events  []string
	Enabled bool
}

func (p *Pool) ListWebhooks(ctx context.Context) ([]Webhook, error) {
	rows, err := p.Query(ctx, `SELECT id, url, secret, events, enabled FROM webhooks ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Webhook
	for rows.Next() {
		var w Webhook
		if err := rows.Scan(&w.ID, &w.URL, &w.Secret, &w.Events, &w.Enabled); err != nil {
			return nil, err
		}
		out = append(out, w)
	}
	return out, rows.Err()
}

func (p *Pool) InsertWebhook(ctx context.Context, url, secret string, events []string) (int64, error) {
	if len(events) == 0 {
		events = []string{"call.ended"}
	}
	var id int64
	err := p.QueryRow(ctx, `
		INSERT INTO webhooks (url, secret, events) VALUES ($1,$2,$3) RETURNING id
	`, url, secret, events).Scan(&id)
	return id, err
}

func (p *Pool) DeleteWebhook(ctx context.Context, id int64) error {
	ct, err := p.Exec(ctx, `DELETE FROM webhooks WHERE id = $1`, id)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		return fmt.Errorf("webhook not found")
	}
	return nil
}

// --- SIP trunks ---

type SIPTrunk struct {
	ID            int64
	Name          string
	SipHost       string
	SipPort       int
	Transport     string
	AuthUsername  string
	AuthPassword  string
	FromUser      string
	RegisterURI   string
	ContactUser   string
	Priority      int
	Enabled       bool
}

func (p *Pool) ListSIPTrunks(ctx context.Context) ([]SIPTrunk, error) {
	rows, err := p.Query(ctx, `
		SELECT id, name, sip_host, sip_port, transport, auth_username, auth_password,
			from_user, register_uri, contact_user, priority, enabled
		FROM sip_trunks ORDER BY priority DESC, id ASC
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SIPTrunk
	for rows.Next() {
		var t SIPTrunk
		if err := rows.Scan(&t.ID, &t.Name, &t.SipHost, &t.SipPort, &t.Transport, &t.AuthUsername, &t.AuthPassword,
			&t.FromUser, &t.RegisterURI, &t.ContactUser, &t.Priority, &t.Enabled); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func (p *Pool) ListEnabledTrunks(ctx context.Context) ([]SIPTrunk, error) {
	rows, err := p.Query(ctx, `
		SELECT id, name, sip_host, sip_port, transport, auth_username, auth_password,
			from_user, register_uri, contact_user, priority, enabled
		FROM sip_trunks WHERE enabled = true
		ORDER BY priority DESC, id ASC
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SIPTrunk
	for rows.Next() {
		var t SIPTrunk
		if err := rows.Scan(&t.ID, &t.Name, &t.SipHost, &t.SipPort, &t.Transport, &t.AuthUsername, &t.AuthPassword,
			&t.FromUser, &t.RegisterURI, &t.ContactUser, &t.Priority, &t.Enabled); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func (p *Pool) InsertSIPTrunk(ctx context.Context, t SIPTrunk) (int64, error) {
	var id int64
	err := p.QueryRow(ctx, `
		INSERT INTO sip_trunks (name, sip_host, sip_port, transport, auth_username, auth_password,
			from_user, register_uri, contact_user, priority, enabled)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id
	`, t.Name, t.SipHost, t.SipPort, t.Transport, t.AuthUsername, t.AuthPassword, t.FromUser, t.RegisterURI, t.ContactUser, t.Priority, t.Enabled).Scan(&id)
	return id, err
}

func (p *Pool) DeleteSIPTrunk(ctx context.Context, id int64) error {
	ct, err := p.Exec(ctx, `DELETE FROM sip_trunks WHERE id = $1`, id)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		return fmt.Errorf("trunk not found")
	}
	return nil
}

// --- Voicemail ---

func (p *Pool) CountVoicemailMessages(ctx context.Context, mailbox string) (int, error) {
	var n int
	err := p.QueryRow(ctx, `SELECT count(*)::int FROM voicemail_messages WHERE mailbox_ext = $1`, mailbox).Scan(&n)
	return n, err
}

func (p *Pool) InsertVoicemailMessage(ctx context.Context, mailbox, caller, path string, durationMs int) error {
	_, err := p.Exec(ctx, `
		INSERT INTO voicemail_messages (mailbox_ext, caller_ext, file_path, duration_ms)
		VALUES ($1,$2,$3,$4)
	`, mailbox, caller, path, durationMs)
	return err
}

type VoicemailListItem struct {
	ID         int64     `json:"id"`
	CallerExt  string    `json:"caller_ext"`
	DurationMs int       `json:"duration_ms"`
	CreatedAt  time.Time `json:"created_at"`
}

func (p *Pool) ListVoicemailMessages(ctx context.Context, mailbox string) ([]VoicemailListItem, error) {
	rows, err := p.Query(ctx, `
		SELECT id, coalesce(caller_ext,''), duration_ms, created_at
		FROM voicemail_messages WHERE mailbox_ext = $1 ORDER BY created_at DESC LIMIT 200
	`, mailbox)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []VoicemailListItem
	for rows.Next() {
		var v VoicemailListItem
		if err := rows.Scan(&v.ID, &v.CallerExt, &v.DurationMs, &v.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

func (p *Pool) GetVoicemailPath(ctx context.Context, id int64, mailbox string) (string, error) {
	var path string
	err := p.QueryRow(ctx, `SELECT file_path FROM voicemail_messages WHERE id = $1 AND mailbox_ext = $2`, id, mailbox).Scan(&path)
	return path, err
}
