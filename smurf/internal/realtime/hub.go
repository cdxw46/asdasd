package realtime

import (
	"encoding/json"
	"sync"
)

type Event struct {
	Type    string `json:"type"`
	Topic   string `json:"topic"`
	Payload any    `json:"payload"`
}

type Subscriber struct {
	ID     string
	Topics map[string]struct{}
	Ch     chan Event
}

type Hub struct {
	mu          sync.RWMutex
	subscribers map[string]*Subscriber
}

func NewHub() *Hub {
	return &Hub{
		subscribers: map[string]*Subscriber{},
	}
}

func (h *Hub) Subscribe(id string, topics ...string) *Subscriber {
	h.mu.Lock()
	defer h.mu.Unlock()
	sub := &Subscriber{
		ID:     id,
		Topics: map[string]struct{}{},
		Ch:     make(chan Event, 64),
	}
	for _, topic := range topics {
		if topic != "" {
			sub.Topics[topic] = struct{}{}
		}
	}
	h.subscribers[id] = sub
	return sub
}

func (h *Hub) Unsubscribe(id string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	sub := h.subscribers[id]
	delete(h.subscribers, id)
	if sub != nil {
		close(sub.Ch)
	}
}

func (h *Hub) SetTopics(id string, topics ...string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	sub := h.subscribers[id]
	if sub == nil {
		return
	}
	sub.Topics = map[string]struct{}{}
	for _, topic := range topics {
		if topic != "" {
			sub.Topics[topic] = struct{}{}
		}
	}
}

func (h *Hub) Publish(topic, eventType string, payload any) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	ev := Event{Type: eventType, Topic: topic, Payload: payload}
	for _, sub := range h.subscribers {
		if _, ok := sub.Topics[topic]; ok {
			select {
			case sub.Ch <- ev:
			default:
			}
			continue
		}
		if _, ok := sub.Topics["*"]; ok {
			select {
			case sub.Ch <- ev:
			default:
			}
		}
	}
}

func MarshalEvent(ev Event) []byte {
	raw, _ := json.Marshal(ev)
	return raw
}
