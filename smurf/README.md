# SMURF PBX

SMURF is an in-house SIP PBX platform under active development. This repository contains **original SMURF code** (no Asterisk, FreeSWITCH, Kamailio, or OpenSIPS embedded as engines).

Replicating every feature of commercial suites such as 3CX is a **long-term roadmap**, not something represented in full by a single revision of this tree. What is implemented here is a **real, runnable core**: PostgreSQL-backed extensions, SIP REGISTER with digest (**SHA-256**), UDP/TCP/TLS/WSS for SIP, symmetric RTP relay, internal INVITE and **call queues** (`call_queues` / `support` hunt to members), **signed HTTP webhooks** on `call.answered` / `call.ended`, HTTPS API on **port 5001** with JWT, plus `install.sh` and **systemd** units.

## Ports

| Service    | Port | Protocol | Notes                          |
|-----------|------|----------|--------------------------------|
| SIP       | 5060 | UDP/TCP  | configurable via env          |
| SIP TLS   | 5061 | TCP/TLS  | optional (`SMURF_SIP_TLS`)   |
| SIP WSS   | 5081 | WebSocket + TLS | `SMURF_SIP_WSS`, path `/sip`, subprotocol `sip` (RFC 7118) |
| Admin/API | 5001 | HTTPS    | self-signed cert by installer |
| RTP relay | dynamic | UDP   | allocated per call on relay bind IP |
| Relay control | 19000 | TCP | localhost only by default     |

## Quick install (Debian/Ubuntu)

```bash
sudo SMURF_LISTEN_IP=127.0.0.1 ./install.sh
```

- **Admin UI**: `https://<host>:5001/` — user `admin`, password `smurfadmin` (change after install).
- **SIP extensions**: `1000` / `smurf1000`, `1001` / `smurf1001` (from `sql/seed.sql`).
- **Web softphone**: `https://<host>:5001/softphone` — WebRTC audio + SIP signaling over WSS to `SMURF_SIP_WSS` (default `wss://<host>:5081/sip`). Trust the server certificate in the browser.
- **Call queue**: dial extension **`support`** (sequential hunt to `1000` then `1001` per `sql/seed_queues.sql`). CDR stores `to_ext = support` and `queue_slug`.
- **Webhooks**: create via API `POST /api/v1/webhooks` with `{"url":"https://...","secret":"...","events":["call.answered","call.ended"]}`. Each POST includes headers `Smurf-Event`, `Smurf-Timestamp`, `Smurf-Signature` (`sha256=` + HMAC-SHA256 of `timestamp + "." + body`).

Environment overrides live in `/etc/smurf/smurf.env`.

## Development

```bash
go test ./...
go build -o smurfrelay ./cmd/smurfrelay
go build -o smurfsip ./cmd/smurfsip
go build -o smurfapi ./cmd/smurfapi
```

## Honest scope

Further work is required for full enterprise parity (WebRTC stack, T.38, HA clustering, full RFC3261 edge cases, SRTP, ICE/TURN integration, mobile push, and so on). The architecture here is intended to grow: separate `smurfsip`, `smurfrelay`, and `smurfapi` processes, PostgreSQL as the source of truth, and systemd supervision.
