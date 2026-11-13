# TgFarm Desktop Architecture Decision

**Status:** Accepted as the next implementation direction  
**Date:** 2026-11-11  
**Scope:** Desktop UI architecture and its integration with the existing Python application

## Decision

Modernize the frontend to **React + TypeScript + Vite**, while keeping the current **Python backend, FastAPI local service, and pywebview desktop shell** for the first implementation stages.

Do not migrate the shell to Tauri or Electron as part of the first frontend rewrite. Keep Tauri 2 as a measured future option, not a prerequisite.

This is a deliberate incremental decision: improve the part that is currently limiting product quality (the growing monolithic HTML/JavaScript UI) without destabilizing the working publication engine, browser automation, scheduler, database, tray, or packaging path.

## Why this is the best fit for TgFarm now

TgFarm is already a Python-first desktop product. Its business logic, platform adapters, Playwright flows, scheduler, persistence, local API, tray integration, and desktop lifecycle are Python-based. The current desktop shell already starts the FastAPI service and hosts the UI in pywebview.

Replacing that shell immediately would add a second migration at the same time as the UI rewrite:

- Tauri requires a Rust shell and a separately packaged Python sidecar.
- Electron requires a Node.js desktop main process and ships its own Chromium runtime.
- PySide6 would move the UI into Qt/QML or Qt widgets and discard most of the existing web UI work.
- pywebview already supports a modern web frontend, a JavaScript/Python bridge, native menus, dialogs, window management, and bundled frontend assets.

React + TypeScript + Vite gives the interface a maintainable component architecture and a predictable build pipeline while preserving the current operational core.

## Evaluated options

| Option | Benefits | Costs / risks | Decision |
|---|---|---|---|
| React + TypeScript + Vite inside pywebview | Reuses Python, existing desktop lifecycle, local API, tray, Playwright, and current platform adapters; modern component architecture | Still depends on platform WebView behavior; frontend build pipeline must be added | **Choose now** |
| Tauri 2 + React + TypeScript + Python sidecar | Compact native shell, explicit capability permissions, modern web frontend, native desktop integration | Rust toolchain, sidecar packaging, process lifecycle, IPC/API boundary, installer and update work; system WebView differences remain | Re-evaluate after a working vertical slice |
| Electron + React + TypeScript | Mature ecosystem, bundled Chromium consistency, large community and desktop integrations | Larger application footprint, Node main process, duplicated runtime/process lifecycle, extra packaging/security surface | Not justified for current needs |
| PySide6 / Qt | Native widgets, direct Python integration, mature cross-platform toolkit | UI would need a substantial QML/widgets redesign; web-style editorial workspaces and visual iteration become less direct | Not chosen |
| Keep plain HTML + large app.js | No framework migration | Component boundaries, state ownership, contracts, and testing become harder as the product grows | Retire incrementally |

## Frontend stack

### Core
- React for reusable screens, components, and interaction state.
- TypeScript with strict checking for API models, platform capabilities, statuses, and action contracts.
- Vite for development server, hot reload, and optimized static production assets.
- Existing FastAPI backend remains the source of truth.

### UI system
- shadcn/ui components as owned source code, not as a locked black-box theme.
- Radix UI primitives for accessible dialogs, menus, popovers, tabs, and keyboard behavior.
- Tailwind CSS for layout and consistent design tokens.
- Lucide icons.
- Preserve TgFarm's dark navy / blue visual identity. Do not copy another product's interface wholesale.

### Data and state
- TanStack Query for API-backed state: loading, caching, background refresh, mutations, invalidation, and error handling.
- Local React state for local interactions.
- Do not add a global state library until a real cross-screen client-state need appears.
- Generate or maintain TypeScript API types from FastAPI's OpenAPI schema. Never duplicate backend status rules in UI without a typed contract.

### Charts
- Keep charts focused on actionable editorial and platform metrics. Use an established React chart library only where a chart improves understanding; avoid chart decoration for its own sake.

## Runtime architecture

    TgFarm Desktop
      ├─ pywebview native window, menu, tray, lifecycle
      ├─ React + TypeScript frontend (built static assets)
      └─ local authenticated FastAPI service
           ├─ editorial pipeline and AI roles
           ├─ platform adapters and delivery reconciliation
           ├─ Playwright browser profiles and web editors
           ├─ scheduler and background jobs
           ├─ database and media
           └─ diagnostics, events, and telemetry

The frontend communicates with the existing local FastAPI API. The API remains authoritative for state and permissions. Secrets, tokens, cookies, and browser profile contents must never be placed in frontend state, HTML, logs, or telemetry.

Playwright login and publishing sessions remain separate from the desktop WebView. The app must not assume that a user being logged into the desktop WebView means the platform's Playwright profile is authenticated.

## Design direction

The application should feel like an editorial operations workspace, not a generic admin template.

1. **Command Center:** only the most important system health, queue pressure, recent editorial cycle, and next operator actions.
2. **AI Team:** roles, actual run stages, decision trace, shared context, and failures. Never invent live activity.
3. **Editorial Studio:** idea, research, outline, draft, fact-check, critique, media, and platform variants in one material workspace.
4. **Publishing Queue:** grouped by lifecycle and urgency, with filters and a clear delivery timeline.
5. **Platform Center:** capability, login method, delivery transport, verification level, supported formats, and last observed result as separate facts.
6. **Analytics & Learning:** platform-specific performance, reliable sample sizes, format comparisons, and feedback into planning.
7. **System:** sources, run journal, diagnostics, settings, and backups.

Interaction standards:
- Persistent left navigation and a compact top workbar.
- Search / command palette for navigation and frequent actions.
- Split-pane or drawer details for inspecting an item without losing queue context.
- Consistent empty, loading, stale, partial-error, unknown, and success states.
- Keyboard navigation, visible focus, reduced-motion support, and clear destructive-action confirmation.
- Every important status must explain the next safe action.
- publish_unknown must never be presented as a normal failure or safe retry.

## Migration plan

### Phase 0 — Complete the current UI foundation
- Keep the existing main branch untouched.
- Continue only in feature/desktop-command-center-ui.
- Complete and document the Command Center / AI Team visual foundation.
- Preserve all existing backend endpoints and publishing adapters.

### Phase 1 — Frontend foundation
- Add a dedicated frontend workspace and lockfile.
- Configure React, TypeScript strict mode, Vite, Tailwind, and shadcn/ui.
- Build the app shell and design tokens using the current TgFarm palette.
- Configure Vite output to the static directory served by the existing Python app.
- Add type-check and production-build checks to CI.
- Keep the existing UI available until the new shell passes smoke checks.

### Phase 2 — Vertical slice
Migrate one complete workflow first: **Publishing Queue → Post Details → Safe Reconciliation**.
- Reuse the existing API endpoints.
- Preserve provenance, quality trace, platform state, and reconciliation confirmation.
- Verify that publish_unknown cannot be blindly retried.
- Test loading, empty, stale, API failure, and success states.
- Compare behavior against the current UI before switching the default entry point.

### Phase 3 — Workspace migration
Migrate in product order:
1. Command Center
2. AI Team and editorial trace
3. Platform Center and connections
4. Editorial planning and trends
5. Analytics, memory, sources, and diagnostics

Migrate one workspace at a time. Do not rewrite the backend just to fit a component.

### Phase 4 — Desktop shell decision gate
Only reconsider Tauri 2 after measuring the React frontend inside pywebview:
- Windows startup time and memory use
- WebView compatibility and rendering defects
- tray, notifications, file dialogs, and window lifecycle
- Playwright coexistence and browser-profile isolation
- installer size, update path, and recovery after crashes

If pywebview meets the product requirements, keep it. If it fails measurable requirements, prototype Tauri 2 with the same React build and Python sidecar, then compare actual results before migrating.

## Acceptance criteria
- The Python editorial and publishing engine remains intact.
- The new frontend can be built reproducibly from a lockfile.
- Type checking and production build run in CI.
- No secret is exposed to the renderer.
- No adapter capability is inferred from login status.
- All current queue actions retain their existing safety semantics.
- The UI never invents agent activity or delivery success.
- One failing API endpoint does not blank the entire application.
- The app is usable at the supported Windows desktop size and at compact window sizes.
- The old UI is not removed until the new workflow passes automated and manual verification.
## Reference products and what their choices tell us

- **Visual Studio Code:** Microsoft's official documentation describes VS Code as built on Electron, combining web technologies and Node.js with native desktop integration. This demonstrates the maturity of the web-UI desktop model, but does not mean every Python-first app should adopt Electron.
- **Slack Desktop:** Slack Engineering documented its migration from direct DOM manipulation to React and its incremental move to TypeScript inside Electron. Its later desktop rewrite emphasized composable UI components, lazy/incomplete data models, and multi-workspace awareness. The transferable lesson for TgFarm is the component architecture and explicit data contracts, not Slack's exact process model.
- **Notion Desktop:** Notion's public desktop UX documentation highlights workspace tabs, quick search, notifications, and fast context switching. These are useful interaction patterns for TgFarm's command palette, material workspaces, and persistent editorial context; the public help page does not establish Notion's internal framework, so no stack claim is made here.

These examples reinforce the decision to modernize the frontend architecture independently from the native shell. React and TypeScript are proven for large desktop workspaces; Electron is one established host, not a mandatory dependency.

Reference links:
- https://code.visualstudio.com/docs/editor/whyvscode
- https://slack.engineering/building-hybrid-applications-with-electron/
- https://slack.engineering/rebuilding-slack-on-the-desktop/
- https://www.notion.com/help/notion-for-desktop
