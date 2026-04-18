 # SMURF PBX
 
 SMURF is a custom SIP PBX foundation written from scratch in Go. This repository includes:
 
 - SIP registrar and B2BUA core with UDP/TCP/TLS listeners
- SIP over WebSocket transport for browser-facing signaling
 - Digest authentication with MD5 and SHA-256
 - Internal extension routing and RTP relay
 - HTTPS admin/API server on port 5001
- SQLite-backed persistence for extensions, registrations, failed auth, CDR, presence, chat, voicemail, and recordings
 - systemd unit and one-shot installer
 
 ## Current scope
 
 This codebase provides the first deployable SMURF core:
 
 - SIP REGISTER, INVITE, ACK, BYE, CANCEL, OPTIONS
- SIP over WebSocket listener and upgrade handling
 - Extension auth and registration persistence
 - Internal extension-to-extension call routing
 - RTP relay port allocation and media address rewriting in SDP
 - Admin login with JWT
- Admin panel for extensions, registrations, stats, CDR, presence, chat, voicemail, recordings, and browser-side softphone controls
- Presence and internal chat persistence with realtime event bus
- Voicemail and recording metadata persistence with API access
 - Automatic TLS certificate generation during install
 
 ## Installation
 
 Run as root or with sudo:
 
 ```bash
 sudo ./install.sh
 ```
 
 The installer will:
 
 1. Install build/runtime dependencies
 2. Create the `smurf` service account
 3. Generate `/etc/smurf/smurf.json` if missing
 4. Generate a self-signed TLS certificate if missing
 5. Build `/usr/local/bin/smurfd`
 6. Install and enable `smurfd.service`
 
 ## Default admin credentials
 
 These are seeded from the default configuration file:
 
 - Username: `admin`
 - Password: `admin123!`
 
 Change them in `/etc/smurf/smurf.json` before first production use.
 
 ## Default test extension
 
 Seeded automatically:
 
 - Extension: `1000`
 - SIP password: `12345`
 - Voicemail PIN: `1234`
 
 ## Ports
 
 - SIP UDP: `5060`
 - SIP TCP: `5060`
 - SIP TLS: `5061`
 - HTTPS admin/API: `5001`
- WebSocket SIP / realtime transport: `/ws` over HTTPS `5001`
 - RTP relay: `20000-20998/udp`
 
 ## Admin panel
 
 Open:
 
 - `https://<server>:5001`
 
 API endpoints:
 
 - `POST /api/login`
 - `GET /api/health`
 - `GET /api/extensions`
 - `POST /api/extensions`
 - `GET /api/registrations`
 - `GET /api/cdr`
 - `GET /api/stats`
 - `GET /api/snapshot`
- `GET/POST /api/ring-groups`
- `GET/POST /api/queues`
- `GET/POST /api/ivr`
- `GET/POST /api/conferences`
- `POST /api/browser/session`
- `POST /api/webrtc/offer`
- `POST /api/webrtc/answer`
- `GET/POST /api/presence`
- `GET/POST /api/chat`
- `GET/POST /api/voicemail`
- `GET/POST /api/recordings`
- `GET /ws` (WebSocket upgrade endpoint)
 
 ## Config
 
 Main config:
 
 - `/etc/smurf/smurf.json`
 
 Data:
 
 - `/var/lib/smurf/smurf.db`
 
 TLS:
 
 - `/etc/smurf/tls/server.crt`
 - `/etc/smurf/tls/server.key`
 
 ## Service management
 
 ```bash
 sudo systemctl status smurfd
 sudo systemctl restart smurfd
 sudo journalctl -u smurfd -f
 ```
 
 ## SIP notes
 
 For internal extension calling:
 
 1. Register extension `1000`
 2. Create another extension in the admin panel
 3. Register the second extension
 4. Place a SIP INVITE from one extension to the other
 
 SMURF rewrites SDP to anchor audio through its RTP relay pool.

## PBX routing entities

SMURF now includes persisted PBX routing entities:

- Ring groups
- Queues
- IVR menus
- Conference rooms

The current routing baseline is:

- Ring group: routes to the first reachable registered member excluding the caller
- Queue: routes using the stored strategy order baseline
- IVR: returns progress and then forwards to the default target
- Conference room: resolves as an internal routed destination

## Browser and realtime layer

- WebSocket upgrade endpoint is exposed at `/ws`
- JSON realtime events can be consumed over `/ws`
- SIP messages can be transported over WebSocket using the `sip` subprotocol
- The embedded web UI includes:
  - admin login
  - presence controls
  - internal chat
  - voicemail/recording views
  - initial browser softphone controls for SIP-over-WebSocket signaling
