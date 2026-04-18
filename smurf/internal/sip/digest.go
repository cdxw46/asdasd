package sip

import (
	"crypto/md5"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"strings"
)

type AuthParams map[string]string

func ParseDigestHeader(h string) AuthParams {
	h = strings.TrimSpace(h)
	if idx := strings.IndexByte(strings.ToLower(h), ' '); idx >= 0 {
		h = strings.TrimSpace(h[idx+1:])
	}
	out := AuthParams{}
	for _, part := range strings.Split(h, ",") {
		part = strings.TrimSpace(part)
		kv := strings.SplitN(part, "=", 2)
		if len(kv) != 2 {
			continue
		}
		k := strings.TrimSpace(strings.ToLower(kv[0]))
		v := strings.TrimSpace(kv[1])
		if len(v) >= 2 && v[0] == '"' && v[len(v)-1] == '"' {
			v = v[1 : len(v)-1]
		}
		out[k] = v
	}
	return out
}

func md5Hex(s string) string {
	sum := md5.Sum([]byte(s))
	return hex.EncodeToString(sum[:])
}

func sha256HexLower(s string) string {
	sum := sha256.Sum256([]byte(s))
	return strings.ToLower(hex.EncodeToString(sum[:]))
}

func VerifyDigestResponse(method, digestURI, body string, challenge, creds AuthParams, username, password string) bool {
	realm := challenge["realm"]
	nonce := challenge["nonce"]
	qop := strings.ToLower(challenge["qop"])
	algo := strings.ToLower(challenge["algorithm"])
	if algo == "" {
		algo = "md5"
	}
	if strings.HasPrefix(algo, "md5") {
		algo = "md5"
	}
	if strings.Contains(algo, "sha-256") {
		algo = "sha-256"
	}

	if uri := creds["uri"]; uri != "" {
		if u, err := url.QueryUnescape(uri); err == nil {
			digestURI = u
		} else {
			digestURI = uri
		}
	}

	ha1 := digestHA1(algo, username, realm, password)
	ha2 := digestHA2(algo, qop, method, digestURI, body)

	resp := creds["response"]

	if qop == "auth" || qop == "auth-int" {
		nc := creds["nc"]
		cnonce := creds["cnonce"]
		qp := creds["qop"]
		if qp == "" {
			qp = qop
		}
		var expected string
		if algo == "sha-256" {
			expected = sha256HexLower(fmt.Sprintf("%s:%s:%s:%s:%s:%s", ha1, nonce, nc, cnonce, qp, ha2))
		} else {
			expected = md5Hex(fmt.Sprintf("%s:%s:%s:%s:%s:%s", ha1, nonce, nc, cnonce, qp, ha2))
		}
		return strings.EqualFold(expected, resp)
	}
	if algo == "sha-256" {
		return strings.EqualFold(sha256HexLower(fmt.Sprintf("%s:%s:%s", ha1, nonce, ha2)), resp)
	}
	return strings.EqualFold(md5Hex(fmt.Sprintf("%s:%s:%s", ha1, nonce, ha2)), resp)
}

func digestHA1(algo, username, realm, password string) string {
	a1 := fmt.Sprintf("%s:%s:%s", username, realm, password)
	if algo == "sha-256" {
		return sha256HexLower(a1)
	}
	return md5Hex(a1)
}

func digestHA2(algo, qop, method, digestURI, body string) string {
	if qop == "auth-int" {
		bodyHash := ""
		if algo == "sha-256" {
			bodyHash = sha256HexLower(body)
		} else {
			bodyHash = md5Hex(body)
		}
		s := fmt.Sprintf("%s:%s:%s", method, digestURI, bodyHash)
		if algo == "sha-256" {
			return sha256HexLower(s)
		}
		return md5Hex(s)
	}
	s := fmt.Sprintf("%s:%s", method, digestURI)
	if algo == "sha-256" {
		return sha256HexLower(s)
	}
	return md5Hex(s)
}

func RandomNonce() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// BuildAuthorizationHeader returns a full Authorization: Digest ... value for a UAC.
func BuildAuthorizationHeader(username, password, method, digestURI, body string, ch AuthParams) string {
	realm := ch["realm"]
	nonce := ch["nonce"]
	qop := strings.ToLower(ch["qop"])
	if qop == "" {
		qop = "auth"
	}
	algo := strings.ToLower(ch["algorithm"])
	if algo == "" {
		algo = "md5"
	}
	if strings.HasPrefix(algo, "md5") {
		algo = "md5"
	}
	if strings.Contains(algo, "sha-256") {
		algo = "sha-256"
	}
	ha1 := digestHA1(algo, username, realm, password)
	ha2 := digestHA2(algo, qop, method, digestURI, body)
	nc := "00000001"
	cnonce := RandomNonce()[:16]
	var response string
	if qop == "auth" || qop == "auth-int" {
		if algo == "sha-256" {
			response = sha256HexLower(fmt.Sprintf("%s:%s:%s:%s:%s:%s", ha1, nonce, nc, cnonce, qop, ha2))
		} else {
			response = md5Hex(fmt.Sprintf("%s:%s:%s:%s:%s:%s", ha1, nonce, nc, cnonce, qop, ha2))
		}
	} else {
		if algo == "sha-256" {
			response = sha256HexLower(fmt.Sprintf("%s:%s:%s", ha1, nonce, ha2))
		} else {
			response = md5Hex(fmt.Sprintf("%s:%s:%s", ha1, nonce, ha2))
		}
	}
	opaque := ch["opaque"]
	algoOut := ch["algorithm"]
	if algoOut == "" {
		if algo == "sha-256" {
			algoOut = "SHA-256"
		} else {
			algoOut = "MD5"
		}
	}
	// quote uri per RFC8760
	escURI := digestURI
	s := fmt.Sprintf(`Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", algorithm=%s`,
		username, realm, nonce, escURI, response, algoOut)
	if qop == "auth" || qop == "auth-int" {
		s += fmt.Sprintf(`, qop=%s, nc=%s, cnonce="%s"`, qop, nc, cnonce)
	}
	if opaque != "" {
		s += fmt.Sprintf(`, opaque="%s"`, opaque)
	}
	return s
}
