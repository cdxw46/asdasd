package sdp

import (
	"encoding/base64"
	"fmt"
	"regexp"
	"strings"
)

var reCryptoLine = regexp.MustCompile(`(?m)^a=crypto:(\d+)\s+(\S+)\s+inline:(\S+)\s*$`)

// StripCryptoLines removes all a=crypto: lines from SDP.
func StripCryptoLines(sdp string) string {
	lines := strings.Split(sdp, "\r\n")
	out := make([]string, 0, len(lines))
	for _, ln := range lines {
		if strings.HasPrefix(strings.ToLower(ln), "a=crypto:") {
			continue
		}
		out = append(out, ln)
	}
	return strings.Join(out, "\r\n")
}

// ParseFirstSDESCrypto returns tag, suite, and base64 key material from the first a=crypto line.
func ParseFirstSDESCrypto(sdp string) (tag int, suite, keyB64 string, ok bool) {
	m := reCryptoLine.FindStringSubmatch(sdp)
	if len(m) < 4 {
		return 0, "", "", false
	}
	var t int
	if _, err := fmt.Sscanf(m[1], "%d", &t); err != nil {
		return 0, "", "", false
	}
	return t, m[2], m[3], true
}

// DecodeSDESInline decodes RFC 3711 "inline:" base64 key material (16-byte key + 14-byte salt for AES_CM_128_HMAC_SHA1_80).
func DecodeSDESInline(keyB64 string) (masterKey, masterSalt []byte, err error) {
	raw, err := base64.StdEncoding.DecodeString(keyB64)
	if err != nil {
		return nil, nil, err
	}
	if len(raw) != 30 {
		return nil, nil, fmt.Errorf("sdes inline: want 30 bytes, got %d", len(raw))
	}
	return raw[:16], raw[16:], nil
}

// SDESInlineToKeySaltB64 splits RFC 3711 30-byte inline material into base64 key and salt (16+14 bytes).
func SDESInlineToKeySaltB64(inlineB64 string) (keyB64, saltB64 string, err error) {
	k, s, err := DecodeSDESInline(inlineB64)
	if err != nil {
		return "", "", err
	}
	return base64.StdEncoding.EncodeToString(k), base64.StdEncoding.EncodeToString(s), nil
}
