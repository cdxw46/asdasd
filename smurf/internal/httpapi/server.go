package httpapi

import (
	"context"
	"crypto/tls"
	"embed"
	"encoding/json"
	"errors"
	"io/fs"
	"net/http"
	"path"
	"strconv"
	"strings"
	"time"

	"smurf/internal/auth"
	"smurf/internal/config"
	"smurf/internal/db"
	"smurf/internal/pbx"
	"smurf/internal/util"
)

//go:embed web/*
var webFS embed.FS

type Server struct {
	cfg      *config.Config
	store    *db.Store
	pbx      *pbx.Engine
	logger   *util.Logger
	http     *http.Server
	tokenTTL time.Duration
}

func New(cfg *config.Config, store *db.Store, pbxEngine *pbx.Engine, logger *util.Logger) *Server {
	s := &Server{
		cfg:      cfg,
		store:    store,
		pbx:      pbxEngine,
		logger:   logger,
		tokenTTL: time.Duration(cfg.Security.AdminTokenHours) * time.Hour,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", s.handleHealth)
	mux.HandleFunc("/api/login", s.handleLogin)
	mux.Handle("/api/extensions", s.authRequired(http.HandlerFunc(s.handleExtensions)))
	mux.Handle("/api/registrations", s.authRequired(http.HandlerFunc(s.handleRegistrations)))
	mux.Handle("/api/cdr", s.authRequired(http.HandlerFunc(s.handleCDR)))
	mux.Handle("/api/stats", s.authRequired(http.HandlerFunc(s.handleStats)))
	mux.Handle("/api/snapshot", s.authRequired(http.HandlerFunc(s.handleSnapshot)))
	mux.Handle("/", s.staticHandler())

	s.http = &http.Server{
		Addr:              cfg.HTTP.HTTPS,
		Handler:           loggingMiddleware(logger, mux),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       20 * time.Second,
		WriteTimeout:      20 * time.Second,
		IdleTimeout:       60 * time.Second,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
		},
	}
	return s
}

func (s *Server) Start() error {
	s.logger.Info("starting HTTPS admin on %s", s.cfg.HTTP.HTTPS)
	return s.http.ListenAndServeTLS(s.cfg.HTTP.TLSCert, s.cfg.HTTP.TLSKey)
}

func (s *Server) Shutdown(ctx context.Context) error {
	return s.http.Shutdown(ctx)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"name":   "SMURF",
		"ts":     time.Now().UTC(),
	})
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid json")
		return
	}
	user, err := s.store.GetAdminUser(r.Context(), body.Username)
	if err != nil {
		writeError(w, http.StatusUnauthorized, "invalid credentials")
		return
	}
	if !auth.VerifyPassword(body.Password, user.PasswordSalt, user.PasswordHash) {
		writeError(w, http.StatusUnauthorized, "invalid credentials")
		return
	}
	token, err := auth.GenerateJWT(s.cfg.Security.JWTSecret, user.Username, user.Role, s.tokenTTL)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "token generation failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"token": token,
		"user": map[string]any{
			"username": user.Username,
			"role":     user.Role,
		},
	})
}

func (s *Server) handleExtensions(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		list, err := s.store.ListExtensions(r.Context())
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, list)
	case http.MethodPost:
		var body struct {
			Number      string `json:"number"`
			DisplayName string `json:"display_name"`
			Password    string `json:"password"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeError(w, http.StatusBadRequest, "invalid json")
			return
		}
		ext, err := s.store.CreateExtension(r.Context(), s.cfg, body.Number, body.DisplayName, body.Password)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		writeJSON(w, http.StatusCreated, ext)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (s *Server) handleRegistrations(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	regs, err := s.store.ListRegistrations(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, regs)
}

func (s *Server) handleCDR(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	limit := 100
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if v, err := strconv.Atoi(raw); err == nil {
			limit = v
		}
	}
	rows, err := s.store.ListCDR(r.Context(), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rows)
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	stats := s.pbx.Stats()
	writeJSON(w, http.StatusOK, stats)
}

func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	extensions, err := s.store.ListExtensions(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	registrations, err := s.store.ListRegistrations(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	cdr, err := s.store.ListCDR(r.Context(), 100)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if extensions == nil {
		extensions = []db.Extension{}
	}
	if registrations == nil {
		registrations = []db.Registration{}
	}
	if cdr == nil {
		cdr = []db.CDR{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"extensions":    extensions,
		"registrations": registrations,
		"cdr":           cdr,
		"active_calls":  s.pbx.Snapshot(),
		"stats":         s.pbx.Stats(),
	})
}

func (s *Server) authRequired(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authz := r.Header.Get("Authorization")
		if authz == "" || !strings.HasPrefix(authz, "Bearer ") {
			writeError(w, http.StatusUnauthorized, "missing bearer token")
			return
		}
		token := strings.TrimSpace(strings.TrimPrefix(authz, "Bearer "))
		claims, err := auth.ParseJWT(s.cfg.Security.JWTSecret, token)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid token")
			return
		}
		ctx := context.WithValue(r.Context(), ctxKeyClaims{}, claims)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

type ctxKeyClaims struct{}

func (s *Server) staticHandler() http.Handler {
	sub, err := fs.Sub(webFS, "web")
	if err != nil {
		panic(err)
	}
	fileServer := http.FileServer(http.FS(sub))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := path.Clean(r.URL.Path)
		if strings.HasPrefix(p, "/api/") {
			http.NotFound(w, r)
			return
		}
		if p == "/" {
			p = "/index.html"
		}
		if _, err := fs.Stat(sub, strings.TrimPrefix(p, "/")); errors.Is(err, fs.ErrNotExist) {
			r.URL.Path = "/index.html"
		}
		fileServer.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, code int, message string) {
	writeJSON(w, code, map[string]any{"error": message})
}

func loggingMiddleware(logger *util.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		logger.Info("http %s %s in %s", r.Method, r.URL.Path, time.Since(start).Round(time.Millisecond))
	})
}
