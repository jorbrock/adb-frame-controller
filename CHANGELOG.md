# Changelog

## Unreleased

- Exit stuck boot animations after Android reports boot completion during scheduled
  and manual reboots, before the configured boot delay and app launch.

- Make individual and all-frame Wake/Sleep controls follow each frame's HTTP
  undim/dim settings, retaining ADB app launch/stop for other action settings.

- Add a per-frame night action to dim through ImmichFrame's HTTP remote control
  while keeping the app open. The default remains stop app and dim via ADB.
- Add an HTTP undim morning action that skips reboot, app restart, and cache trim.
  HTTP commands use the frame's hostname on port 53287 and retry failures.


## v1.5.1

- Add an optional **Run as root** setting per frame, disabled by default. When
  enabled, request `adb root`, reconnect, and verify root access before scheduled
  or manual frame commands. Report elevation failures without continuing the action.
- Fix cache trimming to use `pm trim-caches 999G`. Update the command example and
  regression tests to match the corrected command.
- Expand frame setup and ADB connection troubleshooting guidance, including
  TCP/IP authorization from a VM.

### Upgrade

Build the `frame-controller:1.5.1` image and recreate the service using the updated
Compose file. Preserve the data volume to retain frame settings, credentials, ADB
keys, controller state, and log history. No settings migration is required.
Enable **Run as root** only for frames that require and support root access;
existing frames continue without requesting root by default.

## v1.5.0

- Trim eligible Android app caches each morning after stopping ImmichFrame,
  before the scheduled reboot or app-only launch. Allow up to 120 seconds for
  trimming, persist completion per wake window, and retry reported failures
  without consuming the reboot attempt or repeating completed trims on launch retries.
- Add **Reset app** beside **Reboot frame**: stop ImmichFrame, trim device caches,
  and relaunch without rebooting. Persist the background request and completed
  cache trim across controller restarts. Like Wake frame, hold the frame awake
  until the next sleep time or a manual Sleep request.
- Add **Frame management** enable/disable in settings. Disabled frames retain
  configuration and history, issue no ADB commands or status checks, and are
  excluded from manual and all-frame controls. Cancel pending manual work and
  clear manual wake/sleep holds on disable. Existing frames default to Enabled.

### Upgrade

Build the `frame-controller:1.5.0` image and recreate the service using the updated
Compose file. Preserve the data volume to retain frame settings, credentials, ADB
keys, controller state, and log history. No settings migration is required.
Morning cache trimming is automatic and affects eligible caches across apps on
the device; cached photos may need to download again on the next launch.

## v1.4.0

- Add Wake all frames and Sleep all frames controls, using existing manual override
  rules and reporting per-frame errors while continuing with other frames.
- Replace log cards with a compact table showing timestamps, results, modes, and
  details, with horizontal scrolling on narrow screens.
- Add per-frame Clear log with confirmation and CSRF protection. Clearing preserves
  the latest status, settings, and manual controls; new events continue to be recorded.
- Log Manual wake active only when entering or changing that state, including across
  restarts, while continuing to refresh the latest-status timestamp.
- Fix wake and sleep time inputs overflowing their container on iOS/mobile.
- Include `frame_log.py` in the Docker build context to fix the missing-file build error.

### Upgrade

Build the `frame-controller:1.4.0` image and recreate the service using the updated
Compose file. Preserve the data volume to retain frame settings, credentials, ADB
keys, controller state, and log history. Existing repeated log entries remain until
cleared through the log view.

## v1.3.0

- Add a View full log link for each frame in the authenticated web UI.
- Show timestamped controller results and errors, newest first, with pagination.
- Persist result history across restarts and frame renames.
- Preserve the previously saved latest result when recording begins; earlier
  results are unavailable because they were not stored.

### Upgrade

Build the `frame-controller:1.3.0` image and recreate the service using the updated
Compose file. Preserve the data volume to retain settings and history. Frame logs
are stored in `/data/<frame-name>.log.jsonl` without automatic rotation.

## v1.2.0

- Add per-frame Wake and Sleep buttons to the authenticated web UI.
- Persist manual wake overrides until the next scheduled sleep event or manual
  sleep request, suppressing periodic night commands and scheduled morning work.
- Hold manual sleep until the next scheduled wake event or manual wake request.
- Preserve overrides across controller restarts and respect them after manual reboots.
- Show active overrides and their expiry alongside manual action progress.
- Queue wake/sleep actions on frame workers with CSRF protection, duplicate-request
  handling, bounded retries, and protection against overlapping actions.
- Manage frame configurations through the web UI, saved in `/data/frames.json`.
- Configure global settings through Compose environment variables; remove the
  `config.json` dependency and inherit the host's default container logging setup.

### Upgrade

Build the `frame-controller:1.2.0` image and recreate the service using the updated
Compose file (or apply the updated TrueNAS app YAML). Preserve the data volume:
existing frame settings, credentials, ADB keys, and reboot journals remain usable.
See the README for migrating older file-based global settings to environment variables.
