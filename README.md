# ADB Frame Controller

A small Python + ADB container with a local web interface that stops ImmichFrame and sets screen brightness to zero
at night, then reboots and explicitly launches the app each morning. It operates
independently of Immich, ImmichFrame's server, and Immich Kiosk; no Immich API key
or changes to those containers are required.

This is generated source code, not a published container image. The local logic
tests use mocked ADB; actual Frameo firmware behavior must be tested on your devices.

### Web behavior and local login

- Lists configured frames with address, wake/sleep times, recent controller
  results, and manual reboot progress. Use **Add frame**, **Edit settings**, and
  **Remove** to manage up to 50 frames without restarting the controller.
- Frame forms cover name, ADB address, app package/activity, wake/sleep times,
  morning action, day brightness, boot delay, and night recheck interval.
  Removal requires a confirmation page. Empty configurations are supported.
- Settings are validated before saving. Busy frames and active manual reboots
  reject edits/removal; stale forms cannot overwrite newer changes.
- Manual reboot runs independently per frame and is serialized with its scheduled
  work. It explicitly relaunches ImmichFrame after Android boots and the delay
  expires. At night, if scheduling is enabled, it stops the app and sets brightness to zero.
- Repeated submissions of the most recent request are ignored. Busy frames reject new
  requests, and there is a two-minute cooldown between manual reboot requests.
  A frame briefly busy doing scheduled work may ask you to try again shortly.
- Manual jobs persist across container restarts and have a 15-minute timeout.
  Connections/launches retry; the actual reboot command is sent at most once per
  request. A successful manual recovery also fulfills the current morning window.
- A failure is shown in the frame's card. You can request another reboot after
  the pending job finishes or times out. An unchanged boot ID is never considered
  a successful reboot. These statuses do not detect stalled photo progression.
- One username/password account, with a salted password hash stored in
  `/data/web-auth.json`. Sessions expire after 12 hours and are invalidated when
  the container restarts or the password changes. Run `set-password` again to
  change/reset the account; no restart is needed for that change.
- Login, reboot, and configuration forms use CSRF tokens; changes require POST.
  Cookies are HttpOnly and SameSite=Strict. Failed logins are rate limited.
  The UI uses only bundled CSS/JavaScript, with no CDN or external services.
- Direct LAN HTTP is supported. HTTP does not encrypt the password or session in
  transit; use a unique local password. If you later use your local reverse proxy
  with HTTPS, set `WEB_SECURE_COOKIE` to `"true"` and recreate the container. Keep it false
  for direct HTTP. Preserve the existing subnet restrictions.

### Saving frame settings

Frame settings are stored only in `/data/frames.json` (under `DATA_DIR` when
configured). If that file is missing, the controller starts with no frames;
use **Add frame** in the web UI to create the first one. Every successful UI
change saves the complete list, which is loaded again on restart. No `config.json`
file or `/config` mount is needed. Back up the data directory and your Compose
file. Invalid saved settings fail validation instead of being discarded.

Changes apply to live workers. Adding a frame while scheduling is enabled can
immediately start its morning sequence or apply night mode. Edits preserve reboot
history, including when renaming a frame: a completed morning sequence does not
run again just because settings changed. App and brightness changes take effect
on the next app launch; schedule changes are evaluated on the next worker tick.
An active manual reboot must finish or time out before editing or removal.
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
| `SCHEDULE_ENABLED` | `"false"` | Enable scheduled wake/sleep actions. Manual reboot remains available when false. |
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
- At night: `am force-stop PACKAGE`, set `screen_brightness_mode` to `0`
  (manual), then set `screen_brightness` to `0`. Android stays awake for network ADB.
  Repeats every five minutes by default to handle incidental app starts or brightness changes.
- In the morning: one reboot attempt per wake window, reconnect, confirm the
  kernel boot ID changed, wait for `sys.boot_completed=1`, allow an additional
  60 seconds, wake the screen with keycode 224, set manual brightness to
  `day_brightness` (default `128`, configurable per frame from `1` to `255`),
  then explicitly launch the app. Daytime manual reboots restore this brightness too.
- Persisted state prevents another reboot in the same wake window after a
  container restart. A failed/ambiguous reboot is not automatically repeated.
  Connection and launch failures retry every 30 seconds.
- The scheduler catches up after downtime. Enabling or first starting it during
  the day immediately begins that day's morning sequence, including a reboot.
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
chown 568:568 /mnt/tank/apps/frame-controller/data
chmod 700 /mnt/tank/apps/frame-controller/data
docker build -t frame-controller:1.1.0 .
```

The image build requires internet access for the Python base image, Debian ADB
packages, and the Flask/Waitress Python dependencies. If your dataset uses ACLs,
grant UID/GID 568 read/write access to data using the TrueNAS ACL editor.
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

## Monitoring and recovery

In the TrueNAS container shell:

```bash
python /app/controller.py status
adb devices -l
```

Logs record night-mode commands, reboot requests, confirmed launches, and errors.
Status shows each frame's latest result and timestamp. Docker health only confirms
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
To manually restart only an app during the day, use the force-stop and start commands
from section 1 in the container shell. During the night, the scheduler will stop the app and set brightness to zero again within the configured recheck interval.

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
startup/removal. They do not validate a Docker build or real frame firmware.

## References

- Android ADB and activity-manager commands: https://developer.android.com/tools/adb
- Wakeup key semantics: https://developer.android.com/reference/android/view/KeyEvent
- ImmichFrame Android/Frameo instructions: https://github.com/immichFrame/ImmichFrame/blob/main/docs/docs/getting-started/apps.md
- Frameo network ADB notes: https://docs.immichkiosk.app/misc/frameo/
- TrueNAS custom applications: https://apps.truenas.com/managing-apps/installing-custom-apps/

- Flask security guidance: https://flask.palletsprojects.com/en/stable/web-security/
- Waitress deployment: https://flask.palletsprojects.com/en/stable/deploying/waitress/
