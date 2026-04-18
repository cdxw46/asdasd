package sip

import (
	"bytes"
	"fmt"
	"strconv"
	"strings"
)

type Message struct {
	StartLine
	Headers    HeaderMap
	Body       []byte
	headerKeys []string // original casing / first occurrence order
}

type StartLine struct {
	IsRequest  bool
	Method     string
	RequestURI string
	Proto      string
	StatusCode int
	Reason     string
}

type HeaderMap map[string][]string

func (h HeaderMap) Get(name string) string {
	v := h[strings.ToLower(name)]
	if len(v) == 0 {
		return ""
	}
	return v[len(v)-1]
}

func (h HeaderMap) All(name string) []string {
	return h[strings.ToLower(name)]
}

func (h HeaderMap) Set(name, value string) {
	k := strings.ToLower(name)
	h[k] = []string{value}
}

func (m *Message) String() string {
	var b strings.Builder
	if m.IsRequest {
		b.WriteString(fmt.Sprintf("%s %s %s\r\n", m.Method, m.RequestURI, m.Proto))
	} else {
		b.WriteString(fmt.Sprintf("%s %d %s\r\n", m.Proto, m.StatusCode, m.Reason))
	}
	if len(m.headerKeys) > 0 {
		for _, orig := range m.headerKeys {
			k := strings.ToLower(orig)
			for _, v := range m.Headers[k] {
				b.WriteString(fmt.Sprintf("%s: %s\r\n", orig, v))
			}
		}
	} else {
		for k, vals := range m.Headers {
			for _, v := range vals {
				b.WriteString(fmt.Sprintf("%s: %s\r\n", titleHeader(k), v))
			}
		}
	}
	b.WriteString("\r\n")
	if len(m.Body) > 0 {
		b.Write(m.Body)
	}
	return b.String()
}

// AddHeader appends a header preserving first-seen key order.
func (m *Message) AddHeader(name, value string) {
	if m.Headers == nil {
		m.Headers = HeaderMap{}
	}
	k := strings.ToLower(name)
	if _, ok := m.Headers[k]; !ok {
		m.headerKeys = append(m.headerKeys, name)
	}
	m.Headers[k] = append(m.Headers[k], value)
}

func titleHeader(lower string) string {
	parts := strings.Split(lower, "-")
	for i := range parts {
		if len(parts[i]) == 0 {
			continue
		}
		parts[i] = strings.ToUpper(parts[i][:1]) + parts[i][1:]
	}
	return strings.Join(parts, "-")
}

func ParseMessage(data []byte) (*Message, error) {
	idx := bytes.Index(data, []byte("\r\n\r\n"))
	if idx < 0 {
		return nil, fmt.Errorf("incomplete sip message")
	}
	head := string(data[:idx])
	body := data[idx+4:]
	lines := strings.Split(head, "\r\n")
	if len(lines) == 0 || lines[0] == "" {
		return nil, fmt.Errorf("empty start line")
	}
	m := &Message{Headers: HeaderMap{}}
	sl := lines[0]
	if strings.Contains(sl, "SIP/2.0") && !strings.HasPrefix(sl, "SIP/2.0") {
		// request
		parts := strings.Fields(sl)
		if len(parts) < 3 {
			return nil, fmt.Errorf("bad request line")
		}
		m.IsRequest = true
		m.Method = parts[0]
		m.RequestURI = parts[1]
		m.Proto = parts[2]
	} else {
		// response
		parts := strings.Fields(sl)
		if len(parts) < 3 {
			return nil, fmt.Errorf("bad status line")
		}
		m.IsRequest = false
		m.Proto = parts[0]
		sc, err := strconv.Atoi(parts[1])
		if err != nil {
			return nil, err
		}
		m.StatusCode = sc
		if len(parts) > 2 {
			m.Reason = strings.TrimSpace(strings.Join(parts[2:], " "))
		}
	}
	for _, ln := range lines[1:] {
		if ln == "" {
			continue
		}
		colon := strings.IndexByte(ln, ':')
		if colon <= 0 {
			continue
		}
		name := strings.TrimSpace(ln[:colon])
		val := strings.TrimSpace(ln[colon+1:])
		k := strings.ToLower(name)
		if _, ok := m.Headers[k]; !ok {
			m.headerKeys = append(m.headerKeys, name)
		}
		m.Headers[k] = append(m.Headers[k], val)
	}
	cl := m.Headers.Get("Content-Length")
	if cl != "" {
		n, err := strconv.Atoi(strings.TrimSpace(cl))
		if err != nil {
			return nil, err
		}
		if n > len(body) {
			return nil, fmt.Errorf("short body: want %d have %d", n, len(body))
		}
		m.Body = body[:n]
	} else {
		m.Body = body
	}
	return m, nil
}
