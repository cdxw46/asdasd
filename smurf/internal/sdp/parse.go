package sdp

import (
	"regexp"
	"strconv"
	"strings"
)

var reConnIP = regexp.MustCompile(`(?m)^c=IN IP4 (\S+)`)
var reAudioPort = regexp.MustCompile(`(?m)^m=audio (\d+)`)

func ParseConnectionIP(sdp string) string {
	m := reConnIP.FindStringSubmatch(sdp)
	if len(m) < 2 {
		return ""
	}
	return m[1]
}

func ParseAudioPort(sdp string) int {
	m := reAudioPort.FindStringSubmatch(sdp)
	if len(m) < 2 {
		return 0
	}
	n, _ := strconv.Atoi(m[1])
	return n
}

func ParseRemoteMediaAddr(sdp string) (ip string, port int) {
	return ParseConnectionIP(sdp), ParseAudioPort(sdp)
}

func HasCrypto(sdp string) bool {
	return strings.Contains(strings.ToLower(sdp), "a=crypto:")
}
