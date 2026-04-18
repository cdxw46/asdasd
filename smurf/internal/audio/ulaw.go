package audio

// ULawDecode decodes one G.711 μ-law byte to linear 16-bit PCM sample.
func ULawDecode(u uint8) int16 {
	u = ^u
	sign := (u & 0x80) != 0
	exponent := int((u >> 4) & 0x07)
	mantissa := int(u & 0x0F)
	sample := ((mantissa << 3) + 0x84) << exponent
	sample -= 0x84
	if sign {
		return int16(-sample)
	}
	return int16(sample)
}
