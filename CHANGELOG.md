# Changelog

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
