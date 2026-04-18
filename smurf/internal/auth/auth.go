package auth

import (
	"crypto/hmac"
	"crypto/md5"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

type Claims struct {
	Sub  string `json:"sub"`
	Role string `json:"role"`
	Exp  int64  `json:"exp"`
	Iat  int64  `json:"iat"`
}

func RandomHex(size int) string {
	buf := make([]byte, size)
	if _, err := rand.Read(buf); err != nil {
		panic(err)
	}
	return hex.EncodeToString(buf)
}

func PasswordHash(password, salt string) string {
	if salt == "" {
		salt = RandomHex(16)
	}
	sum := []byte(password + ":" + salt)
	for i := 0; i < 120000; i++ {
		hash := sha256.Sum256(sum)
		sum = hash[:]
	}
	return hex.EncodeToString(sum)
}

func VerifyPassword(password, salt, expected string) bool {
	return hmac.Equal([]byte(PasswordHash(password, salt)), []byte(expected))
}

func HashPassword(password string) (salt string, hash string) {
	salt = RandomHex(16)
	hash = PasswordHash(password, salt)
	return salt, hash
}

func ComputeHA1(username, realm, password, algorithm string) string {
	input := fmt.Sprintf("%s:%s:%s", username, realm, password)
	switch strings.ToUpper(strings.TrimSpace(algorithm)) {
	case "SHA-256", "SHA256":
		sum := sha256.Sum256([]byte(input))
		return hex.EncodeToString(sum[:])
	default:
		return md5Hex(input)
	}
}

func md5Hex(input string) string {
	sum := md5.Sum([]byte(input))
	return hex.EncodeToString(sum[:])
}

type simpleJWTHeader struct {
	Alg string `json:"alg"`
	Typ string `json:"typ"`
}

func GenerateJWT(secret, subject, role string, ttl time.Duration) (string, error) {
	header, err := json.Marshal(simpleJWTHeader{Alg: "HS256", Typ: "JWT"})
	if err != nil {
		return "", err
	}
	now := time.Now().UTC()
	claims, err := json.Marshal(Claims{
		Sub:  subject,
		Role: role,
		Iat:  now.Unix(),
		Exp:  now.Add(ttl).Unix(),
	})
	if err != nil {
		return "", err
	}
	enc := base64.RawURLEncoding
	unsigned := enc.EncodeToString(header) + "." + enc.EncodeToString(claims)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(unsigned))
	sig := enc.EncodeToString(mac.Sum(nil))
	return unsigned + "." + sig, nil
}

func ParseJWT(secret, token string) (*Claims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("invalid token format")
	}
	enc := base64.RawURLEncoding
	unsigned := parts[0] + "." + parts[1]
	wantMac := hmac.New(sha256.New, []byte(secret))
	wantMac.Write([]byte(unsigned))
	wantSig := wantMac.Sum(nil)
	gotSig, err := enc.DecodeString(parts[2])
	if err != nil {
		return nil, err
	}
	if !hmac.Equal(gotSig, wantSig) {
		return nil, errors.New("invalid signature")
	}
	var header simpleJWTHeader
	if raw, err := enc.DecodeString(parts[0]); err != nil {
		return nil, err
	} else if err := json.Unmarshal(raw, &header); err != nil {
		return nil, err
	}
	if header.Alg != "HS256" || header.Typ != "JWT" {
		return nil, errors.New("unsupported token header")
	}
	var claims Claims
	if raw, err := enc.DecodeString(parts[1]); err != nil {
		return nil, err
	} else if err := json.Unmarshal(raw, &claims); err != nil {
		return nil, err
	}
	if time.Now().UTC().Unix() > claims.Exp {
		return nil, errors.New("token expired")
	}
	return &claims, nil
}
