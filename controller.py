"""Scheduled LAN ADB frame controller with optional authenticated web control."""
import argparse
from copy import deepcopy
import fcntl
import frame_log
from wyze_power import POWER, normalize_mac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("frames")
STOP = threading.Event()
POWER_PHASES = ("power_off_pending", "power_wait", "power_on_pending")
SHUTDOWN_PHASES = ("shutting_down", "shutdown_wait", "sleep_power_off")
ACTIVE_PHASES = ("queued", "rebooting", "starting", "boot_wait") + POWER_PHASES + SHUTDOWN_PHASES
POWER_ACTIONS = ("hard_reboot", "power_on", "power_off")
PLUG_ACTIONS = POWER_ACTIONS + ("wake", "sleep")
WAKE_ATTEMPTS = 5
RETRY_SECONDS = 30
SHUTDOWN_ATTEMPTS = 3
SHUTDOWN_GRACE_SECONDS = 30
DATA = Path(os.environ.get("DATA_DIR", "/data"))


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


def next_boundary(now, value):
    """Next daily wall-clock event: first fold, or first valid minute after a gap."""
    hour, mins = divmod(minute(value), 60)
    for days in (0, 1):
        candidate = (now + timedelta(days=days)).replace(hour=hour, minute=mins,
                                                       second=0, microsecond=0, fold=0)
        # A nonexistent DST time takes effect when the local clock resumes.
        while (datetime.fromtimestamp(candidate.timestamp(), now.tzinfo).replace(tzinfo=None)
               != candidate.replace(tzinfo=None)):
            candidate += timedelta(minutes=1)
        if candidate.timestamp() > now.timestamp():
            return candidate.timestamp()
    raise ValueError("Cannot find next schedule boundary")


def env_bool(name, default):
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def load_config():
    timezone = os.environ.get("TZ", "UTC")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("TZ must be a valid IANA timezone, such as UTC or America/Los_Angeles") from exc
    return validate_config(dict(
        enabled=env_bool("SCHEDULE_ENABLED", False),
        timezone=timezone,
        web=dict(enabled=env_bool("WEB_ENABLED", True),
                 secure_cookie=env_bool("WEB_SECURE_COOKIE", False)),
        frames=read_json(DATA / "frames.json", []),
    ))


def validate_config(config):
    config = deepcopy(config)
    ZoneInfo(config["timezone"])
    if type(config.get("enabled", False)) is not bool:
        raise ValueError("enabled must be a boolean")
    config.setdefault("web", {"enabled": False})
    if type(config["web"].get("enabled", False)) is not bool:
        raise ValueError("web.enabled must be a boolean")
    if type(config["web"].get("secure_cookie", False)) is not bool:
        raise ValueError("web.secure_cookie must be a boolean")
    frames = config["frames"]
    if not isinstance(frames, list) or len(frames) > 50:
        raise ValueError("Configure 0 to 50 frames")
    ids, addresses, plugs = set(), set(), set()
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("Each frame must be an object")
        for field in ("name", "address", "package", "component", "wake", "sleep"):
            if not isinstance(frame.get(field), str) or not frame[field]:
                raise ValueError(f"{field} is required and must be text")
        frame.setdefault("enabled", True)
        if type(frame["enabled"]) is not bool:
            raise ValueError("Frame enabled must be a boolean")
        frame.setdefault("run_as_root", False)
        if type(frame["run_as_root"]) is not bool:
            raise ValueError("Frame run_as_root must be a boolean")
        frame["wyze_mac"] = normalize_mac(frame.get("wyze_mac", ""))
        if frame["wyze_mac"]:
            if frame["wyze_mac"] in plugs:
                raise ValueError("Each Wyze plug MAC can belong to only one frame")
            plugs.add(frame["wyze_mac"])
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
        # Retired options are ignored on load and removed on the next settings save.
        for key in ("morning_action", "night_action", "night_recheck_seconds"):
            frame.pop(key, None)
        for key, default, low, high in (
            ("day_brightness", 128, 1, 255),
            ("boot_delay_seconds", 60, 0, 600),
        ):
            frame.setdefault(key, default)
            if type(frame[key]) is not int or not low <= frame[key] <= high:
                raise ValueError(f"Invalid {key}")
    return config


class ADB:
    def __init__(self, address, run_as_root=False):
        self.address = address
        self.run_as_root = run_as_root

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

        if self.run_as_root:
            output = self.run("root")
            # Root can restart adbd and drop the TCP connection.
            self.run("connect", self.address, targeted=False)
            self.run("wait-for-device", timeout=60)
            if self.shell("id", "-u") != "0":
                raise RuntimeError(f"ADB root failed: {output[:600] or 'shell is not running as root'}")

    def shell(self, *args, timeout=20):
        # ADB passes a remote shell string; quote each argument for that shell too.
        return self.run("shell", shlex.join(args), timeout=timeout)

    def boot_id(self):
        value = self.shell("cat", "/proc/sys/kernel/random/boot_id")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            raise RuntimeError("Cannot read boot ID; reboot mode requires it")
        return value


class Frame:
    def __init__(self, config, timezone, scheduled=True, data=None):
        self.cfg = config
        self.zone = ZoneInfo(timezone)
        self.adb = ADB(config["address"], config.get("run_as_root", False))
        data = DATA if data is None else data
        self.path = data / (config["name"] + ".state.json")
        self.status_path = data / (config["name"] + ".status.json")
        self.log_path = data / (config["name"] + ".log.jsonl")
        self.state = read_json(self.path, {})
        job = self.state.get("manual", {})
        if job.get("action") == "hard_reboot" and job.get("phase") == "power_wait":
            # A crash may have interrupted the off request. Allow a fresh 30s
            # before restoring power; never send another off command on recovery.
            job["power_on_at"] = max(job["power_on_at"], time.time() + 30)
        self.scheduled = scheduled
        self.mutex = threading.Lock()
        self.wakeup = threading.Event()
        self.stopped = threading.Event()

    def save(self):
        atomic_json(self.path, self.state)

    def status(self, result, **extra):
        entry = {
            "frame": self.cfg["name"], "address": self.cfg["address"],
            "updated_at": datetime.now(self.zone).isoformat(),
            "result": result, **extra,
        }
        previous = read_json(self.status_path, {})
        atomic_json(self.status_path, entry)
        try:
            # These states are checked every cycle without sending device commands.
            # Refresh the status timestamp, but only log changes to these states.
            unchanged_state = result in ("manual_wake_active", "schedule_disabled") and (
                {key: value for key, value in entry.items() if key != "updated_at"}
                == {key: value for key, value in previous.items() if key != "updated_at"}
            )
            if unchanged_state and self.log_path.exists() and self.log_path.stat().st_size:
                return
            frame_log.append(self.log_path, entry, previous)
        except OSError:
            LOG.exception("Cannot append controller log for %s", self.cfg["name"])

    def active_override(self, now, state=None):
        override = (self.state if state is None else state).get("override", {})
        if override and (not self.scheduled or now.timestamp() < override["expires_at"]):
            return override
        return {}

    def power_suspended(self, state=None):
        return (self.state if state is None else state).get("power") in ("off", "unknown")

    def schedule_paused(self, state=None):
        state = self.state if state is None else state
        # Older explicit Power off journals did not have a separate pause flag.
        return state.get("power_paused", self.power_suspended(state))

    def schedule_event(self, now):
        # Compare actual instants so a repeated DST hour cannot undo an event
        # that already happened in the first fold.
        wake_at = next_boundary(now, self.cfg["wake"])
        sleep_at = next_boundary(now, self.cfg["sleep"])
        return ("sleep", wake_at) if wake_at < sleep_at else ("wake", sleep_at)

    def tick(self):
        if not self.cfg.get("enabled", True):
            return
        now = datetime.now(self.zone)
        if self.state.get("override") and not self.active_override(now):
            del self.state["override"]
            self.save()
        if self.manual_tick():
            return
        if self.schedule_paused() or self.active_override(datetime.now(self.zone)):
            return  # Keep the latest success/error visible throughout an override.
        if not self.scheduled:
            if not self.state.get("manual"):
                self.status("schedule_disabled")
            return
        action, expires_at = self.schedule_event(datetime.now(self.zone))
        event = [action, expires_at]
        if self.state.get("schedule_event") == event:
            return
        # Record the event before commands, including failures. No nightly rechecks
        # or endless wake retries; the next boundary creates a fresh operation.
        previous = deepcopy(self.state)
        self.state["schedule_event"] = event
        self.state["manual"] = dict(
            action=action, source="scheduled", phase="queued", requested_at=time.time(),
            expires_at=expires_at, message=f"Scheduled {action} queued")
        try:
            self.save()
        except OSError:
            self.state = previous
            raise
        self.manual_tick()

    def job_expired(self, job):
        if job.get("source") == "scheduled":
            return not self.scheduled or datetime.now(self.zone).timestamp() >= job["expires_at"]
        return not self.active_override(datetime.now(self.zone))

    def job_status(self, job, result, **extra):
        self.status(f"{job.get('source', 'manual')}_{job.get('action', 'reboot')}_{result}", **extra)

    def finish_job(self, job, message):
        phase = "completed"
        if job.get("action") == "hard_reboot" and not job.get("off_confirmed"):
            phase = "failed"
            message = "Power restored and display mode applied, but the plug off command was not confirmed; hard reboot may not have occurred."
        job.update(phase=phase, message=message, completed_at=time.time())
        if self.scheduled and job.get("source") != "scheduled":
            event = list(self.schedule_event(datetime.now(self.zone)))
            if event[0] == ("sleep" if self.power_suspended() else "wake"):
                self.state["schedule_event"] = event
        self.save()
        self.job_status(job, phase)
        LOG.info("%s: %s", self.cfg["name"], message)

    def shutdown_tick(self, job):
        """Return whether Android shutdown attempts and the grace period are done."""
        if job["phase"] not in SHUTDOWN_PHASES:
            job.update(phase="shutting_down", shutdown_attempts=0, shutdown_started_at=time.time(),
                       forced=False)
            self.state["power_paused"] = False
            self.save()
        if job["phase"] == "shutting_down":
            if job["shutdown_attempts"] >= SHUTDOWN_ATTEMPTS:
                job.update(phase="sleep_power_off", forced=True,
                           message="Android shutdown attempt limit reached; forcing plug power off")
                self.save()
            else:
                job["shutdown_attempts"] += 1
                self.save()  # A restart cannot reset the shutdown attempt budget.
                try:
                    self.adb.connect()
                except Exception as exc:
                    job.update(phase="sleep_power_off", forced=True,
                               message=f"ADB unavailable; forcing plug power off: {str(exc)[:400]}")
                else:
                    # Journal a grace period before sending: Android may shut down
                    # successfully even if the ADB response is lost or we restart.
                    job.update(phase="shutdown_wait", power_off_at=time.time() + SHUTDOWN_GRACE_SECONDS)
                    self.save()
                    errors = []
                    for command in (("svc", "power", "shutdown"), ("reboot", "-p")):
                        try:
                            output = self.adb.shell(*command)
                            # svc can print a failure/usage message and exit zero.
                            if re.search(r"failed|error|exception|denied|not found|usage:", output, re.I):
                                raise RuntimeError(output[:400])
                        except Exception as exc:
                            errors.append(str(exc)[:400])
                        else:
                            job.update(message="Android shutdown requested; allowing 30 seconds before cutting power")
                            break
                    else:
                        job.update(phase="shutting_down", message="; ".join(errors)[:600])
                        if job["shutdown_attempts"] >= SHUTDOWN_ATTEMPTS:
                            job.update(phase="sleep_power_off", forced=True,
                                       message="Android shutdown failed after 3 attempts; forcing plug power off")
                    # Measure the grace period from command completion.
                    job["power_off_at"] = time.time() + SHUTDOWN_GRACE_SECONDS
                self.save()
                self.job_status(job, job["phase"], **({"error": job["message"]} if job.get("forced") else {}))
                if job.get("forced"):
                    LOG.warning("%s: %s", self.cfg["name"], job["message"])
        if job["phase"] == "shutdown_wait":
            if time.time() < job["power_off_at"]:
                return
            job["phase"] = "sleep_power_off"
            self.save()
        return job["phase"] == "sleep_power_off"

    def sleep_tick(self, job):
        """Shut Android down, then leave plug power off until the next wake."""
        if self.shutdown_tick(job):
            self.state.update(power="unknown", power_paused=False)
            self.save()
            POWER.set_power(self.cfg["wyze_mac"], False)
            self.state["power"] = "off"
            self.finish_job(job, "Plug powered off" + (
                "; forced shutdown because Android shutdown was unavailable" if job.get("forced")
                else " after Android shutdown grace period"))

    def wake_tick(self, job):
        """Power up once, then make five connection attempts spaced 30s apart."""
        if job["phase"] == "queued":
            job["phase"] = "power_on_pending"
            self.save()
        if job["phase"] == "power_on_pending":
            POWER.set_power(self.cfg["wyze_mac"], True)
            self.state.update(power="on", power_paused=False)
            job.update(phase="boot_wait", powered_on_at=time.time(), connect_attempts=0,
                       next_attempt_at=time.time() + self.cfg["boot_delay_seconds"],
                       message="Plug powered on; waiting for boot delay")
            self.save()
            self.job_status(job, "boot_wait")
            return
        if time.time() < job.get("next_attempt_at", 0):
            return
        attempts = job.get("connect_attempts", 0)
        if attempts >= WAKE_ATTEMPTS:
            job.update(phase="failed", message="Wake failed after 5 attempts. Check ADB connectivity and frame logs.")
            self.save()
            self.job_status(job, "failed", error=job["message"])
            LOG.error("%s: %s", self.cfg["name"], job["message"])
            return
        job.update(connect_attempts=attempts + 1, next_attempt_at=time.time() + RETRY_SECONDS)
        self.save()
        try:
            self.adb.connect()
            if self.job_expired(job):
                job.update(phase="cancelled", message="The next schedule event passed; wake cancelled.")
                self.save()
                return
            self.adb.shell("setprop", "service.bootanim.exit", "1")
            self.restore_brightness()
            # pidof exits 1 when absent; a remote conditional distinguishes that
            # normal result from an ADB transport failure. Match the main process.
            running = self.adb.shell("sh", "-c", f'pidof {shlex.quote(self.cfg["package"])} || [ "$?" = 1 ]')
            if self.job_expired(job):
                job.update(phase="cancelled", message="The next schedule event passed; wake cancelled.")
                self.save()
                return
            if not running.strip():
                self.start_app()
            elif not re.fullmatch(r"[0-9]+(?:\s+[0-9]+)*", running.strip()):
                raise RuntimeError("Cannot determine whether ImmichFrame is running")
            self.finish_job(job, "Wake completed; brightness restored and ImmichFrame running")
        except Exception as exc:
            attempts = job["connect_attempts"]
            job.update(phase="failed" if attempts == WAKE_ATTEMPTS else "boot_wait",
                       next_attempt_at=time.time() + RETRY_SECONDS,
                       message=f"Wake attempt {attempts}/{WAKE_ATTEMPTS} failed: {str(exc)[:450]}" +
                               (". Retrying in 30 seconds." if attempts < WAKE_ATTEMPTS else ". No attempts remaining."))
            self.save()
            self.job_status(job, "failed" if attempts == WAKE_ATTEMPTS else "waiting", error=job["message"])
            (LOG.error if attempts >= 3 else LOG.warning)("%s: %s", self.cfg["name"], job["message"])

    def trim_device_caches(self):
        self.adb.shell("am", "force-stop", self.cfg["package"])
        self.adb.shell("pm", "trim-caches", "999G", timeout=120)

    def restore_brightness(self):
        self.adb.shell("settings", "put", "system", "screen_brightness_mode", "0")
        self.adb.shell("settings", "put", "system", "screen_brightness",
                       str(self.cfg.get("day_brightness", 128)))

    def start_app(self):
        output = self.adb.shell("am", "start", "-W", "-n", self.cfg["component"])
        if re.search(r"error|exception|unable to resolve", output, re.I) or "Status: ok" not in output:
            raise RuntimeError(f"Application launch not confirmed: {output[:600]}")

    def launch(self):
        self.adb.shell("input", "keyevent", "224")
        self.adb.shell("am", "force-stop", self.cfg["package"])
        self.restore_brightness()
        self.start_app()

    def request_reboot(self, request_id, mode="soft"):
        if mode not in ("soft", "hard"):
            raise ValueError("Reboot mode must be soft or hard")
        self.request_action("hard_reboot" if mode == "hard" else "reboot", request_id)

    def request_action(self, action, request_id):
        if action not in ("wake", "sleep", "reboot", "reset_app") + POWER_ACTIONS:
            raise ValueError("Invalid manual action")
        # Never wait for ADB in a web request or let two actions overlap.
        if not self.mutex.acquire(blocking=False):
            raise RuntimeError("Frame is busy. Try again shortly.")
        try:
            if not self.cfg.get("enabled", True):
                raise RuntimeError("Frame management is disabled. Enable it in settings first.")
            old = self.state.get("manual", {})
            if old.get("id") == request_id:
                if old.get("action", "reboot") != action:
                    raise RuntimeError("Request ID already used for a different action.")
                return  # Same browser request never renews an override.
            if old.get("phase") in ACTIVE_PHASES:
                raise RuntimeError("A manual action is already in progress for this frame.")
            if action in PLUG_ACTIONS and not self.cfg.get("wyze_mac"):
                raise RuntimeError("Pair a Wyze plug in frame settings first.")
            if self.power_suspended() and action not in ("power_on", "power_off", "wake", "sleep"):
                raise RuntimeError("Frame power is off or unknown. Power it on first.")
            now = time.time()
            last_reboot = self.state.get("last_reboot_requested_at",
                old.get("requested_at", 0) if old.get("action", "reboot") == "reboot" else 0)
            if action in ("reboot", "hard_reboot") and now - last_reboot < 120:
                raise RuntimeError("Please wait two minutes between reboot requests.")
            previous = deepcopy(self.state)
            self.state["manual"] = dict(
                id=request_id, action=action, phase="queued", requested_at=now,
                message="Waiting for plug" if action in PLUG_ACTIONS else "Waiting to connect")
            self.state["last_reboot_requested_at"] = last_reboot
            if action in ("reboot", "hard_reboot"):
                self.state["last_reboot_requested_at"] = now
            elif action not in POWER_ACTIONS:
                self.state["override"] = dict(
                    mode="day" if action in ("wake", "reset_app") else "night",
                    expires_at=next_boundary(datetime.now(self.zone),
                                             self.cfg["sleep" if action in ("wake", "reset_app") else "wake"]),
                )
            try:
                self.save()  # Persist intent before any device receives commands.
            except OSError:
                self.state = previous
                raise
            self.wakeup.set()
            LOG.info("%s: manual %s queued", self.cfg["name"], action)
        finally:
            self.mutex.release()

    def manual_tick(self):
        if not self.cfg.get("enabled", True):
            return False
        job = self.state.get("manual", {})
        if job.get("phase") not in ACTIVE_PHASES:
            return False
        action = job.get("action", "reboot")  # Jobs written before v1.2.0 are reboots.
        if (action in ("wake", "sleep", "reset_app") and self.job_expired(job)
                and job["phase"] not in SHUTDOWN_PHASES):
            job.update(phase="cancelled", message="The next schedule event passed; action cancelled.")
            self.save()
            return False
        # Bounded job, including unreachable frames. Never retry reboot itself.
        preparing_cycle = action == "hard_reboot" and "powered_on_at" not in job
        restoring_power = action == "hard_reboot" and (
            job["phase"] in POWER_PHASES or preparing_cycle and job["phase"] in SHUTDOWN_PHASES)
        connecting_wake = action == "wake" and job["phase"] == "boot_wait"
        started_at = job.get("shutdown_started_at", job.get("powered_on_at", job["requested_at"]))
        if time.time() - started_at > 900 and not restoring_power and not connecting_wake:
            job.update(phase="failed", message="Timed out after 15 minutes. Check frame connectivity and the configured action.")
            self.save()
            self.job_status(job, "failed", error=job["message"])
            return True
        try:
            if action in ("wake", "sleep") or (job["phase"] in SHUTDOWN_PHASES and not preparing_cycle):
                if not self.cfg.get("wyze_mac"):
                    job.update(phase="failed", message="Pair a Wyze plug in frame settings to use wake and sleep.")
                    self.save()
                    self.job_status(job, "failed", error=job["message"])
                    LOG.error("%s: %s", self.cfg["name"], job["message"])
                elif action == "wake":
                    self.wake_tick(job)
                else:
                    self.sleep_tick(job)
                return True
            if action in POWER_ACTIONS and self.power_tick(job):
                return True
            if self.power_suspended():
                return True
            self.adb.connect()
            if action == "reset_app":
                if self.job_expired(job):
                    job.update(phase="cancelled", message="The next schedule event passed; app reset cancelled.")
                    self.save()
                    return False
                if not job.get("cache_trimmed"):
                    self.trim_device_caches()
                    job.update(cache_trimmed=True, phase="starting",
                               message="Cache trim completed; restarting ImmichFrame")
                    self.save()
                    if self.job_expired(job):
                        job.update(phase="cancelled", message="The next schedule event passed; app reset cancelled.")
                        self.save()
                        return False
                self.launch()
                self.finish_job(job, "App reset completed: device cache trim finished and ImmichFrame launched")
                return True
            boot_id = self.adb.boot_id()
            if action == "reboot" and job["phase"] == "queued":
                job.update(phase="rebooting", previous_boot_id=boot_id,
                           message="Reboot requested; waiting for the frame")
                self.save()  # Journal before sending, just like the scheduled path.
                self.adb.run("reboot", timeout=60)
                return True
            if action == "reboot" and boot_id == job["previous_boot_id"]:
                raise RuntimeError("Waiting for the frame to reboot; no duplicate reboot will be sent")
            if self.adb.shell("getprop", "sys.boot_completed") != "1":
                raise RuntimeError("Waiting for Android to finish booting")
            if job.get("ready_boot_id") != boot_id:
                self.adb.shell("setprop", "service.bootanim.exit", "1")
                job.update(phase="starting", ready_boot_id=boot_id,
                           ready_at=time.time() + self.cfg["boot_delay_seconds"],
                           message="Android is ready; allowing the frame to settle")
                self.save()
            if time.time() < job["ready_at"]:
                return True
            now = datetime.now(self.zone)
            mode = "day" if self.schedule_event(now)[0] == "wake" else "night"
            override = self.active_override(now)
            mode = override.get("mode", mode if self.scheduled else "day")
            if mode == "night":
                if not self.cfg.get("wyze_mac"):
                    raise RuntimeError("Pair a Wyze plug to shut down the frame for sleep.")
                self.sleep_tick(job)
                return True
            self.launch()
            self.finish_job(job, "Power on completed and ImmichFrame launched" if action == "power_on"
                            else "Reboot completed and ImmichFrame launched")
        except Exception as exc:
            job["message"] = str(exc)[:600]
            self.save()
            self.job_status(job, "waiting", error=job["message"])
            LOG.warning("%s manual %s: %s", self.cfg["name"], action, exc)
        return True

    def power_tick(self, job):
        """Advance a durable power operation, gracefully shutting down for a cycle."""
        action = job["action"]
        if action == "hard_reboot" and job["phase"] in ("queued",) + SHUTDOWN_PHASES:
            if not self.shutdown_tick(job):
                return True
            self.state["power"] = "unknown"
            job.update(phase="power_wait", power_on_at=time.time() + 30,
                       message="Switching plug off; power will be restored after 30 seconds")
            self.save()  # Never repeat the off command after an ambiguous response or crash.
            try:
                POWER.set_power(self.cfg["wyze_mac"], False)
                self.state["power"] = "off"
                job["off_confirmed"] = True
                job["message"] = "Plug powered off; waiting 30 seconds before restoring power"
            finally:
                # Measure from command completion, including a failed/ambiguous response.
                job["power_on_at"] = time.time() + 30
                self.save()
            self.status("hard_reboot_power_off")
            return True
        if job["phase"] == "queued":
            job["phase"] = "power_on_pending" if action == "power_on" else "power_off_pending"
            self.save()
        if job["phase"] == "power_off_pending":
            # A lost response can mean the plug switched off. Suspend all device
            # work before sending, and safely repeat this idempotent command.
            self.state.update(power="unknown", power_paused=True)
            self.save()
            POWER.set_power(self.cfg["wyze_mac"], False)
            self.state["power"] = "off"
            self.state.pop("override", None)
            job.update(phase="completed", completed_at=time.time(),
                       message="Plug powered off; monitoring and scheduling paused until power on.")
            self.save()
            self.status("powered_off")
            return True
        if job["phase"] == "power_wait":
            if time.time() < job["power_on_at"]:
                return True
            job.update(phase="power_on_pending", message="Restoring plug power")
            self.save()
        if job["phase"] == "power_on_pending":
            POWER.set_power(self.cfg["wyze_mac"], True)
            self.state["power"] = "on"
            job.update(phase="rebooting", powered_on_at=time.time(),
                       message="Plug powered on; waiting for Android to boot")
            self.state["power_paused"] = False
            # Boot recovery gets a fresh timeout after a potentially long outage.
            job.pop("shutdown_started_at", None)
            self.save()
            self.status("powered_on")
            return True
        return False

    def snapshot(self):
        # Atomic files keep page loads independent of slow ADB calls.
        state = read_json(self.path, {})
        status = read_json(self.status_path, {})
        job = state.get("manual", {})
        now = datetime.now(self.zone)
        override = self.active_override(now, state)
        display_override = dict(override)
        if override and self.scheduled:
            display_override["until"] = datetime.fromtimestamp(override["expires_at"], self.zone).isoformat()
        mode = "day" if self.schedule_event(now)[0] == "wake" else "night"
        return {**self.cfg, "mode": override.get("mode", mode),
                "status": status, "job": job, "override": display_override,
                "busy": job.get("phase") in ACTIVE_PHASES,
                "power": state.get("power", "untracked"),
                "power_suspended": self.power_suspended(state),
                "schedule_paused": self.schedule_paused(state)}

    def work(self):
        while not STOP.is_set() and not self.stopped.is_set():
            self.wakeup.clear()
            with self.mutex:
                if self.stopped.is_set():
                    return
                try:
                    self.tick()
                except Exception as exc:
                    LOG.warning("%s: %s", self.cfg["name"], exc)
                    try:
                        self.status("error", error=str(exc))
                    except OSError:
                        LOG.exception("Cannot write frame status")
            self.wakeup.wait(None if not self.cfg.get("enabled", True) else
                             5 if self.state.get("manual", {}).get("phase") in ACTIVE_PHASES else 30)


class FrameRegistry:
    """Serialize configuration changes and coordinate live frame workers."""

    def __init__(self, config, data, frames=None):
        self.config = config
        self.data = data
        self.lock = threading.RLock()
        self.revision = secrets.token_hex(16)
        self.frames = {frame.cfg["name"]: frame for frame in (
            frames if frames is not None else
            [Frame(cfg, config["timezone"], config.get("enabled", False), data)
             for cfg in config["frames"]])}
        self.workers = {}
        self.running = False

    def snapshots(self):
        with self.lock:
            return [frame.snapshot() for frame in self.frames.values()]

    def configuration(self, name=None):
        with self.lock:
            return (deepcopy(self.frames[name].cfg) if name is not None else {}, self.revision)

    def log_page(self, name, page):
        with self.lock:
            frame = self.frames[name]
            records, has_older = frame_log.page(frame.log_path, page)
            if not records and page == 1 and (
                    not frame.log_path.exists() or frame.log_path.stat().st_size == 0):
                latest = read_json(frame.status_path, {})
                records = [latest] if latest else []
            return deepcopy(frame.cfg), records, has_older

    def clear_log(self, name):
        with self.lock:
            frame = self.frames[name]
            if not frame.mutex.acquire(blocking=False):
                raise RuntimeError("Frame is busy. Try again shortly.")
            try:
                frame_log.clear(frame.log_path)
            finally:
                frame.mutex.release()

    def request_reboot(self, name, token, mode="soft"):
        with self.lock:
            if mode == "soft":
                self.frames[name].request_reboot(token)
            else:
                self.frames[name].request_reboot(token, mode)

    def request_action(self, name, action, token):
        with self.lock:
            self.frames[name].request_action(action, token)

    def request_all(self, action, token):
        if action not in ("wake", "sleep"):
            raise ValueError("Invalid global display action")
        accepted, errors = [], {}
        with self.lock:
            for name, frame in self.frames.items():
                if not frame.cfg.get("enabled", True):
                    continue
                try:
                    frame.request_action(action, token)
                except RuntimeError as exc:
                    errors[name] = str(exc)
                except OSError:
                    LOG.exception("Cannot save manual %s request for %s", action, name)
                    errors[name] = "Could not save the request. Check the data directory and try again."
                else:
                    accepted.append(name)
        return accepted, errors

    def _start(self, frame):
        def run():
            # A newly added worker must not send commands before its config is saved.
            with self.lock:
                active = not frame.stopped.is_set()
            if active:
                frame.work()
        worker = threading.Thread(target=run, name=frame.cfg["name"], daemon=True)
        worker.start()
        return worker

    def start(self):
        with self.lock:
            self.running = True
            for frame in self.frames.values():
                self.workers[frame.cfg["name"]] = self._start(frame)

    def healthy(self):
        with self.lock:
            return all(worker.is_alive() for worker in self.workers.values())

    def change(self, name, values, revision):
        """Add (name=None), remove (values=None), or edit; persist before applying."""
        with self.lock:
            if revision != self.revision:
                raise RuntimeError("Frame settings changed in another session. Reload this page and try again.")
            frame = self.frames[name] if name is not None else None
            candidates = [values if key == name else existing.cfg
                          for key, existing in self.frames.items()
                          if key != name or values is not None]
            if name is None:
                candidates.append(values)
            validated = validate_config({**self.config, "frames": candidates})["frames"]
            cfg = next((cfg for cfg in validated if values is not None and cfg["name"] == values["name"]), None)
            if frame is not None and not frame.mutex.acquire(blocking=False):
                raise RuntimeError("Frame is busy. Try again shortly.")
            added = None
            try:
                if frame is not None:
                    job = frame.state.get("manual", {})
                    if (job.get("phase") in SHUTDOWN_PHASES or
                            job.get("action") in PLUG_ACTIONS and job.get("phase") in ACTIVE_PHASES):
                        raise RuntimeError("Wait for the power action to finish before changing this frame.")
                    if (cfg is not None and frame.power_suspended()
                            and cfg.get("wyze_mac") != frame.cfg.get("wyze_mac")):
                        raise RuntimeError("Power the frame on before changing or removing its Wyze plug pairing.")
                if (frame is not None and frame.state.get("manual", {}).get("phase") in ACTIVE_PHASES
                        and (cfg is None or cfg.get("enabled", True))):
                    raise RuntimeError("Wait for the manual action to finish before editing or removing this frame.")
                if frame is None:
                    added = Frame(cfg, self.config["timezone"], self.config.get("enabled", False), self.data)
                    worker = self._start(added) if self.running else None
                elif cfg is not None and cfg["name"] != name:
                    frame_log.copy(frame.log_path, self.data / (cfg["name"] + ".log.jsonl"))
                    # Copy the journal first, so a crash cannot lose reboot history.
                    # Retain old files, also when removing a frame, for recovery.
                    atomic_json(self.data / (cfg["name"] + ".state.json"), frame.state)
                    atomic_json(self.data / (cfg["name"] + ".status.json"),
                                read_json(frame.status_path, {}))
                atomic_json(self.data / "frames.json", validated)
                if added is not None:
                    self.frames[cfg["name"]] = added
                    if worker is not None:
                        self.workers[cfg["name"]] = worker
                elif cfg is None:
                    frame.stopped.set()
                    frame.wakeup.set()
                    del self.frames[name]
                    self.workers.pop(name, None)
                else:
                    frame.cfg = cfg
                    frame.adb = ADB(cfg["address"], cfg.get("run_as_root", False))
                    frame.path = self.data / (cfg["name"] + ".state.json")
                    frame.status_path = self.data / (cfg["name"] + ".status.json")
                    frame.log_path = self.data / (cfg["name"] + ".log.jsonl")
                    if not cfg["enabled"]:
                        job = frame.state.get("manual", {})
                        if job.get("phase") in ACTIVE_PHASES:
                            job.update(phase="cancelled", message="Frame management disabled.")
                        frame.state.pop("override", None)
                        frame.save()
                    frame.wakeup.set()
                    self.frames = {cfg["name"] if key == name else key: existing
                                   for key, existing in self.frames.items()}
                    if name in self.workers:
                        worker = self.workers.pop(name)
                        worker.name = cfg["name"]
                        self.workers[cfg["name"]] = worker
                self.config["frames"] = validated
                self.revision = secrets.token_hex(16)
            except Exception:
                if added is not None:
                    added.stopped.set()
                    added.wakeup.set()
                raise
            finally:
                if frame is not None:
                    frame.mutex.release()


def health():
    heartbeat = DATA / "heartbeat"
    return 0 if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 90 else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "validate", "status", "health"], nargs="?", default="run")
    command = parser.parse_args().command
    if command == "health":
        return health()
    config = load_config()
    if command == "status":
        print(json.dumps([read_json(DATA / (cfg["name"] + ".status.json"), {})
                          for cfg in config["frames"]], indent=2))
        return 0
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
    # works with scheduling disabled.
    registry = FrameRegistry(config, DATA)
    registry.start()
    if config["web"].get("enabled", False):
        from webui import create_app
        from waitress import create_server
        server = create_server(create_app(config, registry, DATA), host="0.0.0.0", port=8080,
                               threads=4, max_request_body_size=8192, connection_limit=32)
        worker = threading.Thread(target=server.run, name="web", daemon=True)
        worker.start()
        workers.append(worker)
        LOG.info("Web interface listening on port 8080")
    if not config.get("enabled", False):
        LOG.warning("Scheduling disabled. Authorize ADB and test frames, then set SCHEDULE_ENABLED=true and recreate the container.")
    while not STOP.is_set():
        if not registry.healthy() or any(not worker.is_alive() for worker in workers):
            raise RuntimeError("A frame worker exited unexpectedly")
        (DATA / "heartbeat").touch()
        STOP.wait(15)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
