# ADB Frame Controller

A small Python + ADB container with a local web interface that stops ImmichFrame and requests screen sleep
at night, then reboots and explicitly launches the app each morning. It operates
independently of Immich, ImmichFrame's server, and Immich Kiosk; no Immich API key
or changes to those containers are required.

This is generated source code, not a published container image. The local logic
tests use mocked ADB; actual Frameo firmware behavior must be tested on your devices.

## Upgrade from v1.0.0: add the web interface

1. Extract this updated project into a source directory. Keep your existing
   `config` and `data` directories, including the ADB keys and state files.
2. Build the updated image on TrueNAS from this directory:

   ```bash
   docker build -t frame-controller:1.1.0 .
   ```

3. Add this top-level property to your existing `config/config.json` (preserve
   your existing `enabled`, timezone, and frames):

   ```json
   "web": { "enabled": true, "secure_cookie": false }
   ```

4. Edit your existing TrueNAS app YAML. Change the image to
   `frame-controller:1.1.0` and add a port mapping under the service:

   ```yaml
   ports:
     - "YOUR_TRUENAS_LAN_IP:8080:8080"
   ```

   Replace `YOUR_TRUENAS_LAN_IP` with the TrueNAS LAN address reachable by your
   browser (not a frame's IP). If host port 8080 is occupied, use e.g.
   `"YOUR_TRUENAS_LAN_IP:8088:8080"` and browse port 8088 instead. Keep existing
   volume mappings and UID/GID settings. Apply the update/redeploy the app.
5. In the running container shell in TrueNAS, set your account:

   ```bash
   python /app/webui.py set-password --username admin
   ```

   Enter a password at the hidden prompts. At least eight characters are required.
   There is no default password. If using Compose CLI instead, run
   `docker compose exec frame-controller python /app/webui.py set-password`.
6. Open `http://YOUR_TRUENAS_LAN_IP:8080`, sign in, and choose **Reboot frame**.
   Confirm the selected frame in the dialog. Its progress updates automatically.

The web UI can run with scheduling disabled (`enabled: false` at the top level).
Manual control still works. Existing v1.0 configuration remains valid and leaves
web access disabled until you add `web.enabled: true`.

### Web behavior and local login

- Lists only frames in your config, with address, wake/sleep times, recent
  controller results, and manual reboot progress. Configuration editing remains
  in the JSON file; there is no arbitrary-command endpoint or bulk reboot button.
- Manual reboot runs independently per frame and is serialized with its scheduled
  work. It explicitly relaunches ImmichFrame after Android boots and the delay
  expires. At night, if scheduling is enabled, it returns the frame to sleep.
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
- Login and reboot forms use CSRF tokens; state-changing operations require POST.
  Cookies are HttpOnly and SameSite=Strict. Failed logins are rate limited.
  The UI uses only bundled CSS/JavaScript, with no CDN or external services.
- Direct LAN HTTP is supported. HTTP does not encrypt the password or session in
  transit; use a unique local password. If you later use your local reverse proxy
  with HTTPS, set `web.secure_cookie` to `true` and restart the app. Keep it false
  for direct HTTP. Preserve the existing subnet restrictions.

## Behavior

- Independent worker per frame; one unreachable device does not block the others.
- Per-frame local wake/sleep times, using an IANA timezone with daylight saving time.
- At night: `am force-stop PACKAGE`, then `input keyevent 223` (SLEEP).
  Repeats every five minutes by default to handle incidental wakeups.
- In the morning: one reboot attempt per wake window, reconnect, confirm the
  kernel boot ID changed, wait for `sys.boot_completed=1`, allow an additional
  60 seconds, wake the screen with keycode 224, then explicitly launch the app.
- Persisted state prevents another reboot in the same wake window after a
  container restart. A failed/ambiguous reboot is not automatically repeated.
  Connection and launch failures retry every 30 seconds.
- The scheduler catches up after downtime. Enabling or first starting it during
  the day immediately begins that day's morning sequence, including a reboot.
  Starting it at night immediately applies sleep. Schedule changes need a restart.
- Spring DST gaps take effect at the first available time after the scheduled
  boundary. Repeated fall hours share one wake-window date and do not add a reboot.
- JSON status files, stdout logs, and a Docker scheduler heartbeat health check.
- Non-root container, no privileged mode, Docker socket, USB access, or host networking
  required. Port 8080 serves the optional web UI. LAN routing to each frame is required.

## 1. Prove these prerequisites on ONE frame

Use its reserved IP and actual ADB port. Commands below assume `192.168.40.61:5555`.

```bash
adb connect 192.168.40.61:5555
adb -s 192.168.40.61:5555 shell pm list packages
adb -s 192.168.40.61:5555 shell am force-stop com.immichframe.immichframe
adb -s 192.168.40.61:5555 shell input keyevent 223
```

Confirm the backlight actually turns off. Leave it asleep for a meaningful
interval (ideally overnight), then reconnect and test wake/start:

```bash
adb connect 192.168.40.61:5555
adb -s 192.168.40.61:5555 shell input keyevent 224
adb -s 192.168.40.61:5555 shell am start -W -n com.immichframe.immichframe/.MainActivity
```

Test a reboot, reconnect after the frame has booted, then check:

```bash
adb -s 192.168.40.61:5555 reboot
# Wait for the device to boot before the next commands.
adb connect 192.168.40.61:5555
adb -s 192.168.40.61:5555 shell getprop sys.boot_completed
adb -s 192.168.40.61:5555 shell cat /proc/sys/kernel/random/boot_id
adb -s 192.168.40.61:5555 shell am start -W -n com.immichframe.immichframe/.MainActivity
```

`sys.boot_completed` must return `1`, and the activity start should report
`Status: ok`. The package/activity above are the ones documented by ImmichFrame;
change the configuration if your APK uses different names.

**Wireless ADB must survive reboot.** Enabling TCP ADB with `adb tcpip 5555` can
be temporary. Frameo persistence varies by firmware; some require USB again
after every reboot. This container cannot reconnect to a disabled ADB service.
Do not enable scheduled reboot until you have verified persistence. You can use
`"morning_action": "restart_app"` instead, which wakes and restarts the app without
rebooting, but ADB must still remain reachable while the screen is asleep.

Keycode 223 is an explicit sleep request, not the power toggle. OEM firmware can
ignore it or turn off only part of the display hardware. If it does not turn the
backlight off, stop here and diagnose the device's display controls before relying
on this schedule. A black image or brightness zero is not necessarily backlight off.

If ImmichFrame is configured as the HOME launcher, Android may relaunch it when
force-stopped. Other launchers, kiosk tools, and app wake locks may also interfere.
Observe one full night/morning cycle; disable conflicting app schedules where appropriate.
The controller sends commands but does not assert the physical display is off.

## 2. Prepare TrueNAS storage and build

Use a Docker-based TrueNAS release. Replace `tank` with your pool name throughout.
Create a dataset/directory for the project, extract this archive into it, and open
a TrueNAS administrative shell in the extracted `frame-controller` directory.
These commands require appropriate host permissions:

```bash
mkdir -p /mnt/tank/apps/frame-controller/config
mkdir -p /mnt/tank/apps/frame-controller/data
cp config.example.json /mnt/tank/apps/frame-controller/config/config.json
chown 568:568 /mnt/tank/apps/frame-controller/data
chmod 700 /mnt/tank/apps/frame-controller/data
docker build -t frame-controller:1.1.0 .
```

The image build requires internet access for the Python base image, Debian ADB
packages, and the Flask/Waitress Python dependencies. If your dataset uses ACLs, grant UID/GID 568 read access to configuration
and write access to data using the TrueNAS ACL editor. Preserve the data directory:
it contains ADB private keys under `.android` and reboot history. Restrict its access.

Edit `config/config.json` with your frame IPs, schedules, and timezone. Remove the
second example frame or duplicate entries as needed. Names and addresses must be
unique. The sample timezone is `America/Los_Angeles`. Leave `enabled` false initially.
Edit the two bind-mount paths in `compose.yaml` to match your actual dataset.
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

Set the local web password using the command in the upgrade section above.
After the single-frame tests pass, set `enabled` to `true` in the host config file
and restart the app. Remember: starting during daytime triggers a catch-up reboot.
The example staggers wake times slightly to spread load on the photo server.

## Monitoring and recovery

In the TrueNAS container shell:

```bash
python /app/controller.py status
adb devices -l
```

Logs record sleep commands, reboot requests, confirmed launches, and errors.
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
from section 1 in the container shell. During the night, the scheduler will send
sleep again within the configured recheck interval.

Use DHCP reservations. On UniFi, permit the TrueNAS container's effective source
IP (normally the TrueNAS LAN IP with bridge networking) to reach only the frames'
ADB TCP ports. Keep ADB LAN-only and block access from untrusted networks; do not
publish it through your router or Cloudflare. Only the web UI port needs inbound access from your trusted devices. ADB port
5555 remains outbound from the container to the frames. Frames retain their
existing access to the photo server.

## Tests

```bash
python -m pip install -r requirements.txt
python -m unittest -v
```

Tests cover wake/sleep boundaries, overnight windows, repeated DST hours, reboot
deduplication after a failed command/container restart, retrying a failed launch
without another reboot, sleep despite force-stop failure, corrupt state, and
app-only morning mode. Additional tests cover authenticated access, CSRF, password
changes, logout, escaped device errors, duplicate manual requests, concurrent
operation protection, restart recovery, nighttime behavior, and manual-job timeouts.
They do not validate a Docker build or real frame firmware.

## References

- Android ADB and activity-manager commands: https://developer.android.com/tools/adb
- Sleep/wakeup key semantics: https://developer.android.com/reference/android/view/KeyEvent
- ImmichFrame Android/Frameo instructions: https://github.com/immichFrame/ImmichFrame/blob/main/docs/docs/getting-started/apps.md
- Frameo network ADB notes: https://docs.immichkiosk.app/misc/frameo/
- TrueNAS custom applications: https://apps.truenas.com/managing-apps/installing-custom-apps/

- Flask security guidance: https://flask.palletsprojects.com/en/stable/web-security/
- Waitress deployment: https://flask.palletsprojects.com/en/stable/deploying/waitress/

