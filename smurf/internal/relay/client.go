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
