# Connect a server VM to a photo frame over TCP/IP ADB

Use this guide when the VM can reach the photo frame but `adb devices -l` shows
`unauthorized`, and another computer already has working USB ADB access to the
frame. You will add the VM's public ADB key to the frame's authorized-key file,
then reconnect from the VM.

This procedure requires root access on the frame. It applies to firmware using
`/data/misc/adb/adb_keys` and traditional TCP/IP ADB, usually on port `5555`.
If the frame displays an **Allow USB debugging?** prompt for the VM, accepting it
and selecting **Always allow** is the easier authorization method. Android's
[ADB documentation](https://developer.android.com/tools/adb) describes this prompt
and the USB-to-TCP/IP connection workflow.

## Before you start

You will use three places. Each step below identifies the correct one:

| Where | What you do there |
| --- | --- |
| **Server VM** | Read the public key and test the network connection. |
| **USB computer** | Run ADB against the frame over its USB data cable and edit the key file. |
| **Frame root shell** | Replace the protected key file and restart the frame's ADB service. This shell is opened from the USB computer. |

Have these ready:

- Android SDK Platform Tools (`adb`) on the VM and USB computer.
- A working USB data cable and an already-authorized USB connection.
- Root access through `su` on the frame, or firmware that already runs ADB as root.
- The frame's IP address. This guide uses `192.168.30.200`; replace it with yours.
- Network access from the VM to the frame's TCP port `5555` on your trusted LAN.

If this controller already manages the frame, set **Frame management → Disabled**
in its configuration while troubleshooting. Re-enable it after the final checks.
This prevents scheduled commands from interrupting your work.

On the **USB computer**, check the connection:

```bash
adb devices -l
adb -d shell getprop ro.product.model
```

The USB entry should say `device`, and the second command should print a model.
This guide uses `adb -d` to select the USB device even if a network connection is
also present. If more than one USB Android device is attached, replace `-d` with
`-s USB_SERIAL`, using the frame's serial from `adb devices -l`.

If TCP/IP ADB is not already enabled, run on the **USB computer**:

```bash
adb -d tcpip 5555
```

Wait for ADB to reconnect before continuing. Enabling TCP/IP this way does not
guarantee it survives a frame reboot; verify that separately before enabling
scheduled reboots. See [new frame setup](new_frame_setup.md#6-set-up-and-test-wireless-adb)
for the broader setup workflow.

## 1. Copy the VM's public ADB key

On the **server VM**, use the same Linux account that normally runs ADB:

```bash
cat ~/.android/adbkey.pub
```

Copy the entire line, including its trailing comment if present. A terminal may
wrap the long line visually; it is still one key and must stay on one line in the
file. Copy only the file contents, without a terminal prompt or command text.

If the file does not exist, first run `adb start-server` as that account, then
try again. Do not delete existing keys to generate new ones.

**If ADB runs inside the controller container**, use its key instead of the VM
login account's key. From the directory containing this project's Compose file,
run on the **server VM**:

```bash
docker compose exec --user 3019:3019 frame-controller cat /data/.android/adbkey.pub
```

If needed, initialize that container's ADB server with:

```bash
docker compose exec --user 3019:3019 frame-controller adb start-server
```

The container uses `HOME=/data`. Its key is separate from the VM user's key and
must remain in the persistent data volume. Copy `adbkey.pub`, the **public** key;
keep `adbkey`, the **private** key, on the machine that owns it.

## 2. Pull the frame's current authorized keys

On the **USB computer**, open a terminal in a new working folder and run:

```bash
adb -d pull /data/misc/adb/adb_keys ./adb_keys
```

Make a local copy named `adb_keys.backup` before editing. You can use your file
manager, or on Linux/macOS:

```bash
cp ./adb_keys ./adb_keys.backup
```

Keep all existing keys. They authorize the USB computer and any other hosts that
already connect successfully. Android's [ADB authentication implementation](https://android.googlesource.com/platform/system/core/+/0aeb50500c76ea67d6f452907f5503d590e81a54%5E%21/)
identifies `/data/misc/adb/adb_keys` as a device authorization-key location.

### If the pull says `Permission denied`

On the **USB computer**, open a shell:

```bash
adb -d shell
```

Inside the **frame shell**, run:

```sh
su
id
```

Confirm `id` includes `uid=0`. If the shell already runs as root, skip `su`.
Then copy the keys to a location the regular ADB connection can read:

```sh
cp /data/misc/adb/adb_keys /sdcard/adb_keys.original
exit
```

If `exit` only leaves `su`, type `exit` again to return to the USB computer's
terminal. Then pull the copy and make your local backup:

```bash
adb -d pull /sdcard/adb_keys.original ./adb_keys
```

If root is unavailable, use the frame's authorization prompt instead; this
manual file-replacement procedure cannot continue. If the original key file is
missing, stop and check the firmware's authorization mechanism rather than
creating an empty replacement that omits existing authorization.

## 3. Add the VM key locally

On the **USB computer**, open `adb_keys` in a plain-text editor, such as VS Code
or Notepad. Do not use a word processor.

1. Leave every existing key unchanged.
2. Put the public key from step 1 on a new line at the bottom. If that exact key
   is already present, do not add a duplicate.
3. Press Enter after the final key so the file ends with a newline and the cursor
   sits on a blank line below it.
4. Save as plain text with UTF-8 encoding without a BOM and Unix/LF line endings.
   Keep the filename `adb_keys`, without an added `.txt` extension.

Each key must occupy one physical line. Do not insert spaces or line breaks into
the long encoded key text.

## 4. Push the edited file to the frame

On the **USB computer**, from the same working folder:

```bash
adb -d push ./adb_keys /sdcard/adb_keys
```

This stages the edited file in writable storage. It does not yet replace the
protected authorization file.

## 5. Enter a root shell on the frame

On the **USB computer**:

```bash
adb -d shell su
```

Inside the **frame root shell**:

```sh
id
ls -l /data/misc/adb/adb_keys
```

Confirm `uid=0` before continuing. Record the existing file's owner, group, and
permissions. If `adb shell su` does not open an interactive shell on this
firmware, use `adb -d shell`, followed by `su`. If ADB already runs as root,
`adb -d shell` is sufficient.

## 6. Back up and replace the authorized-key file

Inside the **frame root shell**, make a backup with a name you have not used
before, then copy the staged file over the existing file:

```sh
cp -p /data/misc/adb/adb_keys /data/misc/adb/adb_keys.before-vm
cp /sdcard/adb_keys /data/misc/adb/adb_keys
```

Keep an existing `adb_keys.before-vm` backup if you are repeating this procedure;
choose a different backup filename for the new attempt. Overwrite the existing
file in place rather than deleting it first, so its ownership and permissions
can be retained. The edited file must include the original keys plus the VM key.

## 7. Verify the replacement

Still inside the **frame root shell**:

```sh
cat /data/misc/adb/adb_keys
ls -l /data/misc/adb/adb_keys
```

Check that the VM key is present as a complete line, that the previous keys remain,
and that ownership and permissions match what you recorded in step 5. If they
changed, restore the original values before restarting ADB; do not make the file
world-writable. If the firmware provides `restorecon`, restore the expected
SELinux label with:

```sh
restorecon /data/misc/adb/adb_keys
```

Once the protected file is verified, remove the temporary copies from shared
storage:

```sh
rm /sdcard/adb_keys
```

If you used the permission-denied workaround, also remove its staged original:

```sh
rm /sdcard/adb_keys.original
```

Keep your local backup and the protected on-frame backup until reconnection works.

## 8. Restart ADB on the frame

Inside the **frame root shell**, the service restart command is:

```sh
stop adbd && start adbd
```

Expect the USB shell to disconnect. **On some firmware, stopping `adbd` kills this
shell before `start adbd` can run.** If that happens, ADB may stay stopped. Use the
frame's local developer settings to toggle debugging, or reboot the frame using
its physical controls, then reconnect over USB. These recovery steps require
firmware that normally starts ADB; do not rely on a reboot if yours needs a
special procedure to enable it.

For a frame already using standard TCP/IP ADB, an alternative to the two service
commands is to exit the root shell and run this on the **USB computer**:

```bash
adb -d tcpip 5555
```

This requests an ADB daemon restart into TCP/IP mode. Allow several seconds for
the service to return before testing from the VM.

## 9. Reconnect from the VM

On the **server VM**, in the same account used in step 1:

```bash
adb disconnect 192.168.30.200:5555
adb connect 192.168.30.200:5555
adb devices -l
adb -s 192.168.30.200:5555 shell getprop ro.product.model
```

Success means the network entry says `device` and the shell command returns the
frame's model. A `connected to ...` message alone is not enough to confirm usable
authorized access.

If you authorized the **controller container's key**, perform these checks inside
that container instead:

```bash
docker compose exec --user 3019:3019 frame-controller adb disconnect 192.168.30.200:5555
docker compose exec --user 3019:3019 frame-controller adb connect 192.168.30.200:5555
docker compose exec --user 3019:3019 frame-controller adb devices -l
docker compose exec --user 3019:3019 frame-controller adb -s 192.168.30.200:5555 shell getprop ro.product.model
```

After successful verification, re-enable **Frame management** if you disabled it.

## If it still does not connect

| Result | What to check next |
| --- | --- |
| `unauthorized` | Confirm you copied the key from the account/container actually running ADB. Check for broken key lines, an added `.txt` extension, and whether the protected file still contains the key after the service restart. Some firmware manages or expires keys differently. |
| `Connection refused` | The address responded, but ADB may not be listening on port `5555`. Reconnect over USB and run `adb -d tcpip 5555`; check the frame's IP and port. |
| Connection timeout or no route | Check Wi-Fi, IP address, VM routing, and firewall access to the frame. Editing keys will not fix network reachability. |
| `offline` | Wait for boot/service startup, then disconnect and reconnect from the VM. |
| `more than one device/emulator` | Use `-d` for one USB device or `-s USB_SERIAL` / `-s IP:5555` to select the intended frame. |
| Works from the VM but not the controller | Authorize `/data/.android/adbkey.pub` from the controller container and test as UID 3019. |
| Stops working after a frame reboot | TCP/IP ADB or key changes may not persist on that firmware. Verify both over USB before selecting scheduled reboot mode. |

If the correct key is installed but the VM still uses stale connection state,
restart the **VM's** ADB server with `adb kill-server` followed by
`adb start-server`, then repeat step 9. This disconnects other ADB sessions served
by that same host ADB server. Run it inside the container if that is the client
you are troubleshooting.

## Restore the original keys if needed

Using the still-authorized **USB computer**, enter the frame root shell as in
step 5. Restore the backup you created in step 6:

```sh
cp -p /data/misc/adb/adb_keys.before-vm /data/misc/adb/adb_keys
cat /data/misc/adb/adb_keys
```

Restore the SELinux label with `restorecon` if available, then use the restart
and verification steps above. Restoring this backup removes authorization for
any keys added after it was taken, including the VM key from this procedure.
