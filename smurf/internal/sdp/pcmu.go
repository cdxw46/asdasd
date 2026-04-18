package sdp

import "strconv"

// BuildPCMU offers a single audio line with G.711 μ-law (PT 0) for classic SIP endpoints.
func BuildPCMU(localIP string, rtpPort int) string {
	return "v=0\r\n" +
		"o=smurf 0 0 IN IP4 " + localIP + "\r\n" +
		"s=smurf\r\n" +
		"c=IN IP4 " + localIP + "\r\n" +
		"t=0 0\r\n" +
		"m=audio " + strconv.Itoa(rtpPort) + " RTP/AVP 0\r\n" +
		"a=rtpmap:0 PCMU/8000\r\n"
}
