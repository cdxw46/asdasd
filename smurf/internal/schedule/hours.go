package schedule

import (
	"strings"
	"time"
)

// InOfficeUTC checks weekday bit mask (bit for time.Weekday: Sun=1<<0 ... Sat=1<<6)
// and time window in UTC using "15:04" or "15:04:05" strings.
func InOfficeUTC(mask int, startStr, endStr string, now time.Time) bool {
	bit := 1 << int(now.Weekday())
	if mask&bit == 0 {
		return false
	}
	st := parseClock(startStr)
	en := parseClock(endStr)
	if st < 0 || en < 0 {
		return true
	}
	cur := now.Hour()*3600 + now.Minute()*60 + now.Second()
	return cur >= st && cur < en
}

func parseClock(s string) int {
	s = strings.TrimSpace(s)
	if s == "" {
		return -1
	}
	layouts := []string{"15:04:05", "15:04"}
	for _, l := range layouts {
		t, err := time.Parse(l, s)
		if err == nil {
			return t.Hour()*3600 + t.Minute()*60 + t.Second()
		}
	}
	return -1
}
