# ADB Frame Controller v1.6.0

A small Python + ADB container with a local web interface that shuts down Android
and cuts power through Wyze plugs each night, then powers on and restores ImmichFrame each morning. It operates
independently of Immich, ImmichFrame's server, and Immich Kiosk; no Immich API key
or changes to those containers are required.

This is generated source code, not a published container image. The local logic
tests use mocked ADB and Wyze calls; actual Frameo firmware and plug behavior must be tested on your devices.

### Web behavior and local login

- Lists configured frames with address, wake/sleep times, recent controller
  results, and manual action progress. Use **Add frame**, **Edit settings**, and
  **Remove** to manage up to 50 frames without restarting the controller.
- Set **Frame management** to **Disabled** in a frame's settings to preserve its
  configuration while stopping all ADB commands and device status checks. Manual
  controls are disabled and all-frame actions skip it. Pending app reset or soft reboot requests are
  cancelled and manual wake/sleep holds are cleared. The frame keeps its current
  display state. Re-enable management to resume normal operation with its existing
  schedule/reboot history. Finish any active power action before disabling management. Existing frames default to Enabled.
- Frame forms cover name, ADB address, Wyze plug MAC and optional Run as root, app package/activity, wake/sleep times,
  day brightness, and boot delay.
  Removal requires a confirmation page. Empty configurations are supported.
- Settings are validated before saving. Busy frames and active manual actions
  reject edits/removal; stale forms cannot overwrite newer changes.
- **Reset app**, beside **Reboot frame**, stops ImmichFrame, trims eligible caches
  across the device, and relaunches ImmichFrame with day brightness. Settings are
  retained; cached photos may need to download again. Like Wake frame, it holds
  the frame awake until the next sleep time (or a manual Sleep request), and works
  with scheduling disabled. Launch retries do not repeat a completed cache trim;
  a new reset request does. The action uses the same 15-minute retry limit and
  busy protection as other manual controls.
- **Wake frame** turns on the Wyze plug, waits the boot delay, then restores brightness
  and starts ImmichFrame if it is not already running. **Sleep frame** attempts
  Android shutdown before switching off the plug.
  Manual overrides and their expiry are shown on each card; see below for scheduling behavior.
- Manual actions run independently per frame and are serialized with scheduled
  work. Manual reboot explicitly relaunches ImmichFrame after Android boots and the delay
  expires. It respects the active manual override; otherwise it follows the schedule.
- Repeated submissions of the most recent request are ignored. Busy frames reject new
  requests, and there is a two-minute cooldown between manual reboot requests.
  A frame briefly busy doing scheduled work may ask you to try again shortly.
- Manual jobs persist across container restarts and normally have a 15-minute timeout.
  Hard reboot keeps retrying power restoration until it succeeds; the Android boot
  timeout starts after power is restored.
  Wake makes at most five ADB/startup attempts, 30 seconds apart after the boot delay.
  Pending wakes are cancelled when their schedule boundary passes. A shutdown
  already in progress finishes cutting power before the next wake is processed.
  The actual reboot command is sent at most once per request. A successful manual
  reboot that launches the app also fulfills the current schedule event.
- A failure is shown in the frame's card. You can request another action after
  the pending job finishes or times out. For soft reboot, an unchanged boot ID is never considered
  a successful reboot. These statuses do not detect stalled photo progression.
- One username/password account, with a salted password hash stored in
  `/data/web-auth.json`. Sessions expire after 12 hours and are invalidated when
  the container restarts or the password changes. Run `set-password` again to
  change/reset the account; no restart is needed for that change.
- Login, manual action, and configuration forms use CSRF tokens; changes require POST.
  Cookies are HttpOnly and SameSite=Strict. Failed logins are rate limited.
  The UI uses only bundled CSS/JavaScript, with no CDN or external services.
- Direct LAN HTTP is supported. HTTP does not encrypt the password or session in
  transit; use a unique local password. If you later use your local reverse proxy
  with HTTPS, set `WEB_SECURE_COOKIE` to `"true"` and recreate the container. Keep it false
  for direct HTTP. Preserve the existing subnet restrictions.

### Manual wake and sleep

A manual wake holds the frame awake until **Sleep frame** is clicked or the next
scheduled sleep event arrives. For example, with sleep set to 22:00, waking a
frame at 23:00 keeps it awake until 22:00 the next day. During this hold the
controller leaves the frame powered on through the next morning wake event.
A wake before midnight or after midnight uses the next sleep time in the frame's
timezone; overnight schedules work the same way.

Manual sleep holds night mode until **Wake frame** is clicked or the next
scheduled wake event arrives. The plug stays off, with no ADB polling or night
rechecks. The next wake restores power even across controller restarts.

Overrides are saved before commands are sent and survive controller restarts.
Rebooting a frame preserves its override and restores the appropriate mode.
When scheduling is paused, overrides remain until manually changed. When scheduling
is enabled again, any override whose saved expiry has passed is cleared.
Changing a frame's settings does not change an existing override's saved expiry;
issue a new manual action to replace it. Retrying the same submitted form does
not extend the hold. An override records the requested mode; check the action
result for connection or command failures.

### Wyze plug power and hard reboot

Pair each smart plug with your account in the Wyze app first. In **Edit settings**,
enter its **Plug device MAC** (`wyze_mac` in `frames.json`). Use the plug's MAC,
not the frame's network MAC. Both 12- and 16-digit hexadecimal device MACs are
accepted; colons, dashes and letter case are normalized. A plug can be assigned
to only one frame. Wake and sleep require a paired plug and configured Wyze
credentials. Unpaired frames retain ADB reset/reboot controls; scheduled wake or
sleep reports a pairing error without sending device commands.

Click the frame’s **Reboot** button to open a menu with **Soft reboot** and **Hard reboot**,
then confirm the selected action. The menu supports arrow keys and Escape.
Soft reboot restarts Android through ADB. Hard reboot uses the same graceful
shutdown sequence as Sleep: try Android shutdown up to three times, with direct
plug fallback if ADB is unavailable or shutdown fails. An accepted shutdown gets
30 seconds to finish before power is cut. The plug then stays off for at least
30 seconds, measured from the off command's completion, before power is restored.
The worker checks every five seconds, so each wait may take a few seconds longer.
Both reboot modes wait for Android boot and the configured boot delay, then restore
the scheduled display mode or active manual override. Scheduled wakes use the
plug-based wake sequence below.

**Power off** switches off the plug, clears manual wake/sleep holds, and pauses
automatic frame monitoring and scheduled work until **Power on** or **Wake**.
All-frame Wake/Sleep includes powered-off frames and skips disabled frames.
This explicit Power off pause survives container restarts and schedule boundaries;
ordinary **Sleep** still allows the next scheduled wake. **Power on** restores plug power, waits for Android, and applies the
current schedule (or launches the slideshow when scheduling is paused), without
sending another reboot. Frame management must be enabled to use power controls.

Power state shown in the UI is the last controller command, not a live electrical
measurement. The controller does not poll Wyze while a frame is powered off or
track changes made using the Wyze app, plug button, or Wyze schedules. Use these
controller controls to keep the pause state synchronized; select **Power on** here
or **Wake** after restoring power elsewhere. A failed/ambiguous off request is shown as
unknown power and also pauses device commands until power is restored.

Hard reboot's power restoration is journaled before switching off. If the
controller restarts during the wait, it allows a fresh 30 seconds before restoring
power and does not send another off command. If the off request fails or its
response is lost, the controller still attempts to restore power, then reports
that the hard reboot was not confirmed. Failed power-on attempts during hard
reboot continue retrying, even beyond the normal job timeout. Other actions and
settings changes remain blocked while the cycle is active. The controller must
be running and Wyze reachable to restore power; an outage can extend the off time.

The integration uses [wyze-sdk](https://github.com/shauntarves/wyze-sdk) and requires
internet access to Wyze. Set credentials in the `environment:` block of
`docker-compose.yaml`, or supply the included substitutions through Compose's
`.env` file:

| Variable | Purpose |
| --- | --- |
| `WYZE_EMAIL` | Wyze account email. |
| `WYZE_PASSWORD` | Wyze account password. |
| `WYZE_KEY_ID` | API key ID from the [Wyze developer portal](https://developer-api-console.wyze.com/#/apikey/view). |
| `WYZE_API_KEY` | API key from the same portal. |
| `WYZE_TOTP_KEY` | Optional authenticator setup secret for accounts using TOTP MFA. Interactive SMS/email MFA is unsupported. |
| `WYZE_ACCESS_TOKEN` | Alternative to email/password/API credentials; takes precedence when set. |
| `WYZE_REFRESH_TOKEN` | Refresh token accompanying the access token, required for automatic renewal in token mode. |

The SDK session is shared across frames and reused; expired access tokens are
refreshed. Login errors back off for one minute. Credentials and tokens remain
in the environment and SDK memory, not frame settings or frame logs. Without
Wyze credentials, ordinary ADB operation remains available. Rebuild the image
with the updated requirements and recreate the container after configuring the
variables. No frame settings migration is required.

### Saving frame settings

Frame settings are stored only in `/data/frames.json` (under `DATA_DIR` when
configured). If that file is missing, the controller starts with no frames;
use **Add frame** in the web UI to create the first one. Every successful UI
change saves the complete list, which is loaded again on restart. No `config.json`
file or `/config` mount is needed. Back up the data directory and your Compose
file. Invalid saved settings fail validation instead of being discarded.

**Run as root** is optional for each frame and defaults to off (`run_as_root: false`
in `frames.json`). Enable it for frames that require and support `adb root`. Before
manual or scheduled commands, the controller requests root, waits for ADB to
reconnect, and verifies root access. If elevation fails, the action reports an
error; Sleep falls back to cutting plug power if ADB/root is unavailable. Leave it unchecked for frames that use normal ADB
permissions. Unchecking it stops requesting root; it does not run `adb unroot`.

Changes apply to live workers. Adding a frame while scheduling is enabled can
immediately wake or shut down the frame. Edits preserve schedule and reboot
history, including when renaming a frame: a completed schedule event does not
run again just because settings changed. App and brightness changes take effect
on the next wake or app launch; schedule changes are evaluated on the next worker tick.
An active manual action must finish, expire, or time out before editing or removal.
Removing a frame stops future commands without changing its current display.
Its state/status files are retained; adding it again under the same name restores
its history. Use the same name for the same physical frame.

To edit frame settings manually, stop the controller, back up and edit the JSON
array in `/data/frames.json`, then start the controller. Do not edit saved settings
files while it is running.

### Environment settings

Set global options in the service's `environment:` section in `compose.yaml`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCHEDULE_ENABLED` | `"false"` | Enable scheduled wake/sleep actions. Manual wake, sleep, and reboot remain available when false. |
| `TZ` | `UTC` | IANA timezone for frame schedules; the sample Compose file uses `America/Los_Angeles`. |
| `WEB_ENABLED` | `"true"` | Serve the authenticated web UI on port 8080. |
| `WEB_SECURE_COOKIE` | `"false"` | Restrict session cookies to HTTPS; enable when using an HTTPS reverse proxy. |
| `DATA_DIR` | `/data` | Directory containing frame settings, reboot history, and web credentials. Keep `/data` with the provided volume mount. |

Quote boolean values in YAML as shown. The application accepts `true` and `false`
(case-insensitive); invalid values or timezone names fail validation at startup.
After changing environment settings, apply the updated app YAML in TrueNAS or run
`docker compose up -d` on a Compose host to recreate the container. A plain
container restart does not pick up changes to Compose environment variables.

When upgrading, replace the old global JSON options with `SCHEDULE_ENABLED`, `TZ`,
`WEB_ENABLED`, and `WEB_SECURE_COOKIE`, and remove the `/config` volume mount.
The old `CONFIG` variable and `config.json` files are ignored. Existing
`/data/frames.json` settings continue to work. If frames were configured only in
the old config file, move that `frames` array into `/data/frames.json` while the
controller is stopped, or add the frames through the web UI.

## Behavior

- Each frame has an independent worker and local wake/sleep times in the configured timezone.
- Sleep first connects over ADB and attempts `svc power shutdown`, falling back to
  `reboot -p` if the first command fails. Failed command pairs are tried at most
  three times. If ADB is unavailable or all three attempts fail, the controller
  forces power off through Wyze. When a command is accepted, it allows 30 seconds
  for shutdown before switching off the plug. Wyze failures remain visible and
  retry within the job timeout.
- Wake turns on the Wyze plug, waits `boot_delay_seconds` (default 60, range 0–600),
  then connects over ADB. There are five total connection/startup attempts, spaced
  30 seconds apart. After the third failure an error appears in the UI, frame
  history, and service log; attempts four and five still run. A fifth failure ends
  the job and remains visible until another action. Wake never sends an ADB reboot.
- Once connected, wake sends `setprop service.bootanim.exit 1`, sets manual
  brightness to `day_brightness` (default 128, range 1–255), and checks the configured
  app's main process with `pidof`. It launches the configured activity only if the
  app is absent. ImmichFrame can be the Home app; an automatically started instance
  is preserved. Routine wakes do not force-stop the app or trim caches.
- Boot delays, retries, shutdown progress, overrides, and schedule events persist
  across controller restarts. Successful sleep leaves scheduling active so the
  next wake can turn on an off frame. A completed or failed event is not repeatedly
  issued during the same schedule window. Use Wake again for a manual retry.
- The scheduler catches up after downtime: daytime starts trigger wake and nighttime
  starts trigger sleep. Manual overrides defer these events until their saved boundary.
  Overnight schedules and daylight-saving boundaries are supported.
- `morning_action`, `night_action`, and `night_recheck_seconds` are retired. Existing
  values are ignored when loading settings and removed on the next UI settings save.
  HTTP dim/undim and the old scheduled reboot/cache-trim paths have been removed.
- JSON status files, per-frame history, and console logs report actions and errors.
  The Docker heartbeat checks the controller process, not physical photo progression.
- The container needs LAN access to each frame's ADB port and internet access to Wyze.
  It does not require privileged mode, USB access, a Docker socket, or host networking.

Android's shutdown commands are implemented in the AOSP
[`svc power` source](https://android.googlesource.com/platform/frameworks/base/+/0ef403e/cmds/svc/src/com/android/commands/svc/PowerCommand.java)
and [`reboot -p` source](https://chromium.googlesource.com/aosp/platform/system/core/+/master/reboot/reboot.c).
Firmware permissions and physical shutdown behavior still need verification on each model.

## 1. Prove these prerequisites on ONE frame

Pair its Wyze plug, configure the controller's Wyze credentials, and reserve the
frame's IP address. Confirm the frame starts automatically when the plug restores
power and that wireless ADB stays enabled and authorized after a full power cycle.
`adb tcpip 5555` can be temporary on some firmware; resolve that before enabling
scheduled wake/sleep. The controller cannot reconnect to a disabled ADB service.

Use the frame's actual address and installed package/activity. With the plug on
and Android booted, verify:

```bash
adb connect 192.168.30.200:5555
adb -s 192.168.30.200:5555 shell setprop service.bootanim.exit 1
adb -s 192.168.30.200:5555 shell settings put system screen_brightness_mode 0
adb -s 192.168.30.200:5555 shell settings put system screen_brightness 128
adb -s 192.168.30.200:5555 shell pidof com.immichframe.immichframe
```

Choose a suitable daytime brightness. If ImmichFrame is not already running, test:

```bash
adb -s 192.168.30.200:5555 shell am start -W -n com.immichframe.immichframe/.MainActivity
```

The launch should report `Status: ok`. ImmichFrame may be the default Home app,
or you may use a separate launcher such as Discreet Launcher. Disable conflicting
Android sleep, vendor power schedules, and app schedules during the wake window.

With scheduling paused, test **Sleep frame** and confirm Android shuts down and
the plug switches off. Then use **Wake frame** and verify automatic boot, ADB
connection, the chosen brightness, and advancing photos. Increase the boot delay
if the frame needs longer before ADB is ready. Observe a full scheduled night and
morning cycle before enabling the remaining frames.

## 2. Prepare TrueNAS storage and build

Use a Docker-based TrueNAS release. Replace `tank` with your pool name throughout.
Create a dataset/directory for the project, extract this archive into it, and open
a TrueNAS administrative shell in the extracted `frame-controller` directory.
These commands require appropriate host permissions:

```bash
mkdir -p /mnt/tank/apps/frame-controller/data
chown 3019:3019 /mnt/tank/apps/frame-controller/data
chmod 700 /mnt/tank/apps/frame-controller/data
docker build -t frame-controller:1.6.0 .
```

The image build requires internet access for the Python base image, Debian ADB
packages, and the Flask/Waitress Python dependencies. If your dataset uses ACLs,
grant UID/GID 3019 read/write access to data using the TrueNAS ACL editor.
Preserve the data directory:
it contains ADB private keys under `.android`, saved frame settings, login credentials,
and reboot history. Restrict its access.

Set `TZ` under `environment:` in `compose.yaml` to your timezone; the sample is
`America/Los_Angeles`. Leave `SCHEDULE_ENABLED: "false"` initially. After setting
up the web login, use **Add frame** to configure your frame IPs and schedules.
Names and addresses must be unique. Edit the data bind-mount path in `compose.yaml`
to match your actual dataset.
Also replace the example TrueNAS IP in the web port mapping with your LAN IP.

## 3. Install as a TrueNAS app

Go to **Apps → Discover Apps → menu → Install via YAML**, name the app
`frame-controller`, and paste `compose.yaml` into the editor. The image must already
have been built on this TrueNAS host. `pull_policy: never` selects that local image.
This makes the service visible in TrueNAS Apps for logs, stop/start, and status.
Do not also start the same service with `docker compose up` if TrueNAS manages it.

The local image is not automatically updated or backed up with the data dataset.
Keep this source and rebuild after migrating hosts or losing/pruning the image.
For updates, build a new image tag and change the app YAML to use that tag.

Alternatively, on a regular Docker Compose host, after adjusting paths and building:

```bash
docker compose up -d
docker compose logs -f frame-controller
docker compose exec frame-controller python /app/controller.py status
```

## 4. Authorize the container's ADB key

Open the running container's shell in TrueNAS Apps and run:

```bash
adb connect 192.168.40.61:5555
adb devices -l
```

Accept the debugging prompt on that frame, selecting "Always allow" if offered.
Repeat for each frame. Authorizing your laptop does not authorize the container:
it has its own key, retained in the data volume. Some Frameo builds omit this prompt.
No USB passthrough is needed when network ADB is already available.

If you use Android 11+ paired wireless debugging instead of a fixed TCP ADB port,
pair from this shell with `adb pair IP:PAIRING_PORT`, then configure the actual
connection port. The pairing port is different, and changing connection ports
require configuration updates. This project is primarily for fixed-port TCP ADB.

Still in the container shell, validate configuration:

```bash
python /app/controller.py validate
```

Set the local web password in the container shell:

```bash
python /app/webui.py set-password --username admin
```

Sign in to the web UI and add your frames. After the single-frame tests pass,
set `SCHEDULE_ENABLED: "true"` in the app YAML and apply it to recreate the
container. On a Compose host, use `docker compose up -d`. Remember: starting during
daytime triggers a catch-up reboot. You can stagger wake times to spread load on
the photo server.

### Application file permissions

The image runs as UID/GID `3019:3019`, matching `user:` in Compose. The Dockerfile
sets application directories to `755` and application files to `644`, then checks
that the runtime user can import the application. This avoids inheriting
restrictive source permissions from a NAS dataset.

If an older image reports `python: can't open file '/app/controller.py':
[Errno 13] Permission denied`, rebuild it with the updated Dockerfile:

```bash
docker build --no-cache -t frame-controller:1.6.0 .
```

Redeploy/recreate the TrueNAS app using that rebuilt image. On a regular Compose
host, use `docker compose up -d --force-recreate`. Restarting an existing container
does not replace its image. You can check the rebuilt image without starting any
frame workers or mounting the data volume:

```bash
docker run --rm --user 3019:3019 --entrypoint python frame-controller:1.6.0 -c "import controller, webui; print('Application readable')"
```

The host data dataset separately needs read/write access for UID/GID `3019:3019`,
including existing state files and ADB keys. Adjust its TrueNAS ACLs if needed.
If choosing another UID/GID, update the Dockerfile, Compose user, and data dataset
permissions together, then rebuild and recreate the container.

## Monitoring and recovery

In the TrueNAS container shell:

```bash
python /app/controller.py status
adb devices -l
```

Logs record shutdowns, plug commands, wake progress, reboot requests, and errors.
Status shows each frame's latest result and timestamp. In the web UI, select
**View full log** below a frame's latest result to browse its recorded results,
timestamps, and errors, newest first (50 per page). **Refresh latest** loads new
activity without interrupting you while you read older entries. The compact table
scrolls horizontally on narrow screens. **Clear log** opens a confirmation page
to permanently remove that frame's history while preserving its latest status and
settings; new events continue to be recorded. Logs require sign-in.
History is persisted in `/data/<frame-name>.log.jsonl` and follows UI frame renames.
Recording starts with this feature and preserves the previously saved latest result;
earlier results cannot be recovered. Log files are retained without automatic rotation,
so their disk usage grows over time. This is controller result history, not Android logcat.

Docker health only confirms
the scheduler is running; it does not mean every frame is reachable or advancing
photos. Docker also does not restart a container merely because it is unhealthy.
Wake failures have a five-attempt limit; process exit uses the restart policy.

While a managed frame should be awake, the controller polls it over ADB every five
minutes, including during manual wake holds and with scheduling disabled. It checks
the main ImmichFrame process, current Android crash/not-responding flags, and the
focused window. Monitoring skips disabled frames, sleep windows/holds, powered-off
or unknown-power frames, and frames with another operation in progress. The first
check is five minutes after startup or a completed operation.

A missing, crashing, non-responsive, or backgrounded app triggers force-stop,
the same full device cache trim as **Reset app** (`pm trim-caches 999G`, retaining
settings), and relaunch. After 30 seconds the controller verifies process health
and foreground focus again. If recovery fails but ADB still works, it issues one
soft reboot. If ADB is unavailable, it uses the paired Wyze plug for a hard reboot
with 30 seconds off. An unreachable soft-rebooted frame also falls back to Wyze
after five boot connection attempts spaced 30 seconds apart. Both reboot paths
respect the configured boot delay, clear the boot animation, restore brightness,
and use the scheduled wake startup routine, bringing ImmichFrame to the foreground
and verifying it after 30 seconds.

Recovery is journaled across controller restarts, serialized with other frame
operations, and cancelled when sleep becomes due. An interrupted plug cycle always
restores power before handing control back to the schedule. Power restoration is
retried until it succeeds; other reboot recovery failures stop after five attempts
and wait until the next five-minute poll. A reachable, paired Wyze plug is required
for hard recovery. Progress and failures appear in the frame status and log.

These checks detect Android-reported non-responsiveness, not a slideshow that
silently stops advancing while its process and foreground window remain healthy.
That requires an ImmichFrame heartbeat/progression signal. Unsupported Android
diagnostic output is logged as a monitoring error rather than assumed healthy or
used to trigger a reboot when basic ADB commands still work. Do not delete state
to fix connection failures.

Use **Wake frame** to turn on power and restore the display, or **Sleep frame** to
shut down Android and cut plug power. **Wake all frames** and **Sleep all frames**
request the same sequence for every enabled frame, including off frames. Busy or
unpaired frames report individual errors while other frames still receive requests.
Manual wake holds until the next sleep event; manual sleep holds until the next wake.

Use DHCP reservations. On UniFi, permit the TrueNAS container's effective source
IP (normally the TrueNAS LAN IP with bridge networking) to reach only the frames'
ADB TCP ports. Keep ADB LAN-only and block access from untrusted networks; do not
publish it through your router or Cloudflare. Only the web UI port needs inbound access from your trusted devices. ADB port
5555 remains outbound from the container to the frames. Frames retain their
existing access to the photo server.

## Develop in a VS Code Dev Container

Install Docker and the VS Code **Dev Containers** extension, start Docker, then
open this repository and run **Dev Containers: Reopen in Container** from the
Command Palette. The first build needs internet access to download the base
image, Debian packages, Python dependencies, and editor extensions.

The development image uses Python 3.12 on Debian Bookworm, matching production,
with ADB, Git, globally installed Python dependencies, and a non-root `vscode` user.
VS Code includes Python debugging and unittest discovery, and forwards port 8080.
The configuration follows the [VS Code Dev Containers workflow](https://code.visualstudio.com/docs/devcontainers/create-dev-container).

The image build installs `requirements.txt` globally. Development environment
settings live in `containerEnv` in `.devcontainer/devcontainer.json`; scheduling
starts disabled and the web UI is enabled. Rebuild the dev container after changing
these values. For a temporary terminal session, export a setting before starting
the controller, for example `export TZ=Europe/London`. Manual web actions still
control the configured devices. The controller does not start automatically when
you open the container.

From the container terminal:

```bash
python -m unittest -v
python controller.py validate
python webui.py set-password --username admin
python controller.py run
```

Open `http://localhost:8080` once the controller is running, or use the forwarded
address in VS Code's Ports panel. For breakpoints, select **Frame Controller**
in Run and Debug and press F5 instead of starting `controller.py run` manually.
Frame settings saved through the web UI apply live. Stop and restart the controller
after changing global configuration or Python code.

Development frame settings, state, login credentials, and ADB keys are kept in
the Git-ignored `.devcontainer/local/` directory and survive container rebuilds.
The container's `~/.android` links to that directory's `data/.android` folder.
For real-device testing, the container needs LAN access to the frames; use
`adb connect IP:PORT` and authorize its development key on each device.

After changing requirements or the development Dockerfile, use
**Dev Containers: Rebuild Container** to install dependencies into the image.
If VS Code retained the previous virtual environment selection, run
**Python: Select Interpreter** and choose `/usr/local/bin/python`.
The production Dockerfile and TrueNAS Compose configuration are separate; run
production `docker build` and `docker compose` commands from a host terminal.

## Tests

```bash
python -m pip install -r requirements.txt
python -m unittest -v
```

Tests use mocked ADB and Wyze calls to cover shutdown retries and fallback,
boot-delay timing, five-attempt wake limits, third-failure errors, home-app detection,
schedule and override boundaries, restart recovery, and plug failure handling.
They also cover manual reboot/reset, authentication, CSRF, settings validation,
configuration persistence, concurrent requests, and frame history. They do not
validate a Docker build or real frame firmware.

## References

- [Fix unauthorized VM-to-frame TCP/IP ADB using USB](docs/vm_adb_tcpip_authorization.md)

- Android ADB and activity-manager commands: https://developer.android.com/tools/adb
- Wakeup key semantics: https://developer.android.com/reference/android/view/KeyEvent
- ImmichFrame Android/Frameo instructions: https://github.com/immichFrame/ImmichFrame/blob/main/docs/docs/getting-started/apps.md
- Frameo network ADB notes: https://docs.immichkiosk.app/misc/frameo/
- TrueNAS custom applications: https://apps.truenas.com/managing-apps/installing-custom-apps/

- Flask security guidance: https://flask.palletsprojects.com/en/stable/web-security/
- Waitress deployment: https://flask.palletsprojects.com/en/stable/deploying/waitress/
