package main

import (
	"context"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"smurf/internal/config"
	"smurf/internal/db"
	"smurf/internal/httpapi"
	"smurf/internal/pbx"
	"smurf/internal/rtp"
	"smurf/internal/sip"
	"smurf/internal/util"
	"smurf/internal/webrtcgw"
)

func main() {
	cfgPath := flag.String("config", "/etc/smurf/smurf.json", "SMURF config path")
	flag.Parse()

	cfg, err := config.Ensure(*cfgPath)
	if err != nil {
		panic(err)
	}
	if err := cfg.Validate(); err != nil {
		panic(err)
	}

	logger := util.NewLogger(cfg.LogLevel)
	store, err := db.Open(cfg)
	if err != nil {
		panic(err)
	}
	defer store.Close()

	rtpManager := rtp.NewManager(cfg)
	pbxEngine := pbx.New(store, rtpManager, logger)
	sipServer := sip.NewServer(cfg, store, logger, pbxEngine)
	webrtcGateway := webrtcgw.New(cfg, pbxEngine, logger)
	httpServer := httpapi.New(cfg, store, pbxEngine, webrtcGateway, logger)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	errCh := make(chan error, 2)
	go func() {
		if err := sipServer.Start(ctx); err != nil {
			errCh <- err
		}
	}()
	go func() {
		if err := httpServer.Start(); err != nil && err != http.ErrServerClosed {
			errCh <- err
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	select {
	case sig := <-sigCh:
		logger.Info("shutdown requested", "signal", sig.String())
	case err := <-errCh:
		logger.Error("daemon exiting due to error", "error", err)
	}

	cancel()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	_ = httpServer.Shutdown(shutdownCtx)
	_ = rtpManager.Shutdown(shutdownCtx)
}
