# Changelog

## Unreleased

- Trim eligible Android app caches each morning after stopping ImmichFrame,
  before the scheduled reboot or app-only launch. Persist completion per wake
  window and retry reported failures without consuming the reboot attempt.

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
