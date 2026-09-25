# Configure a new frame end to end

This walkthrough starts with **ADB already enabled, Wi-Fi connected, and a static
IP assigned**. It takes one frame from initial ADB access to a blank default Home
screen, working ImmichFrame slideshow, and scheduled control through this project.
No firmware flashing or ADB-enabling procedure is included.

## 1. Gather the addresses and tools

You will need a computer with [Android SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools)
(`adb`), a USB data cable for the initial setup and app installs, access to the
frame's touchscreen, and a Docker/TrueNAS host for the
controller. You also need an existing Immich library and an ImmichFrame server;
step 4 covers preparing that server if it is not running yet.

Replace these example addresses throughout:

| Purpose | Example | Used by |
| --- | --- | --- |
| Frame's static IP and TCP ADB port | `192.168.30.200:5555` | Computer and controller container |
| Immich server URL | `http://192.168.10.20:2283` | ImmichFrame server |
| ImmichFrame server URL | `http://192.168.10.20:8080` | Android slideshow app |
| ADB Frame Controller web UI | `http://192.168.10.13` | Your browser |

The last two services are separate. This repository controls the display through
ADB; it does not serve photos or need an Immich API key. Keep ADB on your trusted
LAN, with routing/firewall rules allowing the Docker host to reach the frame's
ADB port. Publishing port 5555 on the controller container is unnecessary: it
makes an outbound connection to the frame.

Commands below identify where to run them. Replace APK paths with the actual
files you downloaded. If `adb` is not on your PATH, run it from the Platform Tools
directory (for example, `./adb` on macOS/Linux).

## 2. Connect over USB

Connect the frame to your **computer** with a USB data cable and run:

```bash
adb devices -l
```

Accept the debugging prompt on the frame and select **Always allow from this
computer** if offered. The entry must say `device`, not `unauthorized` or
`offline`. Some frame firmware does not require authorization and shows no prompt.

The USB commands assume only one device is attached and no other ADB devices or
emulators are connected, so no device selector is needed:

```bash
adb shell getprop ro.product.model
```

A returned model name confirms shell access. Keep USB connected through the app
installs and display checks in steps 3–5. Wireless ADB is set up in step 6, just
before preparing the controller container.

## 3. Install Discreet Launcher and make Home blank

Download the APK from [Discreet Launcher releases](https://github.com/falzonv/discreet-launcher/releases).
On your **computer**, install it and open Android's Home app selection:

```bash
adb install -r /path/to/discreet-launcher.apk
adb shell am start -a android.settings.HOME_SETTINGS
```

On the **frame**:

1. Choose **Discreet Launcher** as the default Home app. If that screen is
   unavailable, use **Android Settings → Apps → Default apps → Home app**, or
   press Home and choose Discreet Launcher with **Always**.
2. Open the launcher's menu/settings. Set a solid black wallpaper using the
   available wallpaper picker; if it offers only images, supply an all-black image.
3. Disable the clock, turn off **Always show favorites**, enable **No system bars
   (immersive mode)**, and enable **No menu button** after finishing configuration.
   Setting names and placement can vary by version. Return Home and confirm no
   icons, clock, or controls remain visible at rest. The available customization
   options and swipe gestures are described on the
   [Discreet Launcher site](https://falzonv.github.io/discreet-launcher/).
4. Swipe up when you need the app drawer. A blank Home screen still allows you to
   open apps deliberately.

On your **computer**, test Home:

```bash
adb shell input keyevent 3
```

**Keep Discreet Launcher as the default Home app after installing ImmichFrame.**
The controller force-stops ImmichFrame at night; making ImmichFrame Home can cause
Android to relaunch it immediately. For this setup, skip the upstream ImmichFrame
instructions that make ImmichFrame the default launcher or a screensaver.

In Android/device settings, disable automatic sleep, screensavers, and any vendor
power schedule that would suspend Wi-Fi or restart a slideshow independently.
You can open Android Settings from your computer if the launcher hides it:

```bash
adb shell am start -a android.settings.SETTINGS
```

A black wallpaper provides the blank fallback screen. The controller separately
sets brightness to zero at night; check the actual backlight behavior on your model.

## 4. Install and configure ImmichFrame

### Prepare the ImmichFrame server

If an existing ImmichFrame server already displays the intended photos, reuse its
URL and client authentication secret and proceed to the APK installation.

Otherwise, on your **Docker host**, deploy a separate ImmichFrame service using
its [official Docker setup](https://immichframe.dev/docs/getting-started/installation/docker).
Use `ghcr.io/immichframe/immichframe`, persist `/app/Config` in a writable volume,
and publish its container port 8080 on an available host port. A bind-mounted
configuration directory must be writable by its runtime UID 1000. Keep this
volume separate from the controller's `/data` volume.

For current releases, open `http://IMMICHFRAME_HOST:PORT/admin`, choose an admin
password on a new installation (or use the configured `IMMICHFRAME_ADMIN_PASSWORD`),
and add your Immich account. Use **Test connection** and save. See the
[ImmichFrame admin UI instructions](https://immichframe.dev/docs/getting-started/admin-ui).

Configure the account's `ImmichServerUrl` and Immich `ApiKey`, then choose the
albums/photo filters and slideshow interval you want. Create the API key in the
Immich account that can access those photos. Optionally set
`General.AuthenticationSecret` for slideshow clients. That client secret is
separate from both the Immich API key and the admin password. Current releases
import settings files only on first start and then use the settings database;
older releases may require their version's configuration-file workflow. See the
[configuration reference](https://immichframe.dev/docs/getting-started/configuration).

Open the ImmichFrame server's root URL in a browser and verify photos advance
before configuring the Android client.

### Install the Android app

Download the APK from the [ImmichFrame Android releases](https://github.com/immichFrame/ImmichFrame_Android/releases).
On your **computer**:

```bash
adb install -r /path/to/ImmichFrame.apk
adb shell am start -W -n com.immichframe.immichframe/.MainActivity
```

On the **frame**, swipe down to open ImmichFrame settings:

1. Enter the **ImmichFrame server URL**, such as `http://192.168.10.20:8080`.
   Use the published host address, not `localhost`, a Docker-only service name,
   the Immich library URL, or the controller UI URL.
2. If configured, enter the server's client **Authorization Secret**. Otherwise
   leave it empty. Do not enter the Immich API key here.
3. On older Frameo devices, disable **WebView** as described in the
   [official Android/Frameo instructions](https://immichframe.dev/docs/getting-started/apps).
   Leave it disabled for the initial check, then follow the WebView 106 upgrade
   section below to enable WebView on a compatible frame.
4. Save/apply the settings, return to the slideshow, and confirm several photos
   advance. Disable any app-level automatic start or schedule option that would
   conflict with the controller. Leave Discreet Launcher as Home.

The package and activity above are also the values to use in the controller UI.
If your APK differs, inspect installed packages with
`adb shell pm list packages` and verify its launch activity
before continuing.

### Update WebView to 106 while connected by USB

ImmichFrame documents **LineageOS WebView 106.0.5249.126 (arm64-v8a + arm-v7a,
Android 6.0+)** for older Frameo devices, tested on 10.1-inch Android 6.0.1 frames.
Download the APK using the link in its
[Frameo WebView Update instructions](https://immichframe.dev/docs/getting-started/apps#frameo-webview-update).
This replaces a system APK and requires root plus a writable `/system`; enabled
ADB alone is insufficient. Apply it only to compatible firmware, and keep a newer
working WebView on devices that already have one.

#### Check the frame and back up its original APK

Keep USB attached. On your **computer**, inspect the Android version, CPU ABIs,
WebView version, and installed APK path:

```bash
adb shell getprop ro.build.version.release
adb shell getprop ro.product.cpu.abilist
adb shell dumpsys package com.android.webview
adb shell pm path com.android.webview
```

In the package output, look for `versionName`. The commands below assume the APK
path is `/system/app/webview/webview.apk`. If your output differs, stop and resolve
that firmware's provider layout before using these paths.

Save a backup on your computer, then stage the downloaded APK:

```bash
adb pull /system/app/webview/webview.apk ./webview-original.apk
adb push /path/to/downloaded/webview-106.apk /sdcard/webview.apk
adb shell
```

You are now in the **frame's shell**. Check privileges:

```sh
id
```

If it does not report `uid=0`, run `su`, then `id` again. If root is unavailable,
leave WebView disabled in ImmichFrame and skip the system replacement.

#### Replace the system WebView

In the **root shell on the frame**, remount the system partition and preserve an
on-device backup. Stop if either command fails; do not overwrite an existing
`.bak` from an earlier attempt.

```sh
mount -o rw,remount /system
test ! -e /system/app/webview/webview.apk.bak && cp /system/app/webview/webview.apk /system/app/webview/webview.apk.bak
```

After both the computer backup and on-device backup succeed, replace the APK and
remove the old compiled WebView cache:

```sh
cp /sdcard/webview.apk /system/app/webview/webview.apk
chown 0:0 /system/app/webview/webview.apk
chmod 644 /system/app/webview/webview.apk
rm -rf /system/app/webview/oat
sync
```

Check each command for errors before continuing. If `restorecon` is available on
the frame, run `restorecon /system/app/webview/webview.apk` to restore its SELinux
label. Exit back to your computer's terminal (`exit` twice if you entered `su`),
then reboot over USB:

```bash
adb reboot
adb wait-for-device
adb shell getprop sys.boot_completed
```

Wait until the last command returns `1`; `wait-for-device` alone only waits for
ADB. Verify the installed version in **Android Settings → Apps → Show system →
Android System WebView**, or repeat `adb shell dumpsys package com.android.webview`
and check `versionName` for `106.0.5249.126`.

Launch ImmichFrame again:

```bash
adb shell am start -W -n com.immichframe.immichframe/.MainActivity
```

Swipe down to settings, enable **WebView**, save/apply, and check that photos and
any configured overlays render and advance. Confirm Home still opens Discreet
Launcher. Keep USB attached for step 5.

#### Restore the original if the replacement fails

If Android and USB ADB remain accessible, disable WebView in ImmichFrame. Open
`adb shell`, obtain root as above, and restore the saved system APK:

```sh
mount -o rw,remount /system
cp /system/app/webview/webview.apk.bak /system/app/webview/webview.apk
chown 0:0 /system/app/webview/webview.apk
chmod 644 /system/app/webview/webview.apk
rm -rf /system/app/webview/oat
sync
```

Restore the SELinux label with `restorecon` if available, exit to the computer,
and run `adb reboot`. If the on-device backup is missing, stage your computer's
`webview-original.apk` with `adb push` and copy that file back from the root shell.
If Android no longer boots or USB ADB is unavailable, recovery is firmware-specific;
these shell commands require a working Android ADB session.

## 5. Test blank Home and display control over USB

With USB still connected, run these commands on your **computer**. First record
the current brightness so
you can choose a daytime value (the controller accepts 1–255):

```bash
adb shell settings get system screen_brightness
adb shell input keyevent 3
adb shell am force-stop com.immichframe.immichframe
adb shell settings put system screen_brightness_mode 0
adb shell settings put system screen_brightness 0
```

Confirm the screen is dark and ImmichFrame stays stopped. This checks display
behavior; the wireless connection and overnight reachability are tested in step 6.
Do not send Android's sleep key (keycode 223); it can make network ADB unreachable.

Restore the display, using your preferred brightness instead of `128`:

```bash
adb shell input keyevent 224
adb shell settings put system screen_brightness_mode 0
adb shell settings put system screen_brightness 128
adb shell am start -W -n com.immichframe.immichframe/.MainActivity
```

Expect `Status: ok` and an advancing slideshow. Leave the frame connected by USB
for the next step.

## 6. Set up and test wireless ADB

With the initial setup complete and USB still connected, run on your **computer**:

```bash
adb tcpip 5555
adb connect 192.168.30.200:5555
adb shell "settings put global adb_enabled 1"
adb shell "settings put global development_settings_enabled 1"
adb shell su
setprop persist.adb.tcp.port 5555
exit
adb reboot
```

Accept any new debugging prompt on the frame. Disconnect USB, then verify access:

```bash
adb -s 192.168.30.200:5555 shell getprop ro.product.model
```

A returned model name confirms the connection is wireless. From this point on,
use `adb -s IP:PORT` to target the frame's wireless connection. If TCP ADB
was already enabled, skip `tcpip` and start with `adb connect`. These steps follow
Android's [ADB connection documentation](https://developer.android.com/tools/adb).

### Check overnight reachability

With USB unplugged, repeat the night commands over the wireless connection:

```bash
adb -s 192.168.30.200:5555 shell input keyevent 3
adb -s 192.168.30.200:5555 shell am force-stop com.immichframe.immichframe
adb -s 192.168.30.200:5555 shell settings put system screen_brightness_mode 0
adb -s 192.168.30.200:5555 shell settings put system screen_brightness 0
```

Leave the frame dark for a meaningful interval, ideally overnight, then reconnect
and restore it. Replace `128` with your tested daytime brightness:

```bash
adb connect 192.168.30.200:5555
adb -s 192.168.30.200:5555 shell getprop ro.product.model
adb -s 192.168.30.200:5555 shell input keyevent 224
adb -s 192.168.30.200:5555 shell settings put system screen_brightness_mode 0
adb -s 192.168.30.200:5555 shell settings put system screen_brightness 128
adb -s 192.168.30.200:5555 shell am start -W -n com.immichframe.immichframe/.MainActivity
```

Require working ADB access and an advancing slideshow without reconnecting USB.

### Check wireless ADB after reboot

**TCP ADB may not survive reboot.** `adb tcpip 5555` does not guarantee a persistent
firmware setting. With USB still unplugged, record the boot ID and reboot:

```bash
adb -s 192.168.30.200:5555 shell cat /proc/sys/kernel/random/boot_id
adb -s 192.168.30.200:5555 reboot
```

Wait for Android and Wi-Fi to start, then run:

```bash
adb connect 192.168.30.200:5555
adb -s 192.168.30.200:5555 shell getprop sys.boot_completed
adb -s 192.168.30.200:5555 shell cat /proc/sys/kernel/random/boot_id
adb -s 192.168.30.200:5555 shell input keyevent 3
```

Require `sys.boot_completed` to return `1`, a changed boot ID, and a blank
Discreet Launcher Home screen. Repeat the wireless display-restore commands above
and confirm ImmichFrame retained its settings.

If wireless ADB stops listening, reconnect USB and repeat
`adb tcpip 5555`, then reconnect wirelessly and unplug USB again. Choose **Restart app only**
in step 9 until your firmware's persistent network ADB configuration is established.
That avoids scheduled reboots but does not solve loss of ADB after a power outage
or manual reboot.

### Alternative: Android paired wireless debugging

If the frame provides **Wireless debugging → Pair device with pairing code**, you
can use this instead of fixed-port TCP ADB. Run `adb pair FRAME_IP:PAIRING_PORT`,
enter the displayed code, then run `adb connect FRAME_IP:CONNECTION_PORT`. Use the
connection port in the wireless tests above and in the controller UI. The two
ports differ, and the connection port can change. Disconnect USB for those tests.
Pair separately from the controller container in step 8. Fixed-port TCP ADB is the
primary setup for this project.

## 7. Prepare the ADB Frame Controller container

On the **Docker/TrueNAS host**, use this repository's
[Docker Compose file](../docker-compose.yaml). For a new deployment:

1. Create a persistent host data directory and grant UID/GID `3019:3019` read/write
   access (use the TrueNAS ACL editor if applicable). Mount it at `/data`.
2. Update the Compose file's host data path, `ports.host_ip`, and published web
   port for your host. The checked-in mapping publishes **port 80**, targeting
   container port 8080, so its example UI URL is `http://192.168.10.13`.
3. Set `TZ` to your IANA timezone, such as `America/Los_Angeles`.
4. Set **`SCHEDULE_ENABLED: "false"`** during setup. The checked-in Compose file
   currently sets it to `"true"`; change it explicitly. Keep `WEB_ENABLED: "true"`.
5. From the repository directory, build the local image and start it:

```bash
docker build -t frame-controller:1.6.0 .
docker compose -f docker-compose.yaml up -d
```

For **TrueNAS Apps**, build the image on the TrueNAS host and install/apply the
Compose YAML through **Apps → Discover Apps → Install via YAML** instead of
starting a second instance with Compose. The [README](../README.md) includes
storage, permissions, and TrueNAS deployment details.

If the controller is already running, use its existing data volume and deployment.
Pausing `SCHEDULE_ENABLED` affects **all frames**. Manual actions still work; apply
environment changes by recreating the container through its existing deployment
method. A plain restart does not load changed Compose environment variables.

## 8. Authorize the controller container on the frame

Authorization belongs to the running container's persisted ADB key, not the Docker
image. Your computer's authorization does not authorize the controller.

On a **Compose host**, open a shell as the controller's runtime user:

```bash
docker compose -f docker-compose.yaml exec --user 3019:3019 frame-controller sh
```

For **TrueNAS**, open the running app container's shell as UID/GID `3019:3019`.
Inside the **controller container**, check the user and key location:

```sh
id
printenv HOME
```

Expect UID 3019 and `/data`. Run the following in that same shell while you can
see and interact with the frame:

```sh
adb connect 192.168.30.200:5555
adb devices -l
```

On the **frame**, accept the new debugging prompt and select **Always allow** if
available. If brightness is zero, first restore it using the already-authorized
computer. Then, inside the **controller container**, verify real shell access:

```sh
adb -s 192.168.30.200:5555 shell getprop ro.product.model
adb -s 192.168.30.200:5555 shell getprop sys.boot_completed
```

Require a `device` entry, a returned model, and boot completion `1`. On firmware
without ADB authentication, no prompt appears; successful shell access is the
check. For paired wireless debugging, run `adb pair IP:PAIRING_PORT` from this
container first, then connect to the frame's connection port.

If remote ADB stays `unauthorized` and you have authorized USB access plus root
on the frame, follow [the VM TCP/IP authorization guide](vm_adb_tcpip_authorization.md).

Keep `/data/.android` in the persistent data volume; it holds the controller's ADB
keys. Do not authorize an unrelated temporary container or a root shell with a
different home directory and assume the application will share that identity.
No USB passthrough, privileged mode, or host networking is required.

## 9. Add the frame in the controller web UI

For a new controller, set its login in the **controller container shell**:

```sh
python /app/webui.py set-password --username admin
```

Enter the password when prompted. For an existing installation, use its existing
login; this command changes/resets the account. Open the controller's published
web URL in your **browser**, sign in, and select **Add frame**.

| UI field | Example / guidance |
| --- | --- |
| Name | `living-room`; unique letters, numbers, underscores, or dashes |
| ADB address | `192.168.30.200:5555`; unique, with the actual connection port |
| App package | `com.immichframe.immichframe` |
| Activity component | `com.immichframe.immichframe/.MainActivity` |
| Wake time | `07:00` |
| Sleep time | `22:00`; must differ from wake time |
| Morning action | **Reboot frame** only after the reboot test passes; otherwise **Restart app only** |
| Day brightness | `128`, or your tested value from 1–255 |
| Boot delay (seconds) | `60`; extra delay after Android reports boot complete, allowed range 0–600 |
| Night recheck (seconds) | `300`; repeat night commands every five minutes, allowed range 30–3600 |

Times use the controller's global `TZ`, shown on the form. Select **Add frame** to
save. Settings persist in `/data/frames.json` and apply without a restart. Use
**Edit settings** for later changes; do not edit the JSON while the controller runs.

In the **controller container shell**, confirm the saved configuration:

```sh
python /app/controller.py validate
python /app/controller.py status
```

## 10. Test from the UI, then enable the schedule

With scheduling paused:

1. Click **Wake frame**. Wait for completion and verify the chosen brightness and
   an advancing slideshow on the physical frame.
2. Click **Sleep frame**. Verify the display is dark and stays dark. From the
   container, repeat the ADB model query to confirm network access remains alive.
3. Click **Wake frame** again and confirm recovery. If reboot persistence passed,
   test the UI's reboot action while awake and wait through boot plus the configured
   delay. Confirm ImmichFrame launches again.
4. Recreate the controller container with the same data mount while scheduling is
   still paused, sign in again, and repeat **Wake frame**. This checks saved frame
   settings and the persisted ADB authorization.

Wait for each manual action to finish before requesting another. UI results
report controller commands; inspect the actual frame to verify photo progression.

Set `SCHEDULE_ENABLED: "true"` in your deployment YAML and apply it. For Compose:

```bash
docker compose -f docker-compose.yaml up -d
```

Confirm the UI says **Schedule enabled**. Enabling scheduling during daytime can
immediately run the morning action, including a reboot; enabling it at night can
immediately apply night mode. Manual wake/sleep overrides can defer scheduled
behavior: wake holds until the next sleep event, and sleep holds until the next
wake event. Overrides persist across container restarts, and existing override
expiry times are not changed by editing a schedule. Allow those overrides to
expire when evaluating the first automatic cycle.

Observe one full night/morning cycle. At night, require a dark display with
working wireless ADB. In the morning, require the intended brightness and photos
advancing after the configured morning action. Back up the controller data volume
and Compose file; also back up the separate ImmichFrame configuration volume.

## Troubleshooting

| Symptom | Check / next step |
| --- | --- |
| `unauthorized` from the container | Restore visible brightness from the computer, accept the container's prompt on the frame, and reconnect. Verify the shell runs as UID 3019 with `HOME=/data`. |
| `offline` or connection refused | Disconnect/reconnect that address with `adb disconnect IP:PORT` and `adb connect IP:PORT`; check IP, port, Wi-Fi, routing, and whether reboot disabled TCP ADB. |
| Computer connects but container cannot | Check Docker-host routing/firewalls to the frame and authorize the container's own key. A successful ping alone does not prove the ADB port is reachable. |
| Home returns to a vendor slideshow or ImmichFrame | Re-select Discreet Launcher as default Home; check firmware/vendor auto-start behavior and app schedules. Repeat the Home/reboot test before enabling scheduling. |
| Blank Home still glows at night | Confirm brightness mode is manual and brightness is zero. Some panels retain a visible minimum backlight; the controller cannot guarantee darkness on every model. |
| ImmichFrame cannot load photos | Verify its server URL and client secret, server-to-Immich access, account/API key, and selected albums. On older frames, try disabling WebView. |
| `Activity ... does not exist` | Confirm the installed APK's package/activity and update the controller form to match. |
| Frame sleeps but cannot be reached later | Disable Android sleep/screensavers and vendor power schedules; repeat the overnight ADB test. |
| Schedule seems inactive | Check `Schedule enabled`, the global timezone, frame action results, and manual override expiry. For Compose logs use `docker compose -f docker-compose.yaml logs --tail=100 frame-controller`. |
