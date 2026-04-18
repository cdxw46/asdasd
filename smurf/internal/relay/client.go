package relay

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"sync"
)

type Client struct {
	addr string
	mu   sync.Mutex
}

func New(addr string) *Client {
	return &Client{addr: addr}
}

type openReq struct {
	Cmd     string `json:"cmd"`
	ID      string `json:"id"`
	TapAddr string `json:"tap_addr,omitempty"` // "host:port" receive copies of packets from RTP leg A (caller)
}

type openResp struct {
	RTPA  int    `json:"rtp_a"`
	RTPB  int    `json:"rtp_b"`
	Error string `json:"error,omitempty"`
}

type closeReq struct {
	Cmd string `json:"cmd"`
	ID  string `json:"id"`
}

// SRTPKeys holds base64-encoded 16-byte master key and 14-byte master salt for smurfrelay (RFC 3711).
type SRTPKeys struct {
	DecryptKey, DecryptSalt string
	EncryptKey, EncryptSalt string
}

type srtpCmd struct {
	Cmd          string `json:"cmd"`
	ID           string `json:"id"`
	ADecryptKey  string `json:"a_decrypt_key,omitempty"`
	ADecryptSalt string `json:"a_decrypt_salt,omitempty"`
	AEncryptKey  string `json:"a_encrypt_key,omitempty"`
	AEncryptSalt string `json:"a_encrypt_salt,omitempty"`
	BDecryptKey  string `json:"b_decrypt_key,omitempty"`
	BDecryptSalt string `json:"b_decrypt_salt,omitempty"`
	BEncryptKey  string `json:"b_encrypt_key,omitempty"`
	BEncryptSalt string `json:"b_encrypt_salt,omitempty"`
}

func (c *Client) OpenSession(id string) (legA int, legB int, err error) {
	return c.OpenSessionWithTap(id, "")
}

func (c *Client) OpenSessionWithTap(id, tapAddr string) (legA int, legB int, err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	conn, err := net.Dial("tcp", c.addr)
	if err != nil {
		return 0, 0, err
	}
	defer conn.Close()
	enc := json.NewEncoder(conn)
	dec := json.NewDecoder(bufio.NewReader(conn))
	if err := enc.Encode(openReq{Cmd: "open", ID: id, TapAddr: tapAddr}); err != nil {
		return 0, 0, err
	}
	var resp openResp
	if err := dec.Decode(&resp); err != nil {
		return 0, 0, err
	}
	if resp.Error != "" {
		return 0, 0, fmt.Errorf("%s", resp.Error)
	}
	if resp.RTPA == 0 || resp.RTPB == 0 {
		return 0, 0, fmt.Errorf("invalid relay response")
	}
	return resp.RTPA, resp.RTPB, nil
}

func (c *Client) CloseSession(id string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	conn, err := net.Dial("tcp", c.addr)
	if err != nil {
		return
	}
	defer conn.Close()
	_ = json.NewEncoder(conn).Encode(closeReq{Cmd: "close", ID: id})
}

// ConfigureSRTP programs AES_CM_128_HMAC_SHA1_80 SDES contexts on an existing relay session (leg A = caller, leg B = callee).
func (c *Client) ConfigureSRTP(id string, legA, legB SRTPKeys) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	conn, err := net.Dial("tcp", c.addr)
	if err != nil {
		return err
	}
	defer conn.Close()
	enc := json.NewEncoder(conn)
	dec := json.NewDecoder(bufio.NewReader(conn))
	cmd := srtpCmd{Cmd: "srtp", ID: id}
	if legA.DecryptKey != "" {
		cmd.ADecryptKey, cmd.ADecryptSalt = legA.DecryptKey, legA.DecryptSalt
		cmd.AEncryptKey, cmd.AEncryptSalt = legA.EncryptKey, legA.EncryptSalt
	}
	if legB.DecryptKey != "" {
		cmd.BDecryptKey, cmd.BDecryptSalt = legB.DecryptKey, legB.DecryptSalt
		cmd.BEncryptKey, cmd.BEncryptSalt = legB.EncryptKey, legB.EncryptSalt
	}
	if err := enc.Encode(cmd); err != nil {
		return err
	}
	var resp map[string]string
	if err := dec.Decode(&resp); err != nil {
		return err
	}
	if e := resp["error"]; e != "" {
		return fmt.Errorf("%s", e)
	}
	return nil
}
