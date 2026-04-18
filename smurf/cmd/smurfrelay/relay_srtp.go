package main

import (
	"encoding/base64"
	"fmt"

	"github.com/pion/srtp/v3"
)

func decodeMasterKeySalt(keyB64, saltB64 string) (key, salt []byte, err error) {
	key, err = base64.StdEncoding.DecodeString(keyB64)
	if err != nil {
		return nil, nil, err
	}
	salt, err = base64.StdEncoding.DecodeString(saltB64)
	if err != nil {
		return nil, nil, err
	}
	if len(key) != 16 {
		return nil, nil, fmt.Errorf("srtp master key: want 16 bytes, got %d", len(key))
	}
	if len(salt) != 14 {
		return nil, nil, fmt.Errorf("srtp master salt: want 14 bytes, got %d", len(salt))
	}
	return key, salt, nil
}

func newSRTPDecryptContext(keyB64, saltB64 string) (*srtp.Context, error) {
	k, s, err := decodeMasterKeySalt(keyB64, saltB64)
	if err != nil {
		return nil, err
	}
	return srtp.CreateContext(k, s, srtp.ProtectionProfileAes128CmHmacSha1_80,
		srtp.SRTPReplayProtection(1024),
		srtp.SRTCPReplayProtection(32),
	)
}

func newSRTPEncryptContext(keyB64, saltB64 string) (*srtp.Context, error) {
	k, s, err := decodeMasterKeySalt(keyB64, saltB64)
	if err != nil {
		return nil, err
	}
	return srtp.CreateContext(k, s, srtp.ProtectionProfileAes128CmHmacSha1_80,
		srtp.SRTPReplayProtection(1024),
		srtp.SRTCPReplayProtection(32),
	)
}

func decryptRTPPacket(ctx *srtp.Context, ciphertext []byte) ([]byte, error) {
	if ctx == nil {
		return ciphertext, nil
	}
	out := make([]byte, 0, len(ciphertext)+128)
	return ctx.DecryptRTP(out, ciphertext, nil)
}

func encryptRTPPacket(ctx *srtp.Context, plaintext []byte) ([]byte, error) {
	if ctx == nil {
		return plaintext, nil
	}
	out := make([]byte, 0, len(plaintext)+128)
	return ctx.EncryptRTP(out, plaintext, nil)
}
