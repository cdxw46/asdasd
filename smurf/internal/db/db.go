package db

import (
	"context"
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
