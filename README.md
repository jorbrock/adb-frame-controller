# ADB Frame Controller v1.5.1

A small Python + ADB container with a local web interface that stops ImmichFrame and sets screen brightness to zero
at night, then reboots and explicitly launches the app each morning. It operates
independently of Immich, ImmichFrame's server, and Immich Kiosk; no Immich API key
or changes to those containers are required.

This is generated source code, not a published container image. The local logic
tests use mocked ADB; actual Frameo firmware behavior must be tested on your devices.

### Web behavior and local login

- Lists configured frames with address, wake/sleep times, recent controller
  results, and manual action progress. Use **Add frame**, **Edit settings**, and
  **Remove** to manage up to 50 frames without restarting the controller.
- Set **Frame management** to **Disabled** in a frame's settings to preserve its
  configuration while stopping all ADB commands and device status checks. Manual
  controls are disabled and all-frame actions skip it. Pending manual requests are
  cancelled and manual wake/sleep holds are cleared. The frame keeps its current
  display state. Re-enable management to resume normal operation with its existing
  morning/reboot history. Existing frames default to Enabled.
- Frame forms cover name, ADB address, optional Run as root, app package/activity, wake/sleep times,
  morning action, day brightness, boot delay, and night recheck interval.
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
- **Wake frame** restores day brightness and launches the app without rebooting.
  **Sleep frame** stops the app and sets brightness to zero while keeping ADB reachable.
  Manual overrides and their expiry are shown on each card; see below for scheduling behavior.
- Manual actions run independently per frame and are serialized with scheduled
  work. Manual reboot explicitly relaunches ImmichFrame after Android boots and the delay
  expires. It respects the active manual override; otherwise it follows the schedule.
- Repeated submissions of the most recent request are ignored. Busy frames reject new
  requests, and there is a two-minute cooldown between manual reboot requests.
  A frame briefly busy doing scheduled work may ask you to try again shortly.
- Manual jobs persist across container restarts and have a 15-minute timeout.
  Wake/sleep connections and commands also retry within this timeout; pending
  wake/sleep requests are cancelled if their schedule boundary passes first.
  The actual reboot command is sent at most once per request. A successful manual
  reboot that launches the app also fulfills the current morning window.
- A failure is shown in the frame's card. You can request another action after
  the pending job finishes or times out. An unchanged boot ID is never considered
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
controller does not send the periodic night commands or run a morning reboot.
A wake before midnight or after midnight uses the next sleep time in the frame's
timezone; overnight schedules work the same way.

Manual sleep holds night mode until **Wake frame** is clicked or the next
scheduled wake event arrives. Night commands continue at the configured recheck
interval while held asleep. Sleep uses brightness zero and app force-stop, not
Android's network-disabling sleep key.

Overrides are saved before commands are sent and survive controller restarts.
Rebooting a frame preserves its override and restores the appropriate mode.
When scheduling is paused, overrides remain until manually changed. When scheduling
is enabled again, any override whose saved expiry has passed is cleared.
Changing a frame's settings does not change an existing override's saved expiry;
issue a new manual action to replace it. Retrying the same submitted form does
not extend the hold. An override records the requested mode; check the action
result for connection or command failures.

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
error without continuing. Leave it unchecked for frames that use normal ADB
permissions. Unchecking it stops requesting root; it does not run `adb unroot`.

Changes apply to live workers. Adding a frame while scheduling is enabled can
immediately start its morning sequence or apply night mode. Edits preserve reboot
history, including when renaming a frame: a completed morning sequence does not
run again just because settings changed. App and brightness changes take effect
on the next app launch; schedule changes are evaluated on the next worker tick.
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

- Independent worker per frame; one unreachable device does not block the others.
- Per-frame local wake/sleep times, using an IANA timezone with daylight saving time.
- By default at night (`night_action: stop_app`): `am force-stop PACKAGE`, set `screen_brightness_mode` to `0`
  (manual), then set `screen_brightness` to `0`. Android stays awake for network ADB.
  Repeats every five minutes by default to handle incidental app starts or brightness changes.
- Select **Dim via ImmichFrame HTTP** (`night_action: dim`) to keep ImmichFrame
  open and send `GET http://HOST:53287/dim` at night, including night rechecks and
  manual Sleep. HOST comes from the frame's ADB address.
- Select **Undim via ImmichFrame HTTP** (`morning_action: undim`) to send
  `GET http://HOST:53287/undim` once per wake window, without ADB, cache trimming,
  boot delay, or app restart. Pair this with HTTP dim: undim cannot start a stopped
  app. ImmichFrame must already be running and port 53287 reachable from the
  controller. Failed HTTP requests retry; completion persists across restarts.
  These endpoints are documented in [ImmichFrame's remote control guide](https://immichframe.dev/docs/getting-started/apps).
  Individual and all-frame Wake/Sleep buttons use each frame's morning/night
  HTTP option when selected; otherwise they launch/stop the app via ADB as before.
  Reset app and Reboot retain their existing behavior. A manual reboot at night launches ImmichFrame before applying HTTP dim.
- In reboot or restart-app morning mode, force-stop ImmichFrame and run
  `pm trim-caches 999G` before the reboot, or before app launch in
  `restart_app` mode. This requests a full trim of eligible Android caches across
  apps; cached photos may need to download again. Allow up to 120 seconds for the
  command. Reported failures retry; a completed trim is persisted per wake window
  so launch retries and controller restarts do not repeat it. Manual Reset app also runs this cleanup on demand.
- In reboot mode: one reboot attempt per wake window, reconnect, confirm the
  kernel boot ID changed, wait for `sys.boot_completed=1`, allow an additional
  60 seconds, wake the screen with keycode 224, set manual brightness to
  `day_brightness` (default `128`, configurable per frame from `1` to `255`),
  then explicitly launch the app. Daytime manual reboots restore this brightness too.
- Persisted state prevents another reboot in the same wake window after a
  container restart. A failed/ambiguous reboot is not automatically repeated.
  Connection and launch failures retry every 30 seconds.
- The scheduler catches up after downtime. Enabling or first starting it during
  the day immediately begins that day's configured morning action.
  Starting it at night immediately applies night mode. UI schedule changes apply live.
- Spring DST gaps take effect at the first available time after the scheduled
  boundary. Repeated fall hours share one wake-window date and do not add a reboot.
- JSON status files, console logs (stdout/stderr), and a Docker scheduler heartbeat health check.
  Compose inherits the host's default logging driver, allowing your existing
  container log collector to collect the console output.
- Non-root container, no privileged mode, Docker socket, USB access, or host networking
  required. Port 8080 serves the optional web UI. LAN routing to each frame is required.

## 1. Prove these prerequisites on ONE frame

Use its reserved IP and actual ADB port. Commands below assume `192.168.30.200:5555`.

Install [Discreet Launcher](https://github.com/falzonv/discreet-launcher) on
**each frame** and make it the default Home app before enabling the schedule.
Download its APK from the project's [releases](https://github.com/falzonv/discreet-launcher/releases),
then install the downloaded file (replace the local path below):

```bash
adb connect 192.168.30.200:5555
adb -s 192.168.30.200:5555 install -r /path/to/discreet-launcher.apk
adb -s 192.168.30.200:5555 shell am start -a android.settings.HOME_SETTINGS
```

On the frame, select **Discreet Launcher** as the default Home app. If that settings
screen is unavailable, open Android Settings → Apps → Default apps → Home app,
or press Home and select Discreet Launcher with **Always** when prompted.
Configure a black wallpaper and confirm Home shows Discreet Launcher.
ImmichFrame must not be the default Home app: Android may otherwise relaunch it
when the controller force-stops it.

Read the current brightness and choose a daytime value for `day_brightness` in
that frame's configuration. The controller uses manual brightness day and night;
it does not restore adaptive brightness or automatically save the previous value.

```bash
adb -s 192.168.30.200:5555 shell pm list packages
adb -s 192.168.30.200:5555 shell settings get system screen_brightness
adb -s 192.168.30.200:5555 shell am force-stop com.immichframe.immichframe
adb -s 192.168.30.200:5555 shell settings put system screen_brightness_mode 0
adb -s 192.168.30.200:5555 shell settings put system screen_brightness 0
```

Confirm the display is dark and ImmichFrame stays stopped. Leave it in this state
for a meaningful interval (ideally overnight), then reconnect and test daytime
brightness/start. Replace `128` with your chosen daytime brightness:

```bash
adb connect 192.168.30.200:5555
adb -s 192.168.30.200:5555 shell input keyevent 224
adb -s 192.168.30.200:5555 shell settings put system screen_brightness_mode 0
adb -s 192.168.30.200:5555 shell settings put system screen_brightness 128
adb -s 192.168.30.200:5555 shell am start -W -n com.immichframe.immichframe/.MainActivity
```

Test a reboot, reconnect after the frame has booted, then check:

```bash
adb -s 192.168.30.200:5555 reboot
# Wait for the device to boot before the next commands.
adb connect 192.168.30.200:5555
adb -s 192.168.30.200:5555 shell getprop sys.boot_completed
adb -s 192.168.30.200:5555 shell cat /proc/sys/kernel/random/boot_id
adb -s 192.168.30.200:5555 shell settings put system screen_brightness_mode 0
adb -s 192.168.30.200:5555 shell settings put system screen_brightness 128
adb -s 192.168.30.200:5555 shell am start -W -n com.immichframe.immichframe/.MainActivity
```

`sys.boot_completed` must return `1`, and the activity start should report
`Status: ok`. The package/activity above are the ones documented by ImmichFrame;
change the configuration if your APK uses different names.

**Wireless ADB must survive reboot.** Enabling TCP ADB with `adb tcpip 5555` can
be temporary. Frameo persistence varies by firmware; some require USB again
after every reboot. This container cannot reconnect to a disabled ADB service.
Do not enable scheduled reboot until you have verified persistence. You can use
`"morning_action": "restart_app"` instead, which wakes and restarts the app without
rebooting, but ADB must still remain reachable overnight.

The controller does not send keycode 223 (SLEEP): on some frame firmware it also
makes network ADB unreachable until a physical reboot. Brightness zero works on
the tested frames, but other firmware may clamp it to a visible minimum. Confirm
the physical result on every model; a black launcher alone does not turn off the
backlight. Keep Android's automatic sleep/screensaver disabled in the device
settings so it does not independently put the frame to sleep overnight.

Observe one full night/morning cycle; disable conflicting app schedules or kiosk
tools where appropriate. The controller sends commands but does not assert the
physical display is off.

## 2. Prepare TrueNAS storage and build

Use a Docker-based TrueNAS release. Replace `tank` with your pool name throughout.
Create a dataset/directory for the project, extract this archive into it, and open
a TrueNAS administrative shell in the extracted `frame-controller` directory.
These commands require appropriate host permissions:

```bash
mkdir -p /mnt/tank/apps/frame-controller/data
chown 3019:3019 /mnt/tank/apps/frame-controller/data
chmod 700 /mnt/tank/apps/frame-controller/data
docker build -t frame-controller:1.5.1 .
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
docker build --no-cache -t frame-controller:1.5.1 .
```

Redeploy/recreate the TrueNAS app using that rebuilt image. On a regular Compose
host, use `docker compose up -d --force-recreate`. Restarting an existing container
does not replace its image. You can check the rebuilt image without starting any
frame workers or mounting the data volume:

```bash
docker run --rm --user 3019:3019 --entrypoint python frame-controller:1.5.1 -c "import controller, webui; print('Application readable')"
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

Logs record night-mode commands, reboot requests, confirmed launches, and errors.
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
Frame errors are retried by the scheduler; process exit uses the restart policy.

The controller does not monitor daytime slideshow progression. Daily reboot is
preventive maintenance, not freeze detection. It cannot recover a device whose
ADB service, Wi-Fi, or kernel has stopped responding; that needs physical power
cycling or a separate controllable power outlet.

If the reboot command was lost, status continues reporting an unchanged boot ID.
There is deliberately no second automated reboot that wake window. Reboot that
one frame manually with `adb -s IP:PORT reboot`; once its boot ID changes, the
controller can finish startup. Do not delete state to fix connection failures.

To reboot a frame from your browser, sign in and use its Reboot frame button.
Use **Wake frame** to launch the app and restore brightness without rebooting,
or **Sleep frame** to stop the app and dim the display. Use **Wake all frames** or
**Sleep all frames** above the frame cards to request the same action for every
configured frame. Busy frames are skipped and any errors are shown by frame; other
frames still receive the request. Global actions follow the same override rules. Manual wake pauses night
rechecks until the next scheduled sleep time or a manual sleep request.

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

Tests cover wake/sleep boundaries, overnight windows, repeated DST hours, reboot
deduplication after a failed command/container restart, retrying a failed launch
without another reboot, dimming despite force-stop failure, corrupt state, and
app-only morning mode. Additional tests cover authenticated access, CSRF, password
changes, logout, escaped device errors, duplicate manual requests, concurrent
operation protection, restart recovery, nighttime behavior, and manual-job timeouts.
Configuration tests cover authenticated add/edit/remove flows, validation,
loading frame settings exclusively from the data directory, environment defaults,
boolean/timezone validation, ignored legacy config files, empty starts, rename
history, storage failures, stale forms, concurrent edits, and live worker
startup/removal. Manual display tests cover wake holds through the following day,
manual sleep, restart persistence, reboot interaction, expired/failed requests,
overnight schedules, DST gaps/repeated hours, CSRF, and busy-action protection.
They do not validate a Docker build or real frame firmware.

## References

- [Fix unauthorized VM-to-frame TCP/IP ADB using USB](docs/vm_adb_tcpip_authorization.md)

- Android ADB and activity-manager commands: https://developer.android.com/tools/adb
- Wakeup key semantics: https://developer.android.com/reference/android/view/KeyEvent
- ImmichFrame Android/Frameo instructions: https://github.com/immichFrame/ImmichFrame/blob/main/docs/docs/getting-started/apps.md
- Frameo network ADB notes: https://docs.immichkiosk.app/misc/frameo/
- TrueNAS custom applications: https://apps.truenas.com/managing-apps/installing-custom-apps/

- Flask security guidance: https://flask.palletsprojects.com/en/stable/web-security/
- Waitress deployment: https://flask.palletsprojects.com/en/stable/deploying/waitress/
