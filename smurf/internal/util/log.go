package util

import (
	"fmt"
	"log"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	levelDebug = iota
	levelInfo
	levelWarn
	levelError
)

type Logger struct {
	mu    sync.Mutex
	level int
	base  *log.Logger
}

func NewLogger(level string) *Logger {
	return &Logger{
		level: parseLevel(level),
		base:  log.New(os.Stdout, "", 0),
	}
}

func parseLevel(level string) int {
	switch strings.ToUpper(strings.TrimSpace(level)) {
	case "DEBUG":
		return levelDebug
	case "WARN", "WARNING":
		return levelWarn
	case "ERROR":
		return levelError
	default:
		return levelInfo
	}
}

func (l *Logger) Debug(args ...any) { l.log(levelDebug, "DEBUG", args...) }
func (l *Logger) Info(args ...any)  { l.log(levelInfo, "INFO", args...) }
func (l *Logger) Warn(args ...any)  { l.log(levelWarn, "WARN", args...) }
func (l *Logger) Error(args ...any) { l.log(levelError, "ERROR", args...) }

func (l *Logger) Debugf(format string, args ...any) { l.logf(levelDebug, "DEBUG", format, args...) }
func (l *Logger) Infof(format string, args ...any)  { l.logf(levelInfo, "INFO", format, args...) }
func (l *Logger) Warnf(format string, args ...any)  { l.logf(levelWarn, "WARN", format, args...) }
func (l *Logger) Errorf(format string, args ...any) { l.logf(levelError, "ERROR", format, args...) }

func (l *Logger) log(level int, tag string, args ...any) {
	if l == nil || level < l.level {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.base.Printf("%s [%s] %s", time.Now().UTC().Format(time.RFC3339), tag, renderArgs(args...))
}

func (l *Logger) logf(level int, tag, format string, args ...any) {
	if l == nil || level < l.level {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.base.Printf("%s [%s] %s", time.Now().UTC().Format(time.RFC3339), tag, fmt.Sprintf(format, args...))
}

func renderArgs(args ...any) string {
	if len(args) == 0 {
		return ""
	}
	if format, ok := args[0].(string); ok {
		if strings.Contains(format, "%") && len(args) > 1 {
			return fmt.Sprintf(format, args[1:]...)
		}
		if len(args) == 1 {
			return format
		}
		if len(args[1:])%2 == 0 {
			fields := make([]string, 0, 1+len(args[1:])/2)
			fields = append(fields, format)
			for i := 1; i < len(args); i += 2 {
				fields = append(fields, fmt.Sprintf("%v=%v", args[i], args[i+1]))
			}
			sort.Strings(fields[1:])
			return strings.Join(fields, " ")
		}
	}
	parts := make([]string, len(args))
	for i, arg := range args {
		parts[i] = fmt.Sprint(arg)
	}
	return strings.Join(parts, " ")
}

func RemoteIP(addr net.Addr) string {
	if addr == nil {
		return ""
	}
	host, _, err := net.SplitHostPort(addr.String())
	if err == nil {
		return host
	}
	return addr.String()
}

func HostPort(host string, port int) string {
	return net.JoinHostPort(strings.Trim(host, "[]"), fmt.Sprintf("%d", port))
}
