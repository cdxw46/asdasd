package main

import (
	"context"
	"strings"
	"time"

	"github.com/smurf/pbx/internal/schedule"
)

func trimTimeForParse(s string) string {
	s = strings.TrimSpace(s)
	if len(s) == 8 && s[2] == ':' && s[5] == ':' {
		return s[:5]
	}
	return s
}

func (s *Server) resolveInviteTarget(ctx context.Context, to string) string {
	oh, err := s.pool.GetOfficeHours(ctx, to)
	if err != nil {
		return to
	}
	st := trimTimeForParse(oh.TimeStart)
	en := trimTimeForParse(oh.TimeEnd)
	if schedule.InOfficeUTC(oh.WeekdayMask, st, en, time.Now().UTC()) {
		return to
	}
	return oh.OutsideTarget
}
