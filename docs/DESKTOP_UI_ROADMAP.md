# TgFarm Desktop UI Roadmap

## Product direction

The Desktop application is the editorial operations workspace for TgFarm. It should make the state of the whole publishing system understandable without requiring the operator to inspect logs or understand implementation details.

This is a UI-first roadmap. Existing publication adapters and authentication mechanisms must be preserved while the interface is improved. A platform is not required to expose an API: the UI must describe the real delivery method supported by that platform's adapter.

## Core interface principles

1. **Show observed state, not optimistic state.** A successful login is not proof that publishing works. A scheduled item is not a published item. Missing telemetry is not zero.
2. **Separate account access from delivery transport.** Login may use OAuth, a browser profile, a token, or a platform-specific flow. Delivery may use an API, browser automation, an official web editor, or a manual hand-off. These are separate capabilities.
3. **Make the next action obvious.** Every warning should explain what happened, why it matters, and the safest available next action.
4. **Preserve platform-specific reality.** Dzen's existing flow and other non-API adapters remain first-class. Do not force all platforms into a generic OAuth/API abstraction.
5. **Do not fake agent activity.** Editorial roles can be represented in the UI, but active/idle/error states must come from real telemetry.
6. **Desktop first, responsive second.** Use the available width for useful workspaces, with sensible compact behavior for narrow windows.
7. **Keep destructive and live actions explicit.** Publishing, retrying uncertain deliveries, and changing LIVE mode require clear confirmation and status feedback.

## Navigation map

### Command Center
Operational overview: farm status, recent pipeline, publishing backlog, delivery health, quality signals, and the next issues requiring attention.

### AI Team
Editorial roles and the real pipeline stages. The current UI reuses the run journal for stage status. Role cards are explanatory, not fake live agent monitors. Future live agent telemetry should be connected only when the backend exposes it.

### Editorial workspaces
Planning, queue, trends, publication memory, and review board. These should converge around a material's lifecycle and make its history easy to follow.

### Distribution
Channels, platform capability catalog, and connections. Platform cards should eventually show separate fields for login method, publishing transport, supported formats, verification status, and last successful delivery.

### System
Sources, run journal, and diagnostics.

## Functional coverage map: S1–S12

Navigation is organized around operator workflows, not one menu item per backend module. A stage may appear in more than one workspace: the Command Center summarizes it, its specialist workspace exposes the detail, and the post lifecycle keeps the relevant evidence together.

| Product stage | Existing UI surface | Design rule and remaining direction |
|---|---|---|
| **S1 Research / sources** | Sources workspace, source-health summary, cooldown/reset action, reprint backfill status, source KPIs | Keep source health, last attempt, freshness and cooldown as separate facts. Grow this into a source registry with search, filters and source detail without turning the dashboard into a raw log. |
| **S2 Trend intelligence** | Trends workspace, trend phase/acceleration, “why?” explanation and router-skip explanation | Keep trend evidence and confidence visible. A trend should open its dossier and related editorial decisions rather than ending at a heat score. |
| **S3 Intelligent routing** | Distribution plan, platform catalog, community-target recommendations and rule history inside editorial flows | Show why a destination was selected or excluded, what rule snapshot was used, and whether its capability/access is actually sufficient. Do not collapse “platform enabled”, “account connected” and “destination eligible”. |
| **S4 Editorial engine** | Editorial planning, AI Team pipeline, post details, native editorial preview and existing strategist/writer benchmark | Keep a material's brief, source set, claims, draft versions and review history connected. Role cards explain responsibility; execution status comes only from real run telemetry. |
| **S5 Review board** | Needs-attention workspace, quality/review details and post drawer | Separate passed, skipped, fallback, rejected and failed checks. Make blockers and the next safe action understandable without treating missing AI review as approval. |
| **S6 Visual media** | Cover/video references on post cards and details, platform preview and short-video preparation actions | Treat media as part of the material, with provenance, dimensions, processing state, licensing evidence and platform fit. Do not imply an asset is licensed or ready merely because a file exists. |
| **S7 Scheduling and publishing** | Publication queue, review board, crosspost health, post trace, publish_unknown reconciliation | Make the material lifecycle the primary navigation path: draft → review → scheduled → delivery attempt → verified publication/unknown. Unknown outcomes must remain distinct and reconciliation-first. |
| **S8 Analytics** | Command Center report, portfolio insights, AI usage, freshness, coverage, attribution and format-performance panels | Every metric needs source, measurement window, sample size and freshness. Unmeasured stays unknown; platform metrics stay separate unless explicitly normalized. |
| **S9 Native crossposting** | Platform-specific editorial preview, draft/manual hand-off, per-platform delivery details | Treat each destination as its own editorial version. Show differences, constraints and adaptation provenance, not a generic “copy to all” action. |
| **S10 Format planning** | Editorial plan entries, platform profiles, format-performance evidence | Show the chosen format and the evidence/reason behind it at the decision point. Do not present an experimental winner based on a tiny or unmeasured sample. |
| **S11 Video studio** | Existing video/short-video preparation controls and media references in editorial flows | Keep the full video workflow attached to the material: script, scenes, assets, voice, subtitles, export and license gate. Do not invent a separate studio data model; reuse tgfarm/s11_video/. |
| **S12 Learning and memory** | Publication memory, topic dossier, portfolio insights and format-learning summaries | Connect each lesson to its underlying publications and measurement evidence. Keep a recommendation distinguishable from an automatic strategy change. |

### Cross-cutting operator workflows

- **Account and delivery readiness:** connections explain the login method; the platform catalog explains supported delivery capabilities; the queue explains whether an individual post can actually proceed.
- **Channel and network control:** channel enablement, per-platform limits and the global editorial plan belong together in distribution settings, with clear scope and impact before changes.
- **Diagnostics and recovery:** run history, post trace, source health and delivery reconciliation should link to the affected material or subsystem. Operators should not have to search unrelated logs manually.
- **Shared data trust:** every workspace must distinguish loading, empty, stale, partial failure, unknown and measured-zero states. The global refresh indicator summarizes the data snapshot; individual views retain their own domain-specific explanations.
- **Extensibility:** new platforms and media types should be described by capability metadata and workflow contracts, not hard-coded assumptions that every network supports the same login, publishing or metrics API.

This map describes UI surfaces present in the current dashboard and intended information architecture. It is not a claim that every stage is fully autonomous or every listed operation is available for every platform.

## Connection and delivery model

The UI must distinguish these independent dimensions:

- **Login method:** OAuth, API token, browser session, platform-specific flow, or manual credentials where unavoidable.
- **Delivery transport:** API adapter, browser automation, platform web editor, manual hand-off, or unsupported.
- **Verification:** not checked, access verified, delivery tested, needs attention, or unknown.
- **Capability:** text, image, video, long-form article, scheduling, metrics, and other features only when confirmed by the adapter.

Do not claim that arbitrary cookies or all browser storage are extracted. Keep credentials and session material local, private, and out of rendered UI. A browser login may prove that a session exists without proving that an automated publishing route is available.

## Current UI-first iteration

- Added a visual refinement layer without replacing the existing theme or component contracts.
- Added the AI Team workspace, showing the actual latest pipeline stages from the run journal.
- Added explanatory editorial-role cards, explicitly separated from live execution telemetry.
- Made connection cards show login method and connection health independently.
- Corrected the connections introduction so it does not promise that every platform saves tokens or uses the same login flow.
- Added explicit tab-to-panel ARIA relationships, named tab navigation, and synchronized selected/hidden states.
- Added browser assertions for every tab/panel relationship and keyboard navigation (arrow keys, Home, End, focus synchronization).
- Added a shared data-freshness indicator with manual refresh, partial-failure visibility, the last successful full snapshot time, and deduplicated recovery notifications.
- Added a browser scenario that simulates one failed data endpoint, verifies that the dashboard identifies it as stale, and verifies recovery through the refresh control.
- Turned the existing post trace into a readable lifecycle timeline with status labels, event timestamps and media count; timestamps without timezone information are shown without silently converting them to local time.
- Removed duplicated short-video actions from published-post details so the same preparation controls appear only once.
- Fixed the narrow-webview grid min-content overflow: the sidebar and workspace now stay within the viewport while the tab row scrolls internally. Browser QA passed at 390 px.
- Added the active channel/persona count to supported platform cards, with Russian singular/plural forms and an honest no-report fallback. The visual fixture now covers both automatic publishing and owner-consent/manual-upload capability on desktop and mobile.
- Added a delivery-capability filter to the platform catalog (automatic publish, draft-only, manual package/consent, read-only, unsupported), separate from enablement/setup filters. Reset restores all filters; browser QA covers each behavior.
- Hardened the review board as an operator decision surface: missing age and attempt measurements stay explicitly unknown instead of rendering as zero; reconciliation, retry, immediate publish and rejection actions require a confirmation step.
- Added browser coverage proving that dismissing the «Проверил: поста нет» confirmation sends no reconciliation request, and that absent age/attempt fields remain unknown.
- Connected both the review board and the Command Center's unresolved-delivery worklist to the same material drawer. Operators can open the full post, editorial context and lifecycle trace before deciding; existing reconciliation actions remain separate and confirmation-gated.
- Unified queue cards, overview rows and review cards around the same material drawer. Queue entries are keyboard-operable; opening a material loads its delivery trace automatically in a separate slot, so editorial previews cannot overwrite lifecycle evidence.
- Made the material drawer an accessible modal workspace: focus moves to its close control, stays inside while open, Escape closes it, and focus returns to the exact queue/review entry. The detail-read error state retains an explicit close action; trace reads are guarded against late responses from a previously opened material.
- Added unit and browser coverage for opening a material from the review board, while retaining the cancel-confirmation test that proves no reconciliation request is sent when the operator backs out.
- Reprioritized the Command Center's four primary shortcuts around operator work: «Требует внимания», planning, publication queue and connections. AI Team remains available in primary navigation, while the highest-risk review path is now one click from the overview.
- Reordered the overview so the real pipeline state and editorial results come first, followed immediately by the last publish decision and per-platform backlog. Portfolio, AI-usage and measurement analytics remain available below the operational workspace rather than competing with daily delivery decisions.
- Added a browser assertion that the attention shortcut opens the review workspace and returns cleanly to the overview.

## Next UI milestones

1. Improve platform cards with adapter-backed login, delivery, media, scheduling and metrics capabilities.
2. Apply the same loading, empty, stale, partial-error and unknown-state contract to each specialist workspace.
3. Make source, trend, community, review and analytics detail views trace back to the source evidence and affected material.
4. Add agent activity only after a reliable backend event/status contract exists.
5. Validate the complete material lifecycle in the Windows pywebview shell, including keyboard/focus behavior and reduced-motion settings.

## Acceptance criteria

- No UI state is fabricated when the backend has no measurement.
- Login status and publishing readiness are never conflated.
- Platform-specific publication adapters continue to work unchanged.
- Every button performs a real action or is clearly disabled with an explanation.
- The app remains navigable when one data endpoint fails.
- Live publishing and retry actions remain explicit and recoverable.


### Publication queue status coverage
- Added direct queue filters for failed publications and drafts. The labels correspond to existing Post.status values and use the existing /api/posts?status=... query; no publishing or retry behavior changes.
- Isolated material-detail reads with a monotonically increasing request identity. If an operator opens material B before material A finishes loading, or closes the drawer while a read is pending, the older response/error is ignored and cannot replace the current drawer contents.
- Added a browser regression scenario for closing a pending material A, opening material B, resolving B first and A last; the late response must not replace B or reopen the drawer.
- The queue visible status set is now: all, gate-passed, scheduled, uncertain delivery, failed, draft, published and rejected.
