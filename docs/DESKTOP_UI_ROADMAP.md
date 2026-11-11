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

## Next UI milestones

1. Improve Command Center information hierarchy and reduce dashboard overload.
2. Make publication queue and delivery details navigable as a single material lifecycle.
3. Improve platform cards with adapter-backed capability and delivery-method labels.
4. Add agent activity only after a reliable backend event/status contract exists.
5. Add clear empty, loading, stale, partial-error, and unknown states to every workspace.
6. Validate at desktop widths and narrow webview widths; verify keyboard focus and reduced-motion behavior.

## Acceptance criteria

- No UI state is fabricated when the backend has no measurement.
- Login status and publishing readiness are never conflated.
- Platform-specific publication adapters continue to work unchanged.
- Every button performs a real action or is clearly disabled with an explanation.
- The app remains navigable when one data endpoint fails.
- Live publishing and retry actions remain explicit and recoverable.
