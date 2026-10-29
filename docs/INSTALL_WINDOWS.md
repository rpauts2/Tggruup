# TgFarm portable build on Windows

This guide is for the Windows bundle produced by the repository's **Windows CI** job.

## 1. Download and extract the bundle

1. Open [TgFarm GitHub Actions](https://github.com/rpauts2/Tggruup/actions).
2. Open the latest successful **CI** run for the commit you want.
3. Under **Artifacts**, download `TgFarm-Windows-<commit-sha>`.
4. Extract the ZIP as a whole folder. Do not move only `TgFarm.exe`: the runtime files next to it are required.
5. Put the folder somewhere your Windows user can write to, for example `C:\\Users\\<you>\\Apps\\TgFarm`. Avoid `Program Files` unless you deliberately configure a separate writable data location.

The app stores its `data` folder next to the executable. Keep that folder and back it up: it contains the local database, API token, logs, media, browser profiles and other runtime state.

## 2. First launch safely

1. In the extracted folder, copy `.env.example` to `.env`.
2. Leave the initial settings as they are for the first launch:
   - `TGFARM_MODE=dry`: the pipeline can prepare and schedule content, but does not send posts to social platforms.
   - `LLM_PROVIDERS=offline`: uses the offline composer rather than calling paid model providers.
   - `IMAGE_ENABLED=false` and `VIDEO_ENABLED=false`: no optional image/video provider is enabled by default.
3. Start `TgFarm.exe`.
4. Review the dashboard, channels, sources, authorization cards and watchdog alerts. Use the diagnostic command from the app menu if anything is marked unhealthy.

If you need a completely network-free run, set `TGFARM_MODE=off`. Source collection and external model providers may use the network when their respective options are enabled.

## 3. Configure credentials without exposing them

- Keep `.env` local. Never commit it or paste its contents into chats or issue reports.
- Add only the credentials for platforms you intend to use. The dashboard's platform cards explain each platform's required authentication method.
- Confirm that a platform is **authorized**, **enabled**, and tested separately. A button that opens a website does not prove the farm can publish there.
- For Dzen, use the dashboard's **Войти в Дзен** action and complete the login manually in the browser window. Do not share the browser profile or its session files.
- Keep the API bound to `127.0.0.1` unless remote access is intentionally configured and secured.

## 4. Enable real publishing deliberately

Do this only after the dry run looks correct and the target account has passed a controlled test:

1. Change `TGFARM_MODE=dry` to `TGFARM_MODE=live` in `.env`.
2. Configure and verify the credentials and destination for the platform.
3. Consider `TGFARM_APPROVAL_REQUIRED=true` if you want every generated post to wait for manual approval.
4. Start the app again and make one controlled test publication. Confirm the remote post URL/ID in the dashboard before increasing volume.

Never use the **Retry** action for `publish_unknown` until you have checked the destination platform and confirmed the post is absent. A network timeout can occur after the platform accepted the post.

## 5. Updates and backups

- Download a new CI artifact into a **new folder** first. Do not overwrite a running installation.
- Stop the old app using its explicit exit action, then copy the old `data` folder into the new bundle only after taking a backup.
- Keep at least one verified backup outside the app folder or on a separate drive. The app's backup watchdog is useful, but it does not replace a tested restore.
- The CI artifact proves that tests and packaging passed on a hosted Windows runner. It does not prove that your account credentials, browser session or local policy allow a real publication.
