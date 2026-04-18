package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type Config struct {
	Domain   string `json:"domain"`
	Realm    string `json:"realm"`
	DataDir  string `json:"data_dir"`
	LogLevel string `json:"log_level"`

	Database struct {
		Path string `json:"path"`
	} `json:"database"`

	SIP struct {
		UDP      string `json:"udp"`
		TCP      string `json:"tcp"`
		TLS      string `json:"tls"`
		TLSCert  string `json:"tls_cert"`
		TLSKey   string `json:"tls_key"`
		NonceTTL int    `json:"nonce_ttl_seconds"`
	} `json:"sip"`

	RTP struct {
		BindIP   string `json:"bind_ip"`
		PublicIP string `json:"public_ip"`
		StartPort int   `json:"start_port"`
		EndPort   int   `json:"end_port"`
		DSCP      int   `json:"dscp"`
	} `json:"rtp"`

	HTTP struct {
		HTTPS   string `json:"https"`
		TLSCert string `json:"tls_cert"`
		TLSKey  string `json:"tls_key"`
	} `json:"http"`

	Security struct {
		JWTSecret        string `json:"jwt_secret"`
		AdminUsername    string `json:"admin_username"`
		AdminPassword    string `json:"admin_password"`
		FailThreshold    int    `json:"fail_threshold"`
		BlockSeconds     int    `json:"block_seconds"`
		AdminTokenHours  int    `json:"admin_token_hours"`
	} `json:"security"`
}

func Default() *Config {
	cfg := &Config{
		Domain:   "smurf.local",
		Realm:    "smurf.local",
		DataDir:  "/var/lib/smurf",
		LogLevel: "INFO",
	}
	cfg.Database.Path = "/var/lib/smurf/smurf.db"
	cfg.SIP.UDP = "0.0.0.0:5060"
	cfg.SIP.TCP = "0.0.0.0:5060"
	cfg.SIP.TLS = "0.0.0.0:5061"
	cfg.SIP.TLSCert = "/etc/smurf/tls/server.crt"
	cfg.SIP.TLSKey = "/etc/smurf/tls/server.key"
	cfg.SIP.NonceTTL = 300
	cfg.RTP.BindIP = "0.0.0.0"
	cfg.RTP.PublicIP = "127.0.0.1"
	cfg.RTP.StartPort = 20000
	cfg.RTP.EndPort = 20998
	cfg.RTP.DSCP = 46
	cfg.HTTP.HTTPS = "0.0.0.0:5001"
	cfg.HTTP.TLSCert = "/etc/smurf/tls/server.crt"
	cfg.HTTP.TLSKey = "/etc/smurf/tls/server.key"
	cfg.Security.JWTSecret = "change-this-jwt-secret"
	cfg.Security.AdminUsername = "admin"
	cfg.Security.AdminPassword = "admin123!"
	cfg.Security.FailThreshold = 5
	cfg.Security.BlockSeconds = 900
	cfg.Security.AdminTokenHours = 12
	return cfg
}

func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	cfg := Default()
	if err := json.Unmarshal(raw, cfg); err != nil {
		return nil, err
	}
	return cfg, nil
}

func Ensure(path string) (*Config, error) {
	if _, err := os.Stat(path); err == nil {
		return Load(path)
	} else if !os.IsNotExist(err) {
		return nil, err
	}

	cfg := Default()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	if err := cfg.Save(path); err != nil {
		return nil, err
	}
	return cfg, nil
}

func (c *Config) Save(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	return os.WriteFile(path, raw, 0o640)
}

func (c *Config) Validate() error {
	if c.Domain == "" || c.Realm == "" {
		return fmt.Errorf("domain and realm are required")
	}
	if c.Database.Path == "" {
		return fmt.Errorf("database path is required")
	}
	if c.RTP.StartPort <= 0 || c.RTP.EndPort <= 0 || c.RTP.EndPort <= c.RTP.StartPort {
		return fmt.Errorf("invalid RTP port range")
	}
	if c.Security.JWTSecret == "" {
		return fmt.Errorf("jwt secret is required")
	}
	return nil
}
