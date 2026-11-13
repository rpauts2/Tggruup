# TgFarm UI Visual QA

## Purpose

The dashboard is a Python-served HTML/CSS/JavaScript application inside the
pywebview desktop shell. This check tests the current shipped UI directly,
without requiring a React migration or a live TgFarm account.

## What the browser job checks

- The dashboard entry page and its CSS/JavaScript assets load.
- The Command Center, AI Team, Publishing Queue, Connections, and Platform
  Center can be opened through the real navigation controls.
- Those screens are captured at 1440×960 and 1280×800.
- Uncaught browser runtime exceptions and failed local UI asset requests fail
  the job.
- API requests are intercepted and receive deterministic demo fixtures for the
  dashboard counters and four representative connection states; other endpoints
  receive empty responses to exercise honest empty states.
- No credentials, platform accounts, publishing actions, or external services
  are used.

The screenshots are uploaded as the \`tgfarm-ui-visual-*\` GitHub Actions artifact
and retained for 14 days. Download the artifact from the UI Visual QA run to
inspect the captured screens.

## Run locally

Install the project's development and Dzen extras, then install Chromium:

\`\`\`bash
python -m pip install -e ".[dev,dzen]"
python -m playwright install chromium
TGFARM_VISUAL_QA=1 python -m pytest tests/ui_visual -q
\`\`\`

Screenshots are written to \`artifacts/ui-visual/\`.

The test is opt-in and is skipped by the ordinary pytest suite, so a standard
headless unit-test run does not need a browser binary.

## Scope and limitations

This is a deterministic browser smoke test, not a pixel-perfect baseline
comparison. It does not claim to validate live API data, platform login,
publishing, the native pywebview window, or Windows-specific rendering.
Those require separate integration and Windows desktop checks. Empty API
fixtures are deliberate: the purpose here is to verify the real page structure,
navigation, asset loading, and browser runtime without mutating farm state.
