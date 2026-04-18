package sdp

import (
	"regexp"
	"strconv"
	"strings"
)

var reConn = regexp.MustCompile(`(?m)^c=IN IP4 \S+`)
var reAudio = regexp.MustCompile(`(?m)^(m=audio )(\d+)( .*)$`)
var reRTCP = regexp.MustCompile(`(?m)^(a=rtcp:)(\d+)(.*)$`)

// PatchMediaEndpoint rewrites connection address, audio RTP port, and rtcp attribute line if present.
func PatchMediaEndpoint(sdp, ip string, rtpPort int) string {
	out := sdp
	if reConn.MatchString(out) {
		out = reConn.ReplaceAllString(out, "c=IN IP4 "+ip)
	} else {
		// insert after first m=audio line's preceding c= missing: best-effort prepend session level
		if idx := strings.Index(out, "m=audio"); idx >= 0 {
			out = out[:idx] + "c=IN IP4 " + ip + "\r\n" + out[idx:]
		}
	}
	out = reAudio.ReplaceAllStringFunc(out, func(line string) string {
		m := reAudio.FindStringSubmatch(line)
		if len(m) != 4 {
			return line
		}
		return m[1] + strconv.Itoa(rtpPort) + m[3]
	})
	rtcp := rtpPort + 1
	out = reRTCP.ReplaceAllStringFunc(out, func(line string) string {
		m := reRTCP.FindStringSubmatch(line)
		if len(m) != 4 {
			return line
		}
		return m[1] + strconv.Itoa(rtcp) + m[3]
	})
	return out
}
