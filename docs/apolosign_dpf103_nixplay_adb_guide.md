# Enabling ADB on an ApoloSign DPF103 Running Nixplay

## Overview

This procedure was developed for an **ApoloSign DPF103** digital photo frame shipped with the **Nixplay** firmware rather than Frameo.

The tested unit had these characteristics:

- ApoloSign model: **DPF103**
- Nixplay firmware: **7.9.4**
- Nixplay system version: **v1.96**
- Android device codename: **L10A01**
- Android version: **7.1.2 / NHG47K**
- SoC: **Rockchip RK3126C**
- PCB: **R125-V1.1**
- Android build type: **userdebug / test-keys**
- Rear USB-C port
- Hidden microSD/TF slot inside the enclosure

The Nixplay UI does not expose Android Developer Options, and during a normal boot the USB-C port does not initially enumerate as an ADB device.

However, the firmware already contains a functional `adbd`. Nixplay has simply commented out the `start adbd` commands in the boot ramdisk. The solution is to enter the factory recovery environment, gain root ADB access, modify the Android boot image, clear the persistent Rockchip recovery request, and boot normally.

> **Warning:** This procedure writes directly to the frame's eMMC boot partition. A mistake can make the frame unbootable. Back up every partition you modify before writing anything.

---

## 1. Install the tools on macOS

Install Android Platform Tools:

```bash
brew install android-platform-tools
```

You will need Python 3, which is already available on most modern Macs:

```bash
python3 --version
```

You will also need `cpio`, `gzip`, `xxd`, `strings`, and standard Unix utilities. macOS includes these.

For working with Android boot images, download the `platform_system_tools_mkbootimg` repository:

https://github.com/jbeich/platform_system_tools_mkbootimg/tree/master

From the repository, the important Python utilities are:

```text
mkbootimg.py
unpack_bootimg.py
```

Create a working directory containing:

```text
adb
mkbootimg.py
unpack_bootimg.py
```

Using `./adb` in the commands below assumes `adb` is in that directory. If installed through Homebrew and available globally, simply substitute `adb`.

---

## 2. Enter the DPF103 recovery environment

On this hardware, recovery mode is entered with the following exact sequence:

1. Disconnect power from the frame.
2. Press and hold **both Power and Reset simultaneously**.
3. While continuing to hold both buttons, reconnect power.
4. Release the buttons after the recovery sequence begins.
5. Connect the rear USB-C port to the Mac.

Check:

```bash
./adb devices
```

The frame should appear similar to:

```text
0123456789ABCDEF    recovery
```

Recovery normally performs an automatic factory-reset/update operation and reboots after roughly 30 seconds, so the first task is to obtain root and prevent recovery from completing its reboot sequence.

---

## 3. Restart recovery ADB as root

Run:

```bash
./adb root
```

On this frame the command may appear to hang and eventually report:

```text
timeout expired while waiting for device
```

That does **not necessarily mean it failed**.

Run it again:

```bash
./adb root
```

It should now say:

```text
adbd is already running as root
```

The recovery environment does not include many normal Android shell commands directly, but it contains BusyBox at:

```text
/sbin/busybox
```

For example:

```bash
./adb shell '/sbin/busybox ls /'
```

The root shell was confirmed to run as UID 0 with SELinux permissive.

---

## 4. Understand the automatic recovery process

List the running processes:

```bash
./adb shell '/sbin/busybox ps -ef'
```

You should see entries similar to:

```text
/sbin/recovery
/tmp/update_binary 3 22 /cache/update.zip retry
/sbin/adbd --root_seclabel=u:r:su:s0 --device_banner=recovery
```

There are two important processes:

```text
/sbin/recovery
```

and:

```text
/tmp/update_binary
```

The update binary installs the factory firmware from:

```text
/cache/update.zip
```

That OTA package rewrites, among other things:

```text
system
boot
trust
uboot
```

Therefore, **do not patch the boot partition while `update_binary` is still running**. It will simply overwrite your modified boot image with the stock one.

---

## 5. Freeze recovery, but allow the updater to finish

Find the PID belonging to `/sbin/recovery`:

```bash
./adb shell '/sbin/busybox ps -ef'
```

For example:

```text
147 root ... /sbin/recovery
172 root ... /tmp/update_binary 3 22 /cache/update.zip retry
```

Your PIDs will vary.

Suspend the **recovery parent process**, not the updater:

```bash
./adb shell 'kill -19 147'
```

`SIGSTOP` freezes the process without terminating it.

Confirm:

```bash
./adb shell 'cat /proc/147/status'
```

Look for:

```text
State:  T (stopped)
```

Leave `/tmp/update_binary` running.

Check periodically:

```bash
./adb shell '/sbin/busybox ps -ef'
```

Wait until `/tmp/update_binary` disappears.

At this point:

- the stock firmware installation has completed;
- the stock `boot` image has already been written;
- `/sbin/recovery` is frozen before it can finish its cleanup/reboot logic;
- you now have an indefinite root recovery session.

---

## 6. Identify the Rockchip partitions

The named partitions are under:

```text
/dev/block/platform/1021c000.rksdmmc/by-name
```

Set a local convenience variable:

```bash
BASE=/dev/block/platform/1021c000.rksdmmc/by-name
```

List them:

```bash
./adb shell "/sbin/busybox ls -l $BASE"
```

On the tested frame the mapping was:

```text
parameter      -> /dev/block/mmcblk0p1
uboot          -> /dev/block/mmcblk0p2
trust          -> /dev/block/mmcblk0p3
misc           -> /dev/block/mmcblk0p4
resource       -> /dev/block/mmcblk0p5
kernel         -> /dev/block/mmcblk0p6
boot           -> /dev/block/mmcblk0p7
recovery       -> /dev/block/mmcblk0p8
backup         -> /dev/block/mmcblk0p9
cache          -> /dev/block/mmcblk0p10
metadata       -> /dev/block/mmcblk0p11
kpanic         -> /dev/block/mmcblk0p12
radical_update -> /dev/block/mmcblk0p13
keys           -> /dev/block/mmcblk0p14
system         -> /dev/block/mmcblk0p15
userdata       -> /dev/block/mmcblk0p16
```

Back up at minimum the partitions we will touch:

```bash
./adb exec-out "/sbin/busybox cat $BASE/boot" > boot-live-backup.img
./adb exec-out "/sbin/busybox cat $BASE/misc" > misc-backup.img
```

Store these somewhere safe.

---

## 7. Dump the stock boot image

Because the entire boot partition is larger than the actual Android boot image, dumping the partition gives you trailing unused bytes.

You can still unpack the resulting image directly in many cases:

```bash
python3 unpack_bootimg.py \
  --boot_img boot-live-backup.img \
  --out boot-unpacked
```

You should get files including:

```text
kernel
ramdisk
second
```

depending on the image.

The tested DPF103 boot image had:

```text
kernel address:       0x60408000
ramdisk address:      0x62000000
second-stage address: 0x60f00000
page size:            16384
```

Check:

```bash
file boot-live-backup.img
```

Typical output:

```text
Android bootimg, kernel (0x60408000),
ramdisk (0x62000000),
second stage (0x60f00000),
page size: 16384,
cmdline (buildvariant=userdebug)
```

---

## 8. Extract the boot ramdisk

Check its compression:

```bash
file boot-unpacked/ramdisk
```

For this firmware it is gzip-compressed.

Extract it:

```bash
mkdir ramdisk
cd ramdisk

gzip -dc ../boot-unpacked/ramdisk | cpio -idmv
```

You should now have files such as:

```text
default.prop
init.rc
init.usb.rc
init.rk30board.usb.rc
init.rk30board.rc
init.rockchip.rc
```

---

## 9. Confirm that ADB has deliberately been disabled

Search:

```bash
grep -RniE \
'persist\.sys\.usb|sys\.usb|adbd|adb\.secure|rkadb|internet\.adb|usb\.config' \
.
```

The tested firmware contained:

```text
default.prop:
persist.sys.usb.config=adb
```

and elsewhere:

```properties
ro.debuggable=1
```

The system image also contained:

```properties
ro.build.type=userdebug
ro.build.tags=test-keys
ro.adb.secure=0
persist.sys.usb.config=mtp,adb
```

So ADB support is already built into the firmware.

The important discovery is in:

```text
init.rk30board.usb.rc
```

For `adb` mode:

```rc
on property:sys.usb.config=adb
    write /sys/class/android_usb/android0/enable 0
    write /sys/class/android_usb/android0/idVendor 2207
    ...
    write /sys/class/android_usb/android0/functions ${sys.usb.config}
    write /sys/class/android_usb/android0/enable 1
    #start adbd
    setprop sys.usb.state ${sys.usb.config}
```

And for `mtp,adb`:

```rc
on property:sys.usb.config=mtp,adb
    ...
    #start adbd
    setprop sys.usb.state ${sys.usb.config}
```

Those commented `start adbd` lines are the reason ADB does not start during a normal Nixplay boot.

The platform-specific USB file is actively imported through:

```rc
import init.${ro.hardware}.usb.rc
```

and this device uses:

```text
ro.hardware=rk30board
```

so Android resolves that to:

```text
init.rk30board.usb.rc
```

---

## 10. Enable ADB

Edit:

```text
init.rk30board.usb.rc
```

Change the `adb` handler from:

```rc
#start adbd
```

to:

```rc
start adbd
```

Do the same in the `mtp,adb` handler.

There are additional modes such as:

```text
rndis,adb
ptp,adb
accessory,adb
midi,adb
```

There is no need to modify them for this procedure.

The resulting relevant sections should contain:

```rc
on property:sys.usb.config=adb
    ...
    start adbd
    setprop sys.usb.state ${sys.usb.config}
```

and:

```rc
on property:sys.usb.config=mtp,adb
    ...
    start adbd
    setprop sys.usb.state ${sys.usb.config}
```

---

## 11. Rebuild the ramdisk

From inside the extracted `ramdisk` directory:

```bash
find . -print0 | cpio --null -o -H newc | gzip -9 > ../ramdisk-new.gz
```

Move back to your boot working directory:

```bash
cd ..
```

Validate:

```bash
file ramdisk-new.gz
gzip -t ramdisk-new.gz
```

The gzip test should return silently.

---

## 12. Rebuild the Android boot image

The exact header parameters from the original image **must be preserved**.

Use `unpack_bootimg.py` to inspect the source image and record:

- kernel load address
- ramdisk load address
- second-stage load address
- tags address
- page size
- board/product name
- command line
- OS version / patch level if present
- header version

For example:

```bash
python3 unpack_bootimg.py \
  --boot_img boot-live-backup.img \
  --out original-unpacked
```

Older versions of `unpack_bootimg.py` may only support:

```text
--boot_img
--out
```

and may **not** support:

```text
--format=mkbootimg
```

That is normal.

Use those original parameters with `mkbootimg.py`, substituting your new ramdisk while keeping the original:

```text
kernel
second-stage image
page size
addresses
board name
cmdline
header properties
```

The general structure is:

```bash
python3 mkbootimg.py \
  --kernel original-unpacked/kernel \
  --ramdisk ramdisk-new.gz \
  --second original-unpacked/second \
  --pagesize <original-page-size> \
  --base <original-base> \
  --kernel_offset <original-kernel-offset> \
  --ramdisk_offset <original-ramdisk-offset> \
  --second_offset <original-second-offset> \
  --tags_offset <original-tags-offset> \
  --cmdline '<original-command-line>' \
  --board '<original-board-name>' \
  --header_version <original-header-version> \
  --output boot-adb.img
```

Do **not** blindly use the example offsets above. Preserve the values from the image you actually dumped.

---

## 13. Verify the rebuilt image

Check:

```bash
file boot-adb.img
```

For this DPF103 it should still report:

```text
Android bootimg
kernel (0x60408000)
ramdisk (0x62000000)
second stage (0x60f00000)
page size: 16384
```

Unpack the modified image again:

```bash
rm -rf verify-unpacked
python3 unpack_bootimg.py \
  --boot_img boot-adb.img \
  --out verify-unpacked
```

Extract its ramdisk:

```bash
mkdir verify-ramdisk
cd verify-ramdisk

gzip -dc ../verify-unpacked/ramdisk | cpio -idmv
```

Verify:

```bash
grep -n -A10 -B2 \
'on property:sys.usb.config=adb' \
init.rk30board.usb.rc
```

Confirm that it contains:

```rc
start adbd
```

Then return to your working directory.

---

## 14. Prevent the factory updater from running again

Once `/tmp/update_binary` has finished and `/sbin/recovery` remains frozen, rename the OTA package:

```bash
./adb shell \
  '/sbin/busybox mv /cache/update.zip /cache/update.zip.disabled'
```

Check:

```bash
./adb shell '/sbin/busybox ls -lh /cache/update.zip*'
```

You should see:

```text
/cache/update.zip.disabled
```

and no active:

```text
/cache/update.zip
```

This prevents recovery from immediately reinstalling the stock boot image if recovery is entered again unexpectedly.

---

## 15. Clear the Rockchip recovery boot request

This step is critical.

On this device, `misc` contains:

```text
boot-recovery
recovery
```

but **not at offset zero**.

Inspect it first:

```bash
./adb exec-out \
  "/sbin/busybox hexdump -C $BASE/misc" | head -80
```

On the tested DPF103:

```text
00004000  ... |boot-recovery...|
00004040  ... |recovery........|
```

Therefore the recovery command begins at:

```text
0x4000
```

A common mistake is to zero the first 2 KiB of `misc`:

```bash
dd if=/dev/zero ... bs=2048 count=1
```

That does **not** work on this frame because it only clears `0x0000–0x07ff`.

Back up `misc` first:

```bash
./adb exec-out \
  "/sbin/busybox cat $BASE/misc" \
  > misc-before-clear.img
```

Then clear 2 KiB beginning at `0x4000`:

```bash
./adb shell \
  "/sbin/busybox dd if=/dev/zero of=$BASE/misc bs=512 seek=32 count=4"
```

Explanation:

```text
32 × 512 = 16384 bytes = 0x4000
4 × 512  = 2048 bytes
```

Sync:

```bash
./adb shell '/sbin/busybox sync'
```

Verify:

```bash
./adb exec-out \
  "/sbin/busybox hexdump -C $BASE/misc" | head -80
```

There should no longer be:

```text
boot-recovery
recovery
```

at `0x4000`.

---

## 16. Flash the modified boot image

Push it:

```bash
./adb push boot-adb.img /tmp/boot-adb.img
```

Write it:

```bash
./adb shell \
  "/sbin/busybox dd if=/tmp/boot-adb.img of=$BASE/boot bs=4096"
```

Sync:

```bash
./adb shell '/sbin/busybox sync'
```

---

## 17. Verify that the image was actually written

Dump the boot partition again:

```bash
./adb exec-out \
  "/sbin/busybox cat $BASE/boot" \
  > boot-after-flash.img
```

Do **not** expect:

```bash
shasum -a 256 boot-adb.img boot-after-flash.img
```

to match.

`boot-adb.img` is shorter than the entire boot partition, while `boot-after-flash.img` contains the complete partition including trailing bytes.

Instead compare only the prefix equal to the size of `boot-adb.img`:

```bash
python3 - <<'PY'
from pathlib import Path
import hashlib

src = Path("boot-adb.img").read_bytes()
dump = Path("boot-after-flash.img").read_bytes()

print("Source size:", len(src))
print("Partition dump size:", len(dump))

print("Source SHA256:")
print(hashlib.sha256(src).hexdigest())

print("Partition-prefix SHA256:")
print(hashlib.sha256(dump[:len(src)]).hexdigest())

print("Prefix identical:", dump[:len(src)] == src)
PY
```

You want:

```text
Prefix identical: True
```

---

## 18. Perform the final checks before booting

Verify that recovery is still frozen:

```bash
./adb shell '/sbin/busybox ps -ef'
```

Verify the updater is **gone**:

```text
/tmp/update_binary
```

should no longer appear.

Verify the OTA has been renamed:

```bash
./adb shell '/sbin/busybox ls -lh /cache/update.zip*'
```

Verify `misc` no longer contains `boot-recovery`:

```bash
./adb exec-out \
  "/sbin/busybox hexdump -C $BASE/misc" | head -80
```

Verify your modified boot partition one final time if desired.

Then:

```bash
./adb shell '/sbin/busybox sync'
```

---

## 19. Power-cycle the frame

**Do not use `adb reboot`.**

`/sbin/recovery` is deliberately suspended, and allowing it to resume or process a normal reboot can restore recovery state.

Instead:

1. Wait several seconds after `sync`.
2. Physically disconnect power.
3. Wait approximately 10 seconds.
4. Reconnect power normally.
5. Do **not** hold Power or Reset.

The frame should now boot directly into Nixplay rather than factory recovery.

---

## 20. Confirm normal-boot ADB

Once Nixplay is running, connect USB-C to the Mac and run:

```bash
adb devices
```

The frame should now appear during normal Android operation.

On the tested unit:

- no Developer Options toggle was required;
- no RSA authorization prompt appeared;
- no password was required.

That is consistent with the firmware settings:

```properties
ro.debuggable=1
ro.adb.secure=0
```

You can then test:

```bash
adb shell
```

and inspect the normal Android environment.

---

## Why this works

The Nixplay firmware already contains almost everything required for ADB.

The Android system reports:

```properties
ro.build.type=userdebug
ro.build.tags=test-keys
ro.adb.secure=0
persist.sys.usb.config=mtp,adb
```

The boot ramdisk additionally contains:

```properties
ro.debuggable=1
persist.sys.usb.config=adb
```

The `adbd` service is defined normally:

```rc
service adbd /sbin/adbd --root_seclabel=u:r:su:s0
```

But the Rockchip USB init configuration deliberately contains:

```rc
#start adbd
```

instead of:

```rc
start adbd
```

Uncommenting that one command restores the ADB functionality that is already present in the firmware.

The other complication is the factory recovery mechanism. Entering the hardware recovery sequence causes the Rockchip `misc` partition to contain a persistent request:

```text
boot-recovery
recovery
```

On this DPF103 that Bootloader Control Block is located beginning at offset:

```text
0x4000
```

If it is not cleared, every reboot re-enters recovery. Recovery then executes `/cache/update.zip`, and that OTA package rewrites the stock `boot.img`, silently undoing the ADB modification.

The successful sequence is therefore:

```text
Enter recovery
      ↓
Restart adbd as root
      ↓
Freeze /sbin/recovery
      ↓
Allow update_binary to finish
      ↓
Disable /cache/update.zip
      ↓
Patch boot image
      ↓
Clear misc at 0x4000
      ↓
sync
      ↓
Hard power-off
      ↓
Normal boot with ADB
```

---

## Troubleshooting

### `adb root` times out

This occurred consistently on the tested frame.

Run:

```bash
adb root
```

Then, after it times out, immediately run:

```bash
adb root
```

again.

If you receive:

```text
adbd is already running as root
```

the first attempt succeeded and merely lost the connection while `adbd` restarted.

### `ls`, `id`, `getprop`, etc. are missing in recovery

Use the recovery BusyBox binary:

```bash
/sbin/busybox
```

For example:

```bash
adb shell '/sbin/busybox ls -l /'
adb shell '/sbin/busybox ps -ef'
adb shell '/sbin/busybox dd ...'
```

Not every BusyBox applet is necessarily compiled in.

### The frame immediately factory-resets after flashing

Check `misc`:

```bash
adb exec-out \
  '/sbin/busybox hexdump -C /dev/block/platform/1021c000.rksdmmc/by-name/misc' \
  | head -80
```

If you still see:

```text
boot-recovery
recovery
```

at `0x4000`, the bootloader is still being instructed to enter recovery.

Clear the correct offset, not the start of the partition.

### My patched `boot.img` reverted

The most likely reason is that:

```text
/tmp/update_binary
```

was still running.

The factory OTA explicitly rewrites `boot`, so the patch must be written **after the updater has completed**.

Freeze `/sbin/recovery`, let `update_binary` finish, and only then flash the modified image.

### SHA-256 of the boot partition doesn't match `boot-adb.img`

That is expected if you dump the entire boot partition.

Compare only the first `len(boot-adb.img)` bytes of the partition dump.

### USB-C still does not enumerate

Confirm that the modified ramdisk actually contains:

```rc
start adbd
```

inside:

```text
init.rk30board.usb.rc
```

Then verify:

```properties
persist.sys.usb.config=adb
```

or:

```properties
persist.sys.usb.config=mtp,adb
```

is still present.

Also confirm that the frame is using:

```text
ro.hardware=rk30board
```

because that determines which platform USB init file is imported.

---

## Restoring the original boot image

If normal Android no longer boots but recovery remains accessible:

1. Enter recovery with Power + Reset.
2. Restart ADB as root.
3. Freeze recovery as described earlier.
4. Push the saved boot backup:

```bash
adb push boot-live-backup.img /tmp/boot-original.img
```

5. Restore it:

```bash
BASE=/dev/block/platform/1021c000.rksdmmc/by-name

adb shell \
  "/sbin/busybox dd if=/tmp/boot-original.img of=$BASE/boot bs=4096"

adb shell '/sbin/busybox sync'
```

6. Clear the recovery request at `misc + 0x4000`.
7. Physically power-cycle.

Keep the original `boot`, `misc`, and preferably `parameter` partition backups permanently.

---

## Applicability to other frames

This procedure is specifically verified on the **ApoloSign DPF103 / L10A01 / RK3126C Nixplay build** described above.

A similar ApoloSign frame may use a different:

- partition map;
- recovery-key combination;
- `misc` offset;
- boot-image page size;
- SoC;
- Android version;
- hardware init filename.

Before writing anything on another model, verify:

```bash
adb shell 'echo /dev/block/platform/*/by-name/*'
```

inspect its `parameter` partition, dump its `misc` partition, and confirm where `boot-recovery` is actually stored.

In particular, **do not assume `0x4000` is universal**. It is the confirmed location for this DPF103 firmware.
