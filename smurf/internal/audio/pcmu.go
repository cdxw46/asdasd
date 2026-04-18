package audio

// ULawEncode encodes linear 16-bit PCM to G.711 μ-law (ITU-T G.711).
func ULawEncode(s int16) uint8 {
	const bias = 0x84
	const clip = 32635
	ps := int(s)
	sign := 0
	if ps < 0 {
		sign = 0x80
		ps = -ps
	}
	if ps > clip {
		ps = clip
	}
	ps += bias
	exponent := 7
	for expMask := 0x4000; (ps&expMask) == 0 && exponent > 0; expMask >>= 1 {
		exponent--
	}
	mantissa := (ps >> (exponent + 3)) & 0x0F
	ulaw := ^(uint8(sign) | uint8(exponent<<4) | uint8(mantissa))
	return ulaw
}
