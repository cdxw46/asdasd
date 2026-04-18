package main

import (
	"bufio"
	"crypto/rand"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"flag"
	"fmt"
	"strings"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:15001", "HTTPS address exposing /ws")
	flag.Parse()

	conn, err := tls.Dial("tcp", *addr, &tls.Config{
		InsecureSkipVerify: true,
		ServerName:         "127.0.0.1",
		MinVersion:         tls.VersionTLS12,
	})
	if err != nil {
		panic(err)
	}
	defer conn.Close()

	keyRaw := make([]byte, 16)
	if _, err := rand.Read(keyRaw); err != nil {
		panic(err)
	}
	key := base64.StdEncoding.EncodeToString(keyRaw)
	req := strings.Join([]string{
		"GET /ws HTTP/1.1",
		"Host: " + *addr,
		"Upgrade: websocket",
		"Connection: Upgrade",
		"Sec-WebSocket-Key: " + key,
		"Sec-WebSocket-Version: 13",
		"",
		"",
	}, "\r\n")
	if _, err := conn.Write([]byte(req)); err != nil {
		panic(err)
	}

	reader := bufio.NewReader(conn)
	status, err := reader.ReadString('\n')
	if err != nil {
		panic(err)
	}
	headers := map[string]string{}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			panic(err)
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		if parts := strings.SplitN(line, ":", 2); len(parts) == 2 {
			headers[strings.ToLower(strings.TrimSpace(parts[0]))] = strings.TrimSpace(parts[1])
		}
	}

	wantAccept := computeAccept(key)
	fmt.Println(strings.Contains(status, "101"))
	fmt.Println(strings.EqualFold(headers["upgrade"], "websocket"))
	fmt.Println(strings.Contains(strings.ToLower(headers["connection"]), "upgrade"))
	fmt.Println(headers["sec-websocket-accept"] == wantAccept)
}

func computeAccept(key string) string {
	sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	return base64.StdEncoding.EncodeToString(sum[:])
}
