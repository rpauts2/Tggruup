# Pentest Log — go7.best

Target: https://go7.best/
Account: tg_352509087_5e1fae0d + second account (id 1605)
Date: 2026-10-28

## Log

### 1. Recon
- Downloaded all JS bundles (37 files) via Playwright + curl.
- Extracted endpoint map: /v1/auth/*, /v1/consumer/*, /v1/loyalty/*, /v1/referral/*, /v1/billing/*, /v1/event-hub/*, /v1/traffic/*, /v1/site-config, /v1/socket-tokens, /v1/geo, /v1/freespins/active, /v1/categories.
- No hidden API, no .env/.git/.env.backup/swagger exposed (SPA fallback 200).
- Subdomain scan: api/www/admin/test/staging/dev/app/ws/socket/cdn/static -> all 502/cert error, no takeover.
- SSTI/SQLi/NoSQLi/XSS probes on params -> no vulns detected (JSON API, strict validation).

### 2. IDOR / BOLA
- Tried user_id, wallet_id, transaction_id, appeal_id with Account 2 token.
- Server returns own data or 404; no cross-account data leak.

### 3. Business Logic
- Race: bonus claim (30 parallel) -> 1 success (201), 29 rejected (400), no double-spend.
- Cash-down create (50 parallel) -> 500/400, rate limited.
- Negative/zero amounts -> 400 validation.
- KYC bypass -> KYC apply returns 500 (validation), status remains none.
- Wager bypass -> no betting endpoint in API; wagering events delivered by game providers.

### 4. Injection / XSS
- Prototype pollution payloads (JSON + form-encoded): 400 validation, no pollution.
- DOM XSS via location.hash: no reflection, no alert.
- SSTI: 500 on promocode (no eval), not exploitable.
- Unsafe deserialization: JWT alg:none rejected, no RCE.

### 5. WebSockets
- Connected anonymously with socket token.
- Publish denied (permission denied), all channels user/admin/system -> 403.

### 6. Supply Chain / Headers
- SRI missing on scripts/CDN.
- Headers: missing HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, CSP, etc. (Grade F).
- No versions leaked (Server: ddos-guard only).

### 7. Tools
- Nuclei install failed (network/Go build issues); not executed.
- FFuf not installed; manual directory scan done with Python (no hidden endpoints beyond SPA fallback).

### 8. Findings Summary
- Critical: missing security headers, 500 internal errors, anonymous socket tokens (with 403 gate), missing SRI.
- No confirmed RCE, IDOR, blind SQLi, XSS, or business-logic bypass.
### 2026-10-28 23:00 — Game Integration & WebSocket
- Discovered POST /v1/games/{guid}/launches (game launch API).
- Called launch for game 2bdb3a46-470f-4c09-ab17-254132cb0e6f (pragmatic) with is_demo=true -> 201.
- Server returned URL pointing to gis.slotegrator.com (game provider server) with signed session token in path.
- No JWT in URL visible; session passed to provider via signed token.
- Attempted to register custom prototype pollution payloads (JSON + form-encoded) -> server returned 400 validation, no pollution.

### 2026-10-28 23:05 — WebSocket
- Anonymous socket token accepted session creation.
- Publish to channel: permission denied (103).
- All private/admin channels: 403 access denied (correct enforcement).

### 2026-10-28 23:10 — Billing API
- /v1/billing/top-up/create and /v1/consumer/billing/cash-down/create require valid structured JSON body and return 400/500 on invalid types.
- Negative/zero amounts rejected by validation (400), not accepted.
- No arbitrary URL fields usable for SSRF.

## Findings
- No open RCE/IDOR/XSS found in tested API surface.
- Missing security headers (critical hygiene).
- Broken 500 handling leaks trace_id.
- Anonymous socket token accepted but channels are protected.
- SRI missing on client scripts.
