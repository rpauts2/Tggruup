# TgFarm autonomy acceptance matrix

This is the release gate for the end-to-end farm, not a claim that every platform has passed live acceptance. CI proves deterministic local behavior; owner-controlled live checks prove external account access and delivery.

## Safety invariants

- [ ] `off` / demo mode cannot make an external publish request, even when the environment was configured for live mode.
- [ ] A publish attempt has a durable local record before external delivery starts.
- [ ] A confirmed remote success stores the remote ID/URL and a terminal local state.
- [ ] A provider-side scheduled queue is distinct from a public post: store a remote ID or public URL only when the platform returns that identifier or permalink.
- [ ] A timeout or process death after a possibly accepted request becomes `publish_unknown`; it is never blindly retried.
- [ ] Retrying an unknown result requires operator reconciliation with the destination.
- [ ] Retryable failures have a bounded attempt count and respect provider retry delays.
- [ ] Logs, diagnostics, and API responses never expose access tokens, cookies, OAuth secrets, or raw secret-bearing provider responses.

## Recovery scenarios

Run each against an isolated temporary database unless a row explicitly requires a real account.

| Scenario | How to exercise | Required result |
|---|---|---|
| Clean restart with queued posts | Schedule a future post, stop and restart the process | Queue survives; one logical task remains |
| Crash before external request | Terminate after durable task creation but before adapter call | Task can resume safely |
| Crash after external request | Simulate adapter acceptance then kill before local confirmation | State is unknown; no automatic duplicate |
| Provider timeout | Fake a read timeout after request submission | Unknown result; operator reconciliation required |
| Rate limit | Fake 429 with `Retry-After` seconds and HTTP-date | Bounded delay, no hot retry loop |
| Permanent provider rejection | Fake a non-retryable 4xx | Terminal failure with actionable safe diagnostic |
| Exhausted retries | Repeatedly return retryable errors | Attempts stop at configured limit and watchdog alerts |
| Corrupt backup | Restore an invalid SQLite snapshot | Restore fails closed; original DB remains untouched |
| Valid backup restore | Restore a verified snapshot into a temporary DB | Integrity, schema, row counts, and foreign keys pass |
| Duplicate scheduling | Enqueue the same logical publish job twice | One active logical job / no duplicate remote delivery |
| Stale worker heartbeat | Leave an active task without heartbeat updates | Watchdog flags it; recovery does not blindly republish |
| Offline demo in live-configured environment | Run `tgfarm demo --cycles 1` with live env configured (default store is a throwaway database; `--configured-store` opts into the production one) | Synthetic content only; zero outbound publish requests; the configured database is untouched unless `--configured-store` was passed |

## Platform acceptance

For each platform adapter, record the capability explicitly: `unsupported`, `read_only`, `draft_only`, `manual_upload`, or `publish`. Do not display “connected” as equivalent to “can publish”.

| Check | Evidence required |
|---|---|
| Authentication | Successful read-only identity/permissions check with an owner-controlled account |
| Publish capability | Official supported endpoint or documented browser workflow, with limitations recorded |
| Single-post smoke test | One controlled post in a designated test destination |
| Remote reconciliation | Remote ID/URL matches the local publication record |
| Restart | Same job is not delivered again after process restart |
| Revoked/expired access | UI and watchdog show a failed or stale auth state without exposing credentials |
| Rate limit / provider outage | Bounded retry or unknown-result flow, according to whether delivery may have happened |

Never bypass CAPTCHA, access controls, platform rate limits, or consent requirements. If a platform does not offer an appropriate supported publishing interface, report that limitation and offer a prepared draft/manual upload instead.

## Autonomy-loop acceptance

The autonomous loop is only considered end-to-end when each transition is persisted and observable:

`source → candidate → ranked topic → draft → quality gate → media → schedule → publish → metrics → next recommendation`

- [ ] Each stage stores status, timestamps, attempt count, and a safe error category.
- [ ] Every generated factual claim links to its source or is explicitly marked as opinion/analysis.
- [ ] Deduplication checks both source items and prior published content for the target channel.
- [ ] Quality failures route to bounded revision or rejection, never an infinite generation loop.
- [ ] Scheduling respects channel limits, local time zone, platform capability, and daily AI budget.
- [ ] Analytics recommendations state sample size and confidence; insufficient data produces “not enough data”.
- [ ] Analytics may suggest changes first; automatic policy changes require explicit opt-in and hard bounds.
- [ ] A single failing source or platform cannot block independent work for the rest of the fleet.

## Operator release checklist

Before enabling autonomous live mode:

1. Run `pytest tests -q`, `ruff check tgfarm tests`, and `tgfarm demo --cycles 1` (leave the default throwaway store; only add `--configured-store` when the point of the run is the live database).
2. Run `tgfarm doctor` and resolve all critical findings.
3. Verify a fresh backup with `tgfarm backup` and `tgfarm backup-test FILE`.
4. Complete the relevant platform smoke-test runbook with an owner-controlled test account.
5. Confirm the emergency stop prevents new external publish calls while preserving task history.
6. Start with a small per-platform quota and a daily AI budget; increase only after clean evidence.
7. Save the test date, app version/commit, platform, local post ID, remote ID/URL, result, and any unresolved limitation.

A green CI run is necessary but is not evidence of live OAuth scopes, account permissions, platform approval, or successful production publishing.
