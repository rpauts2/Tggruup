# Slotegrator Game Integration — Pentest Findings

## Target: go7.best / gis.slotegrator.com

## 1. Real Money Game Launch (is_demo: false) — CONFIRMED
POST /v1/games/{guid}/launches with {"is_demo": false} returns 201 with a real-money game URL.
- Game: The Dog House (Pragmatic Play)
- Currency: RUB (real Russian rubles, NOT demo)
- URL: https://gis.slotegrator.com/api/index.php/v1/client/games/launch/{base64url}/{encrypted_token}
- Balance: 0.0 RUB (user has not deposited)

## 2. SingleSessionAPI/data — Session Data Leak
POST https://cdn-1405.cloud4rucustomers001.net/gs2c/SingleSessionAPI/data
Headers: ssid: <ssid_value>, Content-Type: application/json
Body: {"ssid": "<ssid>", "cid": "<cid>"}
Returns:
{
  "error": 0,
  "data": {
    "balance": {"cash": 0.0, "bonus": 0.0, "total": 0.0},
    "currency": "RUB",
    "currencyCode": "RUB",
    "country": "RU",
    "jurisdiction": "99",
    "environmentId": 199,
    "mid": 83013008
  }
}
Vulnerability: Only requires `ssid` header — no additional authentication, JWT, or signature validation.

## 3. SSID Forgery — NOT VULNERABLE
Forged SSID values (changed environmentId, UUID, hash) return 200 but with empty balance data.
Server validates SSID against server-side session store.

## 4. LobbyClientAPI — PARTIALLY EXPOSED
POST /LobbyClientAPI/ping returns {"error":1,"description":"Session not found or unauthorized"}
Endpoint exists but requires proper session authentication.

## 5. reloadBalance.do / stats.do — Require Browser Session
Return HTML "unlogged.css" page — require cookies/full browser context, not accessible via simple API.

## 6. Bet Placement — NOT TESTED (Canvas/WebGL)
Game uses canvas rendering, no HTML spin button accessible via Playwright.
Betting goes through Pragmatic Play game server, not Slotegrator API.

## Summary
- CRITICAL: Real money game (RUB) can be launched without deposit/KYC
- MEDIUM: SingleSessionAPI/data leaks session data with only `ssid` header
- INFO: SSID forgery does not work (server-side validation)
- INFO: Balance is 0.0 (no funds at risk in this test account)