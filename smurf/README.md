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
- **Call queue**: dial extension **`support`** (sequential hunt to `1000` then `1001` per `sql/seed_queues.sql`). CDR stores `to_ext = support` and `queue_slug`. For **plain RTP UDP** callers (no SRTP in the offer), the server sends **183 Session Progress** with PCMU SDP on the relay leg and streams **MoH** from `SMURF_QUEUE_MOH_WAV` (default `/var/lib/smurf/moh/default.wav`) until a member answers or the hunt times out.
- **Webhooks**: create via API `POST /api/v1/webhooks` with `{"url":"https://...","secret":"...","events":["call.answered","call.ended"]}`. Each POST includes headers `Smurf-Event`, `Smurf-Timestamp`, `Smurf-Signature` (`sha256=` + HMAC-SHA256 of `timestamp + "." + body`).
- **Voicemail deposit**: dial `*<ext>` (e.g. `*1000`) while authenticated; server answers with SDP, records **PCMU** RTP to **WAV** under `SMURF_VOICEMAIL_DIR` (default `/var/lib/smurf/voicemail`), inserts a row in `voicemail_messages`, sends **MWI NOTIFY** (`message-summary`) to the mailbox if registered. List/download: `GET /api/v1/voicemail/{ext}`, `GET /api/v1/voicemail/{ext}/download/{id}` (JWT).
- **SIP trunks (outbound)**: configure `sip_trunks` via `POST /api/v1/sip-trunks` (host, auth, `priority` for failover). Calls to **E.164-style** destinations (8+ digits, optional `+`) go out through trunks in priority order: **REGISTER** (digest) runs periodically; **INVITE** uses digest on 401/407, **ACK** on 200, **BYE** to provider when caller hangs up.
- **Office hours**: table `office_hours` per extension (weekday bitmask + `time_start`/`time_end` UTC, `outside_target`). Example: extension `1000` outside hours routes INVITE target to IVR slug `main` (see `sql/seed_ivr_moh.sql`).
- **IVR**: dial the menu **slug** as extension (e.g. `main`). Server answers with WAV from `ivr_menus.welcome_file` (default `/var/lib/smurf/moh/default.wav` — place a **mono 8 kHz 16-bit PCM WAV** there). DTMF: send **`INFO`** with body a single digit (`0`–`9`, `*`, `#`) and `Content-Type: application/dtmf-relay`. Action from `ivr_options`: extension number, `queue:support`, or `*1000` for voicemail. After digit, server sends **`REFER`** to the caller’s Contact to redirect the call.
- **Call recording**: set `extensions.record_calls = true` (DB or future API). Internal calls use **smurfrelay tap** to duplicate caller RTP (PCMU) to a WAV under `SMURF_RECORDINGS_DIR` (default `/var/lib/smurf/recordings`). Same for **IVR** sessions when the caller has `record_calls`.
- **WebRTC ICE**: set `SMURF_ICE_SERVERS` to a comma-separated list of STUN/TURN URLs (e.g. `stun:stun.l.google.com:19302`) for the browser leg.
- **SRTP (SDES)**: for **internal extension** and **queue** calls, if the caller’s INVITE SDP includes `a=crypto:` with **AES_CM_128_HMAC_SHA1_80**, `smurfsip` offers a fresh crypto line toward the callee and, after the 200 OK, programs **smurfrelay** via TCP `srtp` command so RTP is **decrypted** from each leg and **re-encrypted** toward the peer (same master key as in each leg’s SDP). WebRTC legs remain SRTP inside the browser; classic SIP+SRTP uses the relay path. Trunk and voicemail legs are unchanged in this revision.

Environment overrides live in `/etc/smurf/smurf.env`.

## Development

```bash
go test ./...
go build -o smurfrelay ./cmd/smurfrelay
go build -o smurfsip ./cmd/smurfsip
go build -o smurfapi ./cmd/smurfapi
```

## Honest scope

Further work is required for full enterprise parity (MoH during queue hunt for **SRTP** or **TCP** callers, SRTP on **trunk** legs, T.38, HA clustering, full RFC3261 edge cases, mobile push, and so on). The architecture here is intended to grow: separate `smurfsip`, `smurfrelay`, and `smurfapi` processes, PostgreSQL as the source of truth, and systemd supervision.
