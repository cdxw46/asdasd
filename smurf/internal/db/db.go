package db

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"smurf/internal/auth"
	"smurf/internal/config"
)

type Store struct {
	SQL *sql.DB
}

type Extension struct {
	ID           int64     `json:"id"`
	Number       string    `json:"number"`
	DisplayName  string    `json:"display_name"`
	Password     string    `json:"password,omitempty"`
	HA1MD5       string    `json:"ha1_md5,omitempty"`
	HA1SHA256    string    `json:"ha1_sha256,omitempty"`
	MaxCalls     int       `json:"max_calls"`
	VoicemailPIN string    `json:"voicemail_pin,omitempty"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

type AdminUser struct {
	ID           int64     `json:"id"`
	Username     string    `json:"username"`
	Role         string    `json:"role"`
	PasswordSalt string    `json:"-"`
	PasswordHash string    `json:"-"`
	CreatedAt    time.Time `json:"created_at"`
}

type Registration struct {
	ID         int64     `json:"id"`
	Extension  string    `json:"extension"`
	ContactURI string    `json:"contact_uri"`
	SourceAddr string    `json:"source_addr"`
	Transport  string    `json:"transport"`
	ExpiresAt  time.Time `json:"expires_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

type CDR struct {
	ID            int64     `json:"id"`
	CallID        string    `json:"call_id"`
	FromExtension string    `json:"from_extension"`
	ToExtension   string    `json:"to_extension"`
	State         string    `json:"state"`
	StartedAt     time.Time `json:"started_at"`
	AnsweredAt    time.Time `json:"answered_at,omitempty"`
	EndedAt       time.Time `json:"ended_at,omitempty"`
	DurationSec   int       `json:"duration_sec"`
}

type PresenceState struct {
	Extension string    `json:"extension"`
	Status    string    `json:"status"`
	Note      string    `json:"note"`
	UpdatedAt time.Time `json:"updated_at"`
}

type ChatMessage struct {
	ID          int64     `json:"id"`
	FromExt     string    `json:"from_extension"`
	ToExt       string    `json:"to_extension"`
	Body        string    `json:"body"`
	CreatedAt   time.Time `json:"created_at"`
	DeliveredAt time.Time `json:"delivered_at,omitempty"`
}

type VoicemailMessage struct {
	ID          int64     `json:"id"`
	Extension   string    `json:"extension"`
	FromExt     string    `json:"from_extension"`
	CallID      string    `json:"call_id"`
	FilePath    string    `json:"file_path"`
	DurationSec int       `json:"duration_sec"`
	Listened    bool      `json:"listened"`
	CreatedAt   time.Time `json:"created_at"`
}

type Recording struct {
	ID          int64     `json:"id"`
	CallID      string    `json:"call_id"`
	FromExt     string    `json:"from_extension"`
	ToExt       string    `json:"to_extension"`
	FilePath    string    `json:"file_path"`
	Format      string    `json:"format"`
	DurationSec int       `json:"duration_sec"`
	CreatedAt   time.Time `json:"created_at"`
}

func Open(cfg *config.Config) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(cfg.Database.Path), 0o755); err != nil {
		return nil, err
	}
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)", cfg.Database.Path)
	sqlDB, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	sqlDB.SetConnMaxLifetime(0)
	sqlDB.SetMaxIdleConns(2)
	sqlDB.SetMaxOpenConns(8)
	store := &Store{SQL: sqlDB}
	if err := store.migrate(context.Background(), cfg); err != nil {
		sqlDB.Close()
		return nil, err
	}
	return store, nil
}

func (s *Store) Close() error {
	if s == nil || s.SQL == nil {
		return nil
	}
	return s.SQL.Close()
}

func (s *Store) migrate(ctx context.Context, cfg *config.Config) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS extensions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			number TEXT NOT NULL UNIQUE,
			display_name TEXT NOT NULL,
			password TEXT NOT NULL,
			ha1_md5 TEXT NOT NULL,
			ha1_sha256 TEXT NOT NULL,
			max_calls INTEGER NOT NULL DEFAULT 4,
			voicemail_pin TEXT NOT NULL DEFAULT '1234',
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS admin_users (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			username TEXT NOT NULL UNIQUE,
			password_salt TEXT NOT NULL,
			password_hash TEXT NOT NULL,
			role TEXT NOT NULL,
			created_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS registrations (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			extension TEXT NOT NULL,
			contact_uri TEXT NOT NULL,
			source_addr TEXT NOT NULL,
			transport TEXT NOT NULL,
			expires_at TEXT NOT NULL,
			updated_at TEXT NOT NULL,
			UNIQUE(extension, contact_uri, transport)
		)`,
		`CREATE TABLE IF NOT EXISTS cdr (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			call_id TEXT NOT NULL UNIQUE,
			from_extension TEXT NOT NULL,
			to_extension TEXT NOT NULL,
			state TEXT NOT NULL,
			started_at TEXT NOT NULL,
			answered_at TEXT,
			ended_at TEXT,
			duration_sec INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS failed_auth (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			source_ip TEXT NOT NULL,
			username TEXT NOT NULL,
			fail_count INTEGER NOT NULL DEFAULT 1,
			last_failed_at TEXT NOT NULL,
			blocked_until TEXT
		)`,
		`CREATE TABLE IF NOT EXISTS settings (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS presence (
			extension TEXT PRIMARY KEY,
			status TEXT NOT NULL,
			note TEXT NOT NULL DEFAULT '',
			updated_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS chat_messages (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			from_extension TEXT NOT NULL,
			to_extension TEXT NOT NULL,
			body TEXT NOT NULL,
			created_at TEXT NOT NULL,
			delivered_at TEXT
		)`,
		`CREATE TABLE IF NOT EXISTS voicemail_messages (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			extension TEXT NOT NULL,
			from_extension TEXT NOT NULL,
			call_id TEXT NOT NULL,
			file_path TEXT NOT NULL,
			duration_sec INTEGER NOT NULL DEFAULT 0,
			listened INTEGER NOT NULL DEFAULT 0,
			created_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS recordings (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			call_id TEXT NOT NULL,
			from_extension TEXT NOT NULL,
			to_extension TEXT NOT NULL,
			file_path TEXT NOT NULL,
			format TEXT NOT NULL,
			duration_sec INTEGER NOT NULL DEFAULT 0,
			created_at TEXT NOT NULL
		)`,
	}
	for _, stmt := range stmts {
		if _, err := s.SQL.ExecContext(ctx, stmt); err != nil {
			return err
		}
	}
	return s.seed(ctx, cfg)
}

func (s *Store) seed(ctx context.Context, cfg *config.Config) error {
	now := time.Now().UTC().Format(time.RFC3339)
	var count int
	if err := s.SQL.QueryRowContext(ctx, `SELECT COUNT(*) FROM admin_users`).Scan(&count); err != nil {
		return err
	}
	if count == 0 {
		salt, hash := auth.HashPassword(cfg.Security.AdminPassword)
		if _, err := s.SQL.ExecContext(ctx, `INSERT INTO admin_users(username, password_salt, password_hash, role, created_at) VALUES(?,?,?,?,?)`,
			cfg.Security.AdminUsername, salt, hash, "superadmin", now,
		); err != nil {
			return err
		}
	}
	if err := s.SQL.QueryRowContext(ctx, `SELECT COUNT(*) FROM extensions`).Scan(&count); err != nil {
		return err
	}
	if count == 0 {
		password := "12345"
		md5HA1 := auth.ComputeHA1("1000", cfg.Realm, password, "MD5")
		shaHA1 := auth.ComputeHA1("1000", cfg.Realm, password, "SHA-256")
		if _, err := s.SQL.ExecContext(ctx, `
			INSERT INTO extensions(number, display_name, password, ha1_md5, ha1_sha256, max_calls, voicemail_pin, created_at, updated_at)
			VALUES(?,?,?,?,?,?,?,?,?)`,
			"1000", "Test Extension 1000", password, md5HA1, shaHA1, 4, "1234", now, now,
		); err != nil {
			return err
		}
	}
	for k, v := range map[string]string{
		"pbx.domain":      cfg.Domain,
		"sip.realm":       cfg.Realm,
		"web.listen_addr": cfg.HTTP.HTTPS,
	} {
		if _, err := s.SQL.ExecContext(ctx,
			`INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO NOTHING`,
			k, v, now,
		); err != nil {
			return err
		}
	}
	extRows, err := s.ListExtensions(ctx)
	if err != nil {
		return err
	}
	for _, ext := range extRows {
		if _, err := s.SQL.ExecContext(ctx,
			`INSERT INTO presence(extension, status, note, updated_at) VALUES(?,?,?,?) ON CONFLICT(extension) DO NOTHING`,
			ext.Number, "offline", "", now,
		); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) ListExtensions(ctx context.Context) ([]Extension, error) {
	rows, err := s.SQL.QueryContext(ctx, `
		SELECT id, number, display_name, password, ha1_md5, ha1_sha256, max_calls, voicemail_pin, created_at, updated_at
		FROM extensions ORDER BY number`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Extension
	for rows.Next() {
		var ext Extension
		var createdAt, updatedAt string
		if err := rows.Scan(&ext.ID, &ext.Number, &ext.DisplayName, &ext.Password, &ext.HA1MD5, &ext.HA1SHA256, &ext.MaxCalls, &ext.VoicemailPIN, &createdAt, &updatedAt); err != nil {
			return nil, err
		}
		ext.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
		ext.UpdatedAt, _ = time.Parse(time.RFC3339, updatedAt)
		out = append(out, ext)
	}
	return out, rows.Err()
}

func (s *Store) CreateExtension(ctx context.Context, cfg *config.Config, number, displayName, password string) (*Extension, error) {
	number = strings.TrimSpace(number)
	displayName = strings.TrimSpace(displayName)
	password = strings.TrimSpace(password)
	if number == "" || displayName == "" || password == "" {
		return nil, errors.New("number, display_name and password are required")
	}
	now := time.Now().UTC().Format(time.RFC3339)
	md5HA1 := auth.ComputeHA1(number, cfg.Realm, password, "MD5")
	shaHA1 := auth.ComputeHA1(number, cfg.Realm, password, "SHA-256")
	res, err := s.SQL.ExecContext(ctx, `
		INSERT INTO extensions(number, display_name, password, ha1_md5, ha1_sha256, max_calls, voicemail_pin, created_at, updated_at)
		VALUES(?,?,?,?,?,?,?,?,?)`,
		number, displayName, password, md5HA1, shaHA1, 4, "1234", now, now,
	)
	if err != nil {
		return nil, err
	}
	id, _ := res.LastInsertId()
	_, _ = s.SQL.ExecContext(ctx,
		`INSERT INTO presence(extension, status, note, updated_at) VALUES(?,?,?,?) ON CONFLICT(extension) DO NOTHING`,
		number, "offline", "", now,
	)
	return s.GetExtensionByID(ctx, id)
}

func (s *Store) GetExtensionByID(ctx context.Context, id int64) (*Extension, error) {
	row := s.SQL.QueryRowContext(ctx, `
		SELECT id, number, display_name, password, ha1_md5, ha1_sha256, max_calls, voicemail_pin, created_at, updated_at
		FROM extensions WHERE id = ?`, id)
	var ext Extension
	var createdAt, updatedAt string
	if err := row.Scan(&ext.ID, &ext.Number, &ext.DisplayName, &ext.Password, &ext.HA1MD5, &ext.HA1SHA256, &ext.MaxCalls, &ext.VoicemailPIN, &createdAt, &updatedAt); err != nil {
		return nil, err
	}
	ext.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
	ext.UpdatedAt, _ = time.Parse(time.RFC3339, updatedAt)
	return &ext, nil
}

func (s *Store) GetExtensionByNumber(ctx context.Context, number string) (*Extension, error) {
	row := s.SQL.QueryRowContext(ctx, `
		SELECT id, number, display_name, password, ha1_md5, ha1_sha256, max_calls, voicemail_pin, created_at, updated_at
		FROM extensions WHERE number = ?`, strings.TrimSpace(number))
	var ext Extension
	var createdAt, updatedAt string
	if err := row.Scan(&ext.ID, &ext.Number, &ext.DisplayName, &ext.Password, &ext.HA1MD5, &ext.HA1SHA256, &ext.MaxCalls, &ext.VoicemailPIN, &createdAt, &updatedAt); err != nil {
		return nil, err
	}
	ext.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
	ext.UpdatedAt, _ = time.Parse(time.RFC3339, updatedAt)
	return &ext, nil
}

func (s *Store) GetAdminUser(ctx context.Context, username string) (*AdminUser, error) {
	row := s.SQL.QueryRowContext(ctx, `
		SELECT id, username, password_salt, password_hash, role, created_at
		FROM admin_users WHERE username = ?`, strings.TrimSpace(username))
	var user AdminUser
	var createdAt string
	if err := row.Scan(&user.ID, &user.Username, &user.PasswordSalt, &user.PasswordHash, &user.Role, &createdAt); err != nil {
		return nil, err
	}
	user.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
	return &user, nil
}

func (s *Store) UpsertRegistration(ctx context.Context, extension, contactURI, sourceAddr, transport string, expiresAt time.Time) error {
	now := time.Now().UTC().Format(time.RFC3339)
	_, err := s.SQL.ExecContext(ctx, `
		INSERT INTO registrations(extension, contact_uri, source_addr, transport, expires_at, updated_at)
		VALUES(?,?,?,?,?,?)
		ON CONFLICT(extension, contact_uri, transport)
		DO UPDATE SET source_addr=excluded.source_addr, expires_at=excluded.expires_at, updated_at=excluded.updated_at`,
		extension, contactURI, sourceAddr, transport, expiresAt.UTC().Format(time.RFC3339), now,
	)
	return err
}

func (s *Store) DeleteRegistration(ctx context.Context, extension, contactURI, transport string) error {
	_, err := s.SQL.ExecContext(ctx, `DELETE FROM registrations WHERE extension = ? AND contact_uri = ? AND transport = ?`, extension, contactURI, transport)
	return err
}

func (s *Store) GetRegistrations(ctx context.Context, extension string) ([]Registration, error) {
	rows, err := s.SQL.QueryContext(ctx, `
		SELECT id, extension, contact_uri, source_addr, transport, expires_at, updated_at
		FROM registrations
		WHERE extension = ? AND expires_at > ?
		ORDER BY updated_at DESC`, extension, time.Now().UTC().Format(time.RFC3339))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Registration
	for rows.Next() {
		var reg Registration
		var expiresAt, updatedAt string
		if err := rows.Scan(&reg.ID, &reg.Extension, &reg.ContactURI, &reg.SourceAddr, &reg.Transport, &expiresAt, &updatedAt); err != nil {
			return nil, err
		}
		reg.ExpiresAt, _ = time.Parse(time.RFC3339, expiresAt)
		reg.UpdatedAt, _ = time.Parse(time.RFC3339, updatedAt)
		out = append(out, reg)
	}
	return out, rows.Err()
}

func (s *Store) ListRegistrations(ctx context.Context) ([]Registration, error) {
	rows, err := s.SQL.QueryContext(ctx, `
		SELECT id, extension, contact_uri, source_addr, transport, expires_at, updated_at
		FROM registrations
		WHERE expires_at > ?
		ORDER BY updated_at DESC`, time.Now().UTC().Format(time.RFC3339))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Registration
	for rows.Next() {
		var reg Registration
		var expiresAt, updatedAt string
		if err := rows.Scan(&reg.ID, &reg.Extension, &reg.ContactURI, &reg.SourceAddr, &reg.Transport, &expiresAt, &updatedAt); err != nil {
			return nil, err
		}
		reg.ExpiresAt, _ = time.Parse(time.RFC3339, expiresAt)
		reg.UpdatedAt, _ = time.Parse(time.RFC3339, updatedAt)
		out = append(out, reg)
	}
	return out, rows.Err()
}

func (s *Store) CleanupExpiredRegistrations(ctx context.Context) error {
	_, err := s.SQL.ExecContext(ctx, `DELETE FROM registrations WHERE expires_at <= ?`, time.Now().UTC().Format(time.RFC3339))
	return err
}

func (s *Store) CreateOrUpdateCDR(ctx context.Context, cdr CDR) error {
	_, err := s.SQL.ExecContext(ctx, `
		INSERT INTO cdr(call_id, from_extension, to_extension, state, started_at, answered_at, ended_at, duration_sec)
		VALUES(?,?,?,?,?,?,?,?)
		ON CONFLICT(call_id) DO UPDATE SET
			state=excluded.state,
			answered_at=excluded.answered_at,
			ended_at=excluded.ended_at,
			duration_sec=excluded.duration_sec`,
		cdr.CallID,
		cdr.FromExtension,
		cdr.ToExtension,
		cdr.State,
		cdr.StartedAt.UTC().Format(time.RFC3339),
		nullTime(cdr.AnsweredAt),
		nullTime(cdr.EndedAt),
		cdr.DurationSec,
	)
	return err
}

func (s *Store) ListCDR(ctx context.Context, limit int) ([]CDR, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := s.SQL.QueryContext(ctx, `
		SELECT id, call_id, from_extension, to_extension, state, started_at, answered_at, ended_at, duration_sec
		FROM cdr ORDER BY started_at DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CDR
	for rows.Next() {
		var c CDR
		var startedAt, answeredAt, endedAt sql.NullString
		if err := rows.Scan(&c.ID, &c.CallID, &c.FromExtension, &c.ToExtension, &c.State, &startedAt, &answeredAt, &endedAt, &c.DurationSec); err != nil {
			return nil, err
		}
		if startedAt.Valid {
			c.StartedAt, _ = time.Parse(time.RFC3339, startedAt.String)
		}
		if answeredAt.Valid {
			c.AnsweredAt, _ = time.Parse(time.RFC3339, answeredAt.String)
		}
		if endedAt.Valid {
			c.EndedAt, _ = time.Parse(time.RFC3339, endedAt.String)
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func (s *Store) RecordFailedAuth(ctx context.Context, sourceIP, username string, blockWindow time.Duration, threshold int) (blockedUntil *time.Time, err error) {
	sourceIP = strings.TrimSpace(sourceIP)
	username = strings.TrimSpace(username)
	now := time.Now().UTC()
	var count int
	var existing string
	err = s.SQL.QueryRowContext(ctx, `SELECT fail_count, COALESCE(blocked_until, '') FROM failed_auth WHERE source_ip = ? AND username = ?`, sourceIP, username).Scan(&count, &existing)
	switch {
	case err == sql.ErrNoRows:
		count = 1
		_, err = s.SQL.ExecContext(ctx, `INSERT INTO failed_auth(source_ip, username, fail_count, last_failed_at) VALUES(?,?,?,?)`,
			sourceIP, username, count, now.Format(time.RFC3339),
		)
	case err == nil:
		count++
		var blocked string
		if count >= threshold {
			t := now.Add(blockWindow)
			blocked = t.Format(time.RFC3339)
			blockedUntil = &t
		}
		_, err = s.SQL.ExecContext(ctx, `UPDATE failed_auth SET fail_count=?, last_failed_at=?, blocked_until=? WHERE source_ip=? AND username=?`,
			count, now.Format(time.RFC3339), blocked, sourceIP, username,
		)
	default:
		return nil, err
	}
	return blockedUntil, err
}

func (s *Store) ResetFailedAuth(ctx context.Context, sourceIP, username string) error {
	_, err := s.SQL.ExecContext(ctx, `DELETE FROM failed_auth WHERE source_ip = ? AND username = ?`, sourceIP, username)
	return err
}

func (s *Store) IsBlocked(ctx context.Context, sourceIP, username string) (bool, time.Time, error) {
	var blocked sql.NullString
	err := s.SQL.QueryRowContext(ctx, `SELECT blocked_until FROM failed_auth WHERE source_ip = ? AND username = ?`, sourceIP, username).Scan(&blocked)
	if errors.Is(err, sql.ErrNoRows) {
		return false, time.Time{}, nil
	}
	if err != nil {
		return false, time.Time{}, err
	}
	if !blocked.Valid || blocked.String == "" {
		return false, time.Time{}, nil
	}
	t, err := time.Parse(time.RFC3339, blocked.String)
	if err != nil {
		return false, time.Time{}, err
	}
	return time.Now().UTC().Before(t), t, nil
}

func nullTime(t time.Time) any {
	if t.IsZero() {
		return nil
	}
	return t.UTC().Format(time.RFC3339)
}

func (s *Store) UpsertPresence(ctx context.Context, extension, status, note string) error {
	status = strings.TrimSpace(status)
	if status == "" {
		status = "offline"
	}
	_, err := s.SQL.ExecContext(ctx, `
		INSERT INTO presence(extension, status, note, updated_at)
		VALUES(?,?,?,?)
		ON CONFLICT(extension) DO UPDATE SET
			status=excluded.status,
			note=excluded.note,
			updated_at=excluded.updated_at`,
		strings.TrimSpace(extension), status, strings.TrimSpace(note), time.Now().UTC().Format(time.RFC3339),
	)
	return err
}

func (s *Store) ListPresence(ctx context.Context) ([]PresenceState, error) {
	rows, err := s.SQL.QueryContext(ctx, `SELECT extension, status, note, updated_at FROM presence ORDER BY extension`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PresenceState
	for rows.Next() {
		var item PresenceState
		var updatedAt string
		if err := rows.Scan(&item.Extension, &item.Status, &item.Note, &updatedAt); err != nil {
			return nil, err
		}
		item.UpdatedAt, _ = time.Parse(time.RFC3339, updatedAt)
		out = append(out, item)
	}
	return out, rows.Err()
}

func (s *Store) CreateChatMessage(ctx context.Context, fromExt, toExt, body string) (*ChatMessage, error) {
	fromExt = strings.TrimSpace(fromExt)
	toExt = strings.TrimSpace(toExt)
	body = strings.TrimSpace(body)
	if fromExt == "" || toExt == "" || body == "" {
		return nil, errors.New("from_extension, to_extension and body are required")
	}
	now := time.Now().UTC().Format(time.RFC3339)
	res, err := s.SQL.ExecContext(ctx,
		`INSERT INTO chat_messages(from_extension, to_extension, body, created_at) VALUES(?,?,?,?)`,
		fromExt, toExt, body, now,
	)
	if err != nil {
		return nil, err
	}
	id, _ := res.LastInsertId()
	return s.GetChatMessage(ctx, id)
}

func (s *Store) GetChatMessage(ctx context.Context, id int64) (*ChatMessage, error) {
	row := s.SQL.QueryRowContext(ctx, `
		SELECT id, from_extension, to_extension, body, created_at, delivered_at
		FROM chat_messages WHERE id = ?`, id,
	)
	var msg ChatMessage
	var createdAt string
	var deliveredAt sql.NullString
	if err := row.Scan(&msg.ID, &msg.FromExt, &msg.ToExt, &msg.Body, &createdAt, &deliveredAt); err != nil {
		return nil, err
	}
	msg.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
	if deliveredAt.Valid {
		msg.DeliveredAt, _ = time.Parse(time.RFC3339, deliveredAt.String)
	}
	return &msg, nil
}

func (s *Store) ListChatMessages(ctx context.Context, extension string, limit int) ([]ChatMessage, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	extension = strings.TrimSpace(extension)
	rows, err := s.SQL.QueryContext(ctx, `
		SELECT id, from_extension, to_extension, body, created_at, delivered_at
		FROM chat_messages
		WHERE from_extension = ? OR to_extension = ?
		ORDER BY id DESC
		LIMIT ?`, extension, extension, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ChatMessage
	for rows.Next() {
		var msg ChatMessage
		var createdAt string
		var deliveredAt sql.NullString
		if err := rows.Scan(&msg.ID, &msg.FromExt, &msg.ToExt, &msg.Body, &createdAt, &deliveredAt); err != nil {
			return nil, err
		}
		msg.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
		if deliveredAt.Valid {
			msg.DeliveredAt, _ = time.Parse(time.RFC3339, deliveredAt.String)
		}
		out = append(out, msg)
	}
	return out, rows.Err()
}

func (s *Store) CreateVoicemailMessage(ctx context.Context, item VoicemailMessage) (*VoicemailMessage, error) {
	now := time.Now().UTC().Format(time.RFC3339)
	res, err := s.SQL.ExecContext(ctx, `
		INSERT INTO voicemail_messages(extension, from_extension, call_id, file_path, duration_sec, listened, created_at)
		VALUES(?,?,?,?,?,?,?)`,
		strings.TrimSpace(item.Extension),
		strings.TrimSpace(item.FromExt),
		strings.TrimSpace(item.CallID),
		strings.TrimSpace(item.FilePath),
		item.DurationSec,
		boolToInt(item.Listened),
		now,
	)
	if err != nil {
		return nil, err
	}
	id, _ := res.LastInsertId()
	return s.GetVoicemailMessage(ctx, id)
}

func (s *Store) GetVoicemailMessage(ctx context.Context, id int64) (*VoicemailMessage, error) {
	row := s.SQL.QueryRowContext(ctx, `
		SELECT id, extension, from_extension, call_id, file_path, duration_sec, listened, created_at
		FROM voicemail_messages WHERE id = ?`, id,
	)
	var item VoicemailMessage
	var listened int
	var createdAt string
	if err := row.Scan(&item.ID, &item.Extension, &item.FromExt, &item.CallID, &item.FilePath, &item.DurationSec, &listened, &createdAt); err != nil {
		return nil, err
	}
	item.Listened = listened != 0
	item.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
	return &item, nil
}

func (s *Store) ListVoicemailMessages(ctx context.Context, extension string) ([]VoicemailMessage, error) {
	rows, err := s.SQL.QueryContext(ctx, `
		SELECT id, extension, from_extension, call_id, file_path, duration_sec, listened, created_at
		FROM voicemail_messages
		WHERE extension = ?
		ORDER BY id DESC`, strings.TrimSpace(extension),
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []VoicemailMessage
	for rows.Next() {
		var item VoicemailMessage
		var listened int
		var createdAt string
		if err := rows.Scan(&item.ID, &item.Extension, &item.FromExt, &item.CallID, &item.FilePath, &item.DurationSec, &listened, &createdAt); err != nil {
			return nil, err
		}
		item.Listened = listened != 0
		item.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
		out = append(out, item)
	}
	return out, rows.Err()
}

func (s *Store) CreateRecording(ctx context.Context, item Recording) (*Recording, error) {
	now := time.Now().UTC().Format(time.RFC3339)
	res, err := s.SQL.ExecContext(ctx, `
		INSERT INTO recordings(call_id, from_extension, to_extension, file_path, format, duration_sec, created_at)
		VALUES(?,?,?,?,?,?,?)`,
		strings.TrimSpace(item.CallID),
		strings.TrimSpace(item.FromExt),
		strings.TrimSpace(item.ToExt),
		strings.TrimSpace(item.FilePath),
		strings.TrimSpace(item.Format),
		item.DurationSec,
		now,
	)
	if err != nil {
		return nil, err
	}
	id, _ := res.LastInsertId()
	return s.GetRecording(ctx, id)
}

func (s *Store) GetRecording(ctx context.Context, id int64) (*Recording, error) {
	row := s.SQL.QueryRowContext(ctx, `
		SELECT id, call_id, from_extension, to_extension, file_path, format, duration_sec, created_at
		FROM recordings WHERE id = ?`, id,
	)
	var item Recording
	var createdAt string
	if err := row.Scan(&item.ID, &item.CallID, &item.FromExt, &item.ToExt, &item.FilePath, &item.Format, &item.DurationSec, &createdAt); err != nil {
		return nil, err
	}
	item.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
	return &item, nil
}

func (s *Store) ListRecordings(ctx context.Context, limit int) ([]Recording, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := s.SQL.QueryContext(ctx, `
		SELECT id, call_id, from_extension, to_extension, file_path, format, duration_sec, created_at
		FROM recordings ORDER BY id DESC LIMIT ?`, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Recording
	for rows.Next() {
		var item Recording
		var createdAt string
		if err := rows.Scan(&item.ID, &item.CallID, &item.FromExt, &item.ToExt, &item.FilePath, &item.Format, &item.DurationSec, &createdAt); err != nil {
			return nil, err
		}
		item.CreatedAt, _ = time.Parse(time.RFC3339, createdAt)
		out = append(out, item)
	}
	return out, rows.Err()
}

func boolToInt(v bool) int {
	if v {
		return 1
	}
	return 0
}
