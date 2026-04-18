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
	Cmd string `json:"cmd"`
	ID  string `json:"id"`
}

type openResp struct {
	RTPA int    `json:"rtp_a"`
	RTPB int    `json:"rtp_b"`
	Err  string `json:"error,omitempty"`
}

type closeReq struct {
	Cmd string `json:"cmd"`
	ID  string `json:"id"`
}

// OpenSession allocates a symmetric RTP bridge. Returns UDP ports for leg A and leg B.
func (c *Client) OpenSession(id string) (legA int, legB int, err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	conn, err := net.Dial("tcp", c.addr)
	if err != nil {
		return 0, 0, err
	}
	defer conn.Close()
	enc := json.NewEncoder(conn)
	dec := json.NewDecoder(bufio.NewReader(conn))
	if err := enc.Encode(openReq{Cmd: "open", ID: id}); err != nil {
		return 0, 0, err
	}
	var resp openResp
	if err := dec.Decode(&resp); err != nil {
		return 0, 0, err
	}
	if resp.Err != "" {
		return 0, 0, fmt.Errorf("%s", resp.Err)
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
