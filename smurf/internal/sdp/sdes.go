package sdp

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"strings"
)

// AppendSDES adds an SDES crypto line to the first m=audio section (RFC 3711 inline key).
func AppendSDES(sdp string, tag int, keyMaterialBase64 string) string {
	idx := strings.Index(sdp, "m=audio")
	if idx < 0 {
		return sdp
	}
	rest := sdp[idx:]
	end := strings.Index(rest, "\nm=")
	var insertAt int
	if end < 0 {
		insertAt = len(sdp)
	} else {
		insertAt = idx + end
	}
	line := fmt.Sprintf("a=crypto:%d AES_CM_128_HMAC_SHA1_80 inline:%s\r\n", tag, keyMaterialBase64)
	return sdp[:insertAt] + line + sdp[insertAt:]
}

// GenerateSDESKeyMaterial returns 30 random bytes as base64 (RFC 3711 AES_CM_128_HMAC_SHA1_80).
func GenerateSDESKeyMaterial() (string, error) {
	b := make([]byte, 30)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(b), nil
}
