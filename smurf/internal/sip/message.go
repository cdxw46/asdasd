package sip

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/textproto"
	"strconv"
	"strings"
)

type Message struct {
	IsRequest   bool
	Method      string
	RequestURI  string
	StatusCode  int
	Reason      string
	Version     string
	Headers     textproto.MIMEHeader
	Body        string
	RawStartLine string
}

func ParseMessage(data []byte) (*Message, error) {
	reader := bufio.NewReader(bytes.NewReader(data))
	startLine, err := reader.ReadString('\n')
	if err != nil {
		return nil, err
	}
	startLine = strings.TrimRight(startLine, "\r\n")
	msg := &Message{
		Headers:      make(textproto.MIMEHeader),
		RawStartLine: startLine,
	}
	if strings.HasPrefix(startLine, "SIP/2.0 ") {
		msg.IsRequest = false
		msg.Version = "SIP/2.0"
		parts := strings.SplitN(startLine, " ", 3)
		if len(parts) < 3 {
			return nil, errors.New("invalid status line")
		}
		code, err := strconv.Atoi(parts[1])
		if err != nil {
			return nil, err
		}
		msg.StatusCode = code
		msg.Reason = parts[2]
	} else {
		msg.IsRequest = true
		parts := strings.SplitN(startLine, " ", 3)
		if len(parts) < 3 {
			return nil, errors.New("invalid request line")
		}
		msg.Method = strings.ToUpper(strings.TrimSpace(parts[0]))
		msg.RequestURI = strings.TrimSpace(parts[1])
		msg.Version = strings.TrimSpace(parts[2])
	}

	tp := textproto.NewReader(reader)
	headers, err := tp.ReadMIMEHeader()
	if err != nil {
		return nil, err
	}
	msg.Headers = headers

	length := 0
	if v := msg.GetHeader("Content-Length"); v != "" {
		length, _ = strconv.Atoi(strings.TrimSpace(v))
	}
	if length > 0 {
		body := make([]byte, length)
		if _, err := reader.Read(body); err != nil {
			return nil, err
		}
		msg.Body = string(body)
	}
	return msg, nil
}

func Parse(data []byte) (*Message, error) {
	return ParseMessage(data)
}

func ReadMessage(reader *bufio.Reader) (*Message, error) {
	var raw bytes.Buffer
	startLine, err := reader.ReadString('\n')
	if err != nil {
		return nil, err
	}
	raw.WriteString(startLine)

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return nil, err
		}
		raw.WriteString(line)
		if line == "\r\n" || line == "\n" {
			break
		}
	}

	tmp, err := ParseMessage(raw.Bytes())
	if err != nil && !errors.Is(err, io.EOF) {
		// Continue to body parsing below for messages where only body is missing.
	}

	contentLen := 0
	if tmp != nil {
		if v := tmp.GetHeader("Content-Length"); v != "" {
			contentLen, _ = strconv.Atoi(strings.TrimSpace(v))
		}
	}
	if contentLen > 0 {
		body := make([]byte, contentLen)
		if _, err := io.ReadFull(reader, body); err != nil {
			return nil, err
		}
		raw.Write(body)
	}
	return ParseMessage(raw.Bytes())
}

func (m *Message) GetHeader(name string) string {
	return textproto.MIMEHeader(m.Headers).Get(name)
}

func (m *Message) Values(name string) []string {
	return textproto.MIMEHeader(m.Headers).Values(name)
}

func (m *Message) Clone() *Message {
	cp := &Message{
		IsRequest:    m.IsRequest,
		Method:       m.Method,
		RequestURI:   m.RequestURI,
		StatusCode:   m.StatusCode,
		Reason:       m.Reason,
		Version:      m.Version,
		Headers:      make(textproto.MIMEHeader),
		Body:         m.Body,
		RawStartLine: m.RawStartLine,
	}
	for k, vals := range m.Headers {
		cp.Headers[k] = append([]string(nil), vals...)
	}
	return cp
}

func (m *Message) SetHeader(name, value string) {
	m.Headers[textproto.CanonicalMIMEHeaderKey(name)] = []string{value}
}

func (m *Message) AddHeader(name, value string) {
	key := textproto.CanonicalMIMEHeaderKey(name)
	m.Headers[key] = append(m.Headers[key], value)
}

func (m *Message) DelHeader(name string) {
	delete(m.Headers, textproto.CanonicalMIMEHeaderKey(name))
}

func (m *Message) String() string {
	var b strings.Builder
	if m.IsRequest {
		fmt.Fprintf(&b, "%s %s %s\r\n", m.Method, m.RequestURI, defaultVersion(m.Version))
	} else {
		fmt.Fprintf(&b, "%s %d %s\r\n", defaultVersion(m.Version), m.StatusCode, m.Reason)
	}
	for k, vals := range m.Headers {
		for _, v := range vals {
			fmt.Fprintf(&b, "%s: %s\r\n", k, v)
		}
	}
	body := m.Body
	if m.GetHeader("Content-Length") == "" {
		fmt.Fprintf(&b, "Content-Length: %d\r\n", len(body))
	}
	b.WriteString("\r\n")
	b.WriteString(body)
	return b.String()
}

func (m *Message) Bytes() []byte {
	return []byte(m.String())
}

func defaultVersion(v string) string {
	if strings.TrimSpace(v) == "" {
		return "SIP/2.0"
	}
	return v
}

func HeaderParam(headerValue, name string) string {
	for _, part := range strings.Split(headerValue, ";") {
		part = strings.TrimSpace(part)
		if strings.HasPrefix(strings.ToLower(part), strings.ToLower(name)+"=") {
			return strings.Trim(strings.SplitN(part, "=", 2)[1], "\"")
		}
	}
	return ""
}

func URIUser(value string) string {
	value = strings.TrimSpace(value)
	if i := strings.Index(value, "<"); i >= 0 {
		value = value[i+1:]
	}
	if j := strings.Index(value, ">"); j >= 0 {
		value = value[:j]
	}
	value = strings.TrimPrefix(value, "sip:")
	value = strings.TrimPrefix(value, "sips:")
	if i := strings.Index(value, "@"); i >= 0 {
		return value[:i]
	}
	if i := strings.IndexAny(value, ";:"); i >= 0 {
		return value[:i]
	}
	return value
}

func BuildResponse(req *Message, code int, reason string, headers map[string]string, body string) *Message {
	resp := &Message{
		IsRequest:  false,
		StatusCode: code,
		Reason:     reason,
		Version:    "SIP/2.0",
		Headers:    make(textproto.MIMEHeader),
		Body:       body,
	}
	copyHeader := func(name string) {
		if req == nil {
			return
		}
		if values := req.Values(name); len(values) > 0 {
			resp.Headers[textproto.CanonicalMIMEHeaderKey(name)] = append([]string(nil), values...)
		}
	}
	for _, name := range []string{"Via", "From", "To", "Call-ID", "CSeq", "Contact"} {
		copyHeader(name)
	}
	if req != nil && code >= 200 && !strings.Contains(strings.ToLower(resp.GetHeader("To")), ";tag=") {
		if to := resp.GetHeader("To"); to != "" {
			resp.SetHeader("To", to+";tag=smurf"+strconv.FormatInt(int64(len(to)+code), 10))
		}
	}
	for k, v := range headers {
		resp.SetHeader(k, v)
	}
	resp.SetHeader("Server", "SMURF")
	resp.SetHeader("Content-Length", strconv.Itoa(len(body)))
	return resp
}
