package main

import (
	"context"
	"log"
	"time"

	"github.com/smurf/pbx/internal/trunk"
)

func (s *Server) startTrunkRegistrationLoop() {
	go func() {
		t := time.NewTicker(25 * time.Minute)
		defer t.Stop()
		for range t.C {
			s.registerAllTrunks()
		}
	}()
	// initial after short delay
	time.AfterFunc(5*time.Second, s.registerAllTrunks)
}

func (s *Server) registerAllTrunks() {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	list, err := s.pool.ListEnabledTrunks(ctx)
	if err != nil {
		log.Printf("trunk list: %v", err)
		return
	}
	for i := range list {
		t := &list[i]
		if t.AuthUsername == "" || t.AuthPassword == "" {
			continue
		}
		if err := trunk.Register(ctx, t, s.realm, s.publicIP, s.sipPort); err != nil {
			log.Printf("trunk register %s: %v", t.Name, err)
		} else {
			log.Printf("trunk register ok: %s", t.Name)
		}
	}
}
