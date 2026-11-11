# Frontend API Contract: Publishing Queue Vertical Slice

**Status:** contract inventory for the first React migration slice  
**Source of truth:** current FastAPI routes and projections in `tgfarm/api.py`  
**Scope:** list → post details → safe delivery reconciliation. This document records existing behavior; it does not introduce new backend semantics.

## Read model

### `GET /api/posts`

Query parameters:
- `status` (optional): exact post status filter
- `channel_id` (optional)
- `hook_style` (optional)
- `variant` (optional)
- `limit` (1–500, default 50)

Response envelope: `{ "posts": PostListItem[] }`.

The current list projection (`_post_view(detail=False)`) includes:
- Identity and routing: `id`, `channel`, `channel_title`, `platform`
- Editorial metadata: `angle`, `post_format`, `hook`, `excerpt`, `chars`, `quality`
- Lifecycle: `status`, `scheduled_at`, `published_at`, `created_at`, `edited_at`, `reject_reason`
- Media references: `cover: { id } | null`, `video: { id } | null`
- Performance snapshot: `er`, `ctr`, `impressions`, `opens`, `views`
- Telegram delivery reference: `tg_message_id`
- Editorial-plan provenance fields forwarded by `plan_provenance(row, plan_labels)`

Date/time values are serialized by FastAPI from the backend datetime values. Treat nullable fields as nullable; do not manufacture zero-valued measurements or missing provenance.

### `GET /api/posts/{post_public_id}`

Returns the same common projection with detail fields added:
- `text`, `body`, `cta`, `sources`, `claims`, `hashtags`
- `format_reason`
- `external_url`, `remote_id`, `external_platform`
- `media[]` with `kind`, `path`, `sha`, `width`, `height`

Unknown post: HTTP 404. The frontend should preserve the queue context and show a local detail error rather than blanking the whole workspace.

### `GET /api/posts/{post_public_id}/trace`

Returns an ordered post event trace, current post status, media count, and the latest stats snapshot. This is the evidence timeline for explaining how a post reached its current state.

## Safe actions

### Reconcile an unknown delivery outcome

`POST /api/posts/{post_public_id}/reconcile`

Request model:
- `action`: `confirm_published` or `retry_after_check`
- `confirm_not_published`: boolean, defaults false

Rules enforced by the backend:
1. Reconciliation applies only while the post status is `publish_unknown`.
2. `confirm_published` records the post as published.
3. `retry_after_check` is rejected with HTTP 400 unless `confirm_not_published=true`.
4. An authorized retry moves the post to `scheduled` and sets a new schedule timestamp.
5. A post not in `publish_unknown` is rejected with HTTP 409.
6. Missing post is HTTP 404.

The UI must never expose a one-click generic retry for `publish_unknown`. The operator must first inspect the destination, then choose either:
- “Пост найден, подтвердить публикацию”
- “Поста нет, разрешить повтор” with an explicit confirmation step.

### Retry a definite failure

`POST /api/posts/{post_public_id}/retry` is a separate action for a definite failed publish. It is not a substitute for reconciliation. The route returns a conflict when the post is not eligible for that retry; the backend supplies the refusal reason.

### Bulk publish

`POST /api/posts/publish` accepts `{ "post_public_ids": string[] }`.

Before publishing, the backend checks for `publish_unknown` IDs and returns HTTP 409 with `error: "publish_outcome_unknown"`, the affected IDs, and an instruction to reconcile. The frontend must surface that refusal and route the operator to reconciliation, never retry the batch blindly.

## Frontend implementation rules

- FastAPI is authoritative. Do not reimplement publish eligibility in the browser.
- Use generated/maintained TypeScript types for the observed response shapes, but allow additive backend fields.
- Keep `publish_unknown` visually distinct from `failed`, `scheduled`, and `published`.
- Do not turn missing measurements into zero or missing provenance into empty strings.
- Treat detail, trace, and reconciliation as separate API states. A detail request failure must not erase the queue.
- Never place credentials, cookies, tokens, or browser-profile data in React state, DOM, logs, or telemetry.
- Keep all delivery mutations explicit and journalled through the existing API.

## Verified route locations

- `GET /api/posts` and `GET /api/posts/{post_public_id}`
- `GET /api/posts/{post_public_id}/trace`
- `POST /api/posts/{post_public_id}/reconcile`
- `POST /api/posts/{post_public_id}/retry`
- `POST /api/posts/publish`

Route declarations and request models live in `tgfarm/api.py`; list/detail field projection is owned by `_post_view`, and reconcile behavior by `_reconcile_publish_outcome`.
