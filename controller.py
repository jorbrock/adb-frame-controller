"""Scheduled LAN ADB frame controller with optional authenticated web control."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOG = logging.getLogger("frames")
STOP = threading.Event()
DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG = os.environ.get("CONFIG", "/config/config.json")


def atomic_json(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path, default):
    # Corrupt state fails closed; do not silently discard reboot history.
    return json.loads(path.read_text()) if path.exists() else default


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("Schedule times must be HH:MM")
    hour, mins = map(int, value.split(":"))
    if hour > 23 or mins > 59:
        raise ValueError("Invalid schedule time")
    return hour * 60 + mins


def window(now, frame):
    """Return mode and unique local date of the active wake window."""
    start, end = minute(frame["wake"]), minute(frame["sleep"])
    current = now.hour * 60 + now.minute
    active = start <= current < end if start < end else current >= start or current < end
    date = now.date()
    if start > end and current < end:
        date -= timedelta(days=1)
    return ("day" if active else "night"), date.isoformat()


def load_config():
    config = json.loads(Path(CONFIG).read_text())
    ZoneInfo(config["timezone"])
    if type(config.get("enabled", False)) is not bool:
        raise ValueError("enabled must be a boolean")
    config.setdefault("web", {"enabled": False})
    if type(config["web"].get("enabled", False)) is not bool:
        raise ValueError("web.enabled must be a boolean")
    if type(config["web"].get("secure_cookie", False)) is not bool:
        raise ValueError("web.secure_cookie must be a boolean")
    frames = config["frames"]
    if not isinstance(frames, list) or not frames or len(frames) > 50:
        raise ValueError("Configure 1 to 50 frames")
    ids, addresses = set(), set()
    for frame in frames:
        name = frame["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in ids:
            raise ValueError("Frame names must be unique letters/numbers/underscore/dash")
        ids.add(name)
        address = frame["address"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*:[0-9]{1,5}", address):
            raise ValueError("address must be IPv4-or-hostname:port")
        if not 1 <= int(address.rsplit(":", 1)[1]) <= 65535 or address in addresses:
            raise ValueError("Invalid port or duplicate frame address")
        addresses.add(address)
        if not re.fullmatch(r"[A-Za-z0-9_.]+", frame["package"]):
            raise ValueError("Invalid package")
        if not re.fullmatch(r"[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+", frame["component"]):
            raise ValueError("Invalid activity component")
        if frame["component"].split("/")[0] != frame["package"]:
            raise ValueError("Component must belong to configured package")
        if minute(frame["wake"]) == minute(frame["sleep"]):
            raise ValueError("Wake and sleep must differ")
        frame.setdefault("morning_action", "reboot")
        if frame["morning_action"] not in ("reboot", "restart_app"):
            raise ValueError("morning_action must be reboot or restart_app")
        for key, default, low, high in (
            ("boot_delay_seconds", 60, 0, 600),
            ("night_recheck_seconds", 300, 30, 3600),
        ):
            frame.setdefault(key, default)
            if type(frame[key]) is not int or not low <= frame[key] <= high:
                raise ValueError(f"Invalid {key}")
    return config


class ADB:
    def __init__(self, address):
        self.address = address

    def run(self, *args, timeout=20, targeted=True):
        command = ["adb"] + (["-s", self.address] if targeted else []) + list(args)
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        output = (result.stdout + result.stderr).strip()
        if result.returncode:
            raise RuntimeError(f"ADB {args[0]} failed: {output[:600]}")
        return output

    def connect(self):
        self.run("connect", self.address, targeted=False)
        if self.run("get-state") != "device":
            raise RuntimeError("ADB not authorized or not online")

    def shell(self, *args):
        # ADB passes a remote shell string; quote each argument for that shell too.
        return self.run("shell", shlex.join(args))

    def boot_id(self):
        value = self.shell("cat", "/proc/sys/kernel/random/boot_id")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            raise RuntimeError("Cannot read boot ID; reboot mode requires it")
        return value


class Frame:
    def __init__(self, config, timezone, scheduled=True):
        self.cfg = config
        self.zone = ZoneInfo(timezone)
        self.adb = ADB(config["address"])
        self.path = DATA / (config["name"] + ".state.json")
        self.status_path = DATA / (config["name"] + ".status.json")
        self.state = read_json(self.path, {})
        self.last_night = 0
        self.last_mode = None
        self.scheduled = scheduled
        self.mutex = threading.Lock()
        self.wakeup = threading.Event()

    def save(self):
        atomic_json(self.path, self.state)

    def status(self, result, **extra):
        atomic_json(self.status_path, {
            "frame": self.cfg["name"], "address": self.cfg["address"],
            "updated_at": datetime.now(self.zone).isoformat(),
            "result": result, **extra,
        })

    def tick(self):
        if self.manual_tick():
            return
        if not self.scheduled:
            self.status("schedule_disabled")
            return
        now = datetime.now(self.zone)
        mode, token = window(now, self.cfg)
        if mode != self.last_mode:
            self.last_night = 0
            self.last_mode = mode
        if mode == "night":
            if time.monotonic() - self.last_night < self.cfg["night_recheck_seconds"]:
                return
            self.adb.connect()
            # Try sleep even if force-stop fails; aggregate errors afterwards.
            errors = []
            for args in (("am", "force-stop", self.cfg["package"]),
                         ("input", "keyevent", "223")):
                try:
                    self.adb.shell(*args)
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError("; ".join(errors))
            self.last_night = time.monotonic()
            self.status("sleep_commands_sent", mode=mode)
            LOG.info("%s: app stopped, sleep command sent", self.cfg["name"])
            return
        if self.state.get("completed_window") == token:
            # No claim that a running process is still advancing photographs.
            self.status("morning_sequence_completed", mode=mode)
            return
        self.adb.connect()
        if self.cfg["morning_action"] == "reboot":
            boot_id = self.adb.boot_id()
            if self.state.get("attempted_window") != token:
                # Journal BEFORE the command. A crash can skip a reboot but
                # cannot repeatedly reboot a frame in the same wake window.
                self.state.update(attempted_window=token, previous_boot_id=boot_id)
                self.state.pop("ready_at", None)
                self.save()
                self.status("reboot_requested", mode=mode)
                self.adb.run("reboot")
                LOG.info("%s: morning reboot requested", self.cfg["name"])
                return
            if boot_id == self.state["previous_boot_id"]:
                raise RuntimeError("Waiting for a changed boot ID; no second reboot will be sent this wake window")
        if self.adb.shell("getprop", "sys.boot_completed") != "1":
            raise RuntimeError("Waiting for Android boot completion")
        # Delay after observing completed boot, persisted across container restarts.
        if self.state.get("ready_window") != token or "ready_at" not in self.state:
            self.state.update(ready_window=token,
                              ready_at=time.time() + self.cfg["boot_delay_seconds"])
            self.save()
        if time.time() < self.state["ready_at"]:
            self.status("waiting_for_boot_delay", mode=mode)
            return
        # Recheck schedule before launch in case a command crossed bedtime.
        if window(datetime.now(self.zone), self.cfg) != (mode, token):
            return
        self.launch()
        self.state["completed_window"] = token
        self.save()
        self.status("morning_sequence_completed", mode=mode)
        LOG.info("%s: morning application launch confirmed", self.cfg["name"])

    def launch(self):
        self.adb.shell("input", "keyevent", "224")
        self.adb.shell("am", "force-stop", self.cfg["package"])
        output = self.adb.shell("am", "start", "-W", "-n", self.cfg["component"])
        if re.search(r"error|exception|unable to resolve", output, re.I) or "Status: ok" not in output:
            raise RuntimeError(f"Application launch not confirmed: {output[:600]}")

    def request_reboot(self, request_id):
        # Never wait for ADB in a web request or let two actions overlap.
        if not self.mutex.acquire(blocking=False):
            raise RuntimeError("Frame is busy. Try again shortly.")
        try:
            old = self.state.get("manual", {})
            if old.get("id") == request_id:
                return  # Same browser request is idempotent, even after completion.
            if old.get("phase") in ("queued", "rebooting", "starting"):
                raise RuntimeError("A reboot is already in progress for this frame.")
            if time.time() - old.get("requested_at", 0) < 120:
                raise RuntimeError("Please wait two minutes between reboot requests.")
            self.state["manual"] = dict(id=request_id, phase="queued", requested_at=time.time(),
                                        message="Waiting to connect")
            self.save()
            self.wakeup.set()
            LOG.info("%s: manual reboot queued", self.cfg["name"])
        finally:
            self.mutex.release()

    def manual_tick(self):
        job = self.state.get("manual", {})
        if job.get("phase") not in ("queued", "rebooting", "starting"):
            return False
        # Bounded job, including unreachable frames. Never retry reboot itself.
        if time.time() - job["requested_at"] > 900:
            job.update(phase="failed", message="Timed out after 15 minutes. Check ADB connectivity and the frame.")
            self.save()
            self.status("manual_reboot_failed", error=job["message"])
            return True
        try:
            self.adb.connect()
            boot_id = self.adb.boot_id()
            if job["phase"] == "queued":
                job.update(phase="rebooting", previous_boot_id=boot_id,
                           message="Reboot requested; waiting for the frame")
                mode, token = window(datetime.now(self.zone), self.cfg)
                if mode == "day":
                    # Manual reboot fulfills today's reboot attempt too.
                    self.state.update(attempted_window=token, previous_boot_id=boot_id)
                self.save()  # Journal before sending, just like the scheduled path.
                self.adb.run("reboot")
                return True
            if boot_id == job["previous_boot_id"]:
                raise RuntimeError("Waiting for the frame to reboot; no duplicate reboot will be sent")
            if self.adb.shell("getprop", "sys.boot_completed") != "1":
                raise RuntimeError("Waiting for Android to finish booting")
            if job.get("ready_boot_id") != boot_id:
                job.update(phase="starting", ready_boot_id=boot_id,
                           ready_at=time.time() + self.cfg["boot_delay_seconds"],
                           message="Android is ready; allowing the frame to settle")
                self.save()
            if time.time() < job["ready_at"]:
                return True
            mode, token = window(datetime.now(self.zone), self.cfg)
            if self.scheduled and mode == "night":
                self.adb.shell("am", "force-stop", self.cfg["package"])
                self.adb.shell("input", "keyevent", "223")
                message = "Reboot completed; sleep requested for the night schedule"
                self.last_night = time.monotonic()
            else:
                self.launch()
                self.state["completed_window"] = token
                message = "Reboot completed and ImmichFrame launched"
            job.update(phase="completed", message=message, completed_at=time.time())
            self.save()
            self.status("manual_reboot_completed")
            LOG.info("%s: %s", self.cfg["name"], message)
        except Exception as exc:
            job["message"] = str(exc)[:600]
            self.save()
            self.status("manual_reboot_waiting", error=job["message"])
            LOG.warning("%s manual reboot: %s", self.cfg["name"], exc)
        return True

    def snapshot(self):
        # Atomic files keep page loads independent of slow ADB calls.
        state = read_json(self.path, {})
        status = read_json(self.status_path, {})
        job = state.get("manual", {})
        return {**self.cfg, "mode": window(datetime.now(self.zone), self.cfg)[0],
                "status": status, "job": job,
                "busy": job.get("phase") in ("queued", "rebooting", "starting")}

    def work(self):
        while not STOP.is_set():
            self.wakeup.clear()
            try:
                with self.mutex:
                    self.tick()
            except Exception as exc:
                LOG.warning("%s: %s", self.cfg["name"], exc)
                try:
                    self.status("error", error=str(exc))
                except OSError:
                    LOG.exception("Cannot write frame status")
            self.wakeup.wait(5 if self.state.get("manual", {}).get("phase") in
                             ("queued", "rebooting", "starting") else 30)


def health():
    heartbeat = DATA / "heartbeat"
    return 0 if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 90 else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "validate", "status", "health"], nargs="?", default="run")
    command = parser.parse_args().command
    if command == "health":
        return health()
    if command == "status":
        print(json.dumps([read_json(p, {}) for p in sorted(DATA.glob("*.status.json"))], indent=2))
        return 0
    config = load_config()
    if command == "validate":
        print(f"Valid: {len(config['frames'])} frames; enabled={config.get('enabled', False)}")
        return 0
    DATA.mkdir(parents=True, exist_ok=True)
    lock = (DATA / "controller.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    workers = []
    # Load all state before any device receives commands. Manual control also
    # works with scheduling disabled; existing configuration stays compatible.
    frames = [Frame(frame, config["timezone"], config.get("enabled", False))
              for frame in config["frames"]]
    if config["web"].get("enabled", False):
        from webui import create_app
        from waitress import create_server
        server = create_server(create_app(config, frames, DATA), host="0.0.0.0", port=8080,
                               threads=4, max_request_body_size=8192, connection_limit=32)
        worker = threading.Thread(target=server.run, name="web", daemon=True)
        worker.start()
        workers.append(worker)
        LOG.info("Web interface listening on port 8080")
    for frame in frames:
        worker = threading.Thread(target=frame.work, name=frame.cfg["name"], daemon=True)
        worker.start()
        workers.append(worker)
    if not config.get("enabled", False):
        LOG.warning("Scheduling disabled. Authorize ADB and test frames, then enable config and restart.")
    while not STOP.is_set():
        if any(not worker.is_alive() for worker in workers):
            raise RuntimeError("A frame worker exited unexpectedly")
        (DATA / "heartbeat").touch()
        STOP.wait(15)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
