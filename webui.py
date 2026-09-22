"""Authenticated frame configuration and reboot controls."""
import argparse
from collections import OrderedDict
from datetime import timedelta
import getpass
import hmac
import json
import os
import re
import secrets
import threading
import time

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


def create_app(config, registry, data):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_NAME="frame_controller_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=config.get("web", {}).get("secure_cookie", False),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=8192,
    )
    attempts = OrderedDict()
    attempt_lock = threading.Lock()

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf, request_token=lambda: secrets.token_hex(16))

    @app.before_request
    def protect():
        if request.endpoint == "static":
            return
        auth_file = data / "web-auth.json"
        g.auth = json.loads(auth_file.read_text()) if auth_file.exists() else None
        if not g.auth:
            return render_template("setup.html"), 503
        g.logged_in = session.get("account_id") == g.auth["id"]
        if request.endpoint != "login" and not g.logged_in:
            return redirect(url_for("login"))
        if request.method == "POST":
            expected = session.get("csrf", "")
            supplied = request.form.get("csrf", "")
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                abort(400, "Your form expired. Reload the page and try again.")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.logged_in:
            return redirect(url_for("index"))
        error = None
        code = 200
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            now = time.monotonic()
            with attempt_lock:
                previous = [value for value in attempts.get(ip, []) if now - value < 300]
                if len(previous) >= 10:
                    return render_template("login.html", error="Too many attempts. Try again in five minutes."), 429
                attempts[ip] = previous + [now]
                attempts.move_to_end(ip)
                while len(attempts) > 1024:
                    attempts.popitem(last=False)
            valid_password = check_password_hash(g.auth["password_hash"], request.form.get("password", ""))
            valid_user = hmac.compare_digest(request.form.get("username", "").encode(), g.auth["username"].encode())
            if valid_password and valid_user:
                with attempt_lock:
                    attempts.pop(ip, None)
                session.clear()
                session["account_id"] = g.auth["id"]
                session.permanent = True
                csrf()
                return redirect(url_for("index"), code=303)
            error, code = "Incorrect username or password.", 401
        return render_template("login.html", error=error), code

    @app.get("/")
    def index():
        return render_template("index.html", frames=registry.snapshots(),
                               timezone=config["timezone"], scheduled=config.get("enabled", False))

    @app.get("/frames/<name>/log")
    def frame_log(name):
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            abort(400, "Invalid log page")
        if page < 1 or page > 1000000:
            abort(400, "Invalid log page")
        try:
            frame, entries, has_older = registry.log_page(name, page)
        except KeyError:
            abort(404)
        except OSError:
            app.logger.exception("Cannot read frame log")
            return "Could not read the frame log. Try again shortly.", 503
        return render_template("frame_log.html", frame=frame, entries=entries,
                               page=page, has_older=has_older)

    @app.route("/frames/<name>/log/clear", methods=["GET", "POST"])
    def clear_frame_log(name):
        frame, _ = configuration(name)
        if request.method == "POST":
            try:
                registry.clear_log(name)
            except KeyError:
                abort(404)
            except RuntimeError as exc:
                flash(str(exc), "error")
            except OSError:
                app.logger.exception("Cannot clear frame log")
                flash("Could not clear the log. Check the data directory and try again.", "error")
            else:
                flash(f"Log cleared for {name}.", "success")
            return redirect(url_for("frame_log", name=name), code=303)
        return render_template("frame_log_clear.html", frame=frame)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.post("/frames/<name>/reboot")
    def reboot(name):
        try:
            registry.configuration(name)
        except KeyError:
            abort(404)
        token = request.form.get("request_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            abort(400, "Invalid request ID")
        try:
            registry.request_reboot(name, token)
        except KeyError:
            abort(404)
        except RuntimeError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Reboot requested for {name}. Progress will appear below.", "success")
        return redirect(url_for("index"), code=303)

    @app.post("/frames/<name>/wake", defaults={"action": "wake"})
    @app.post("/frames/<name>/sleep", defaults={"action": "sleep"})
    @app.post("/frames/<name>/reset_app", defaults={"action": "reset_app"})
    def display_action(name, action):
        token = request.form.get("request_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            abort(400, "Invalid request ID")
        try:
            registry.request_action(name, action, token)
        except KeyError:
            abort(404)
        except RuntimeError as exc:
            flash(str(exc), "error")
        except OSError:
            app.logger.exception("Cannot save manual display request")
            flash("Could not save the request. Check the data directory and try again.", "error")
        else:
            flash(f"{action.replace('_', ' ').capitalize()} requested for {name}. Progress will appear below.", "success")
        return redirect(url_for("index"), code=303)

    @app.post("/frames/actions/wake", defaults={"action": "wake"})
    @app.post("/frames/actions/sleep", defaults={"action": "sleep"})
    def display_all(action):
        token = request.form.get("request_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            abort(400, "Invalid request ID")
        accepted, errors = registry.request_all(action, token)
        if accepted:
            flash(f"{action.capitalize()} requested for {len(accepted)} frame(s): "
                  + ", ".join(accepted) + ". Progress will appear below.", "success")
        for name, error in errors.items():
            flash(f"{name}: {error}", "error")
        if not accepted and not errors:
            flash("No frames configured. Add a frame first.", "error")
        return redirect(url_for("index"), code=303)

    def configuration(name):
        try:
            return registry.configuration(name)
        except KeyError:
            abort(404)

    def save_configuration(name, values, revision):
        try:
            registry.change(name, values, revision)
        except KeyError:
            abort(404)
        except ValueError as exc:
            return str(exc), 400
        except RuntimeError as exc:
            return str(exc), 409
        except OSError:
            app.logger.exception("Cannot save frame settings")
            return "Could not save settings. Check that the data directory is writable and has free space.", 503
        return None, 303

    @app.route("/frames/new", methods=["GET", "POST"])
    @app.route("/frames/<name>/edit", methods=["GET", "POST"])
    def edit_frame(name=None):
        values, revision = configuration(name)
        if name is None:
            values = dict(name="", address="", enabled=True, package="com.immichframe.immichframe",
                          component="com.immichframe.immichframe/.MainActivity",
                          wake="07:00", sleep="22:00", morning_action="reboot",
                          day_brightness=128, boot_delay_seconds=60, night_recheck_seconds=300)
        error, code = None, 200
        if request.method == "POST":
            revision = request.form.get("revision", "")
            # Keep unknown file-based options intact when editing a frame.
            for field in ("name", "address", "package", "component", "wake", "sleep", "morning_action",
                          "day_brightness", "boot_delay_seconds", "night_recheck_seconds"):
                values[field] = request.form.get(field, "").strip()
            enabled = request.form.get("enabled", "true")
            values["enabled"] = enabled == "true"
            if enabled not in ("true", "false"):
                error, code = "Frame enabled must be true or false.", 400
            parsed = dict(values)
            for field in ("day_brightness", "boot_delay_seconds", "night_recheck_seconds"):
                try:
                    parsed[field] = int(values[field])
                except ValueError:
                    error, code = f"{field} must be a whole number.", 400
                    break
            if error is None:
                error, code = save_configuration(name, parsed, revision)
            if error is None:
                flash(f"Settings saved for {parsed['name']}.", "success")
                return redirect(url_for("index"), code=303)
        return render_template("frame_form.html", frame=values, name=name, revision=revision,
                               timezone=config["timezone"], error=error), code

    @app.route("/frames/<name>/remove", methods=["GET", "POST"])
    def remove_frame(name):
        values, revision = configuration(name)
        error, code = None, 200
        if request.method == "POST":
            revision = request.form.get("revision", "")
            error, code = save_configuration(name, None, revision)
            if error is None:
                flash(f"{name} removed. The controller will no longer send commands to this frame.", "success")
                return redirect(url_for("index"), code=303)
        return render_template("frame_remove.html", frame=values, revision=revision, error=error), code

    return app


def set_password():
    parser = argparse.ArgumentParser(description="Create or replace the local web account")
    parser.add_argument("command", choices=["set-password"])
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    if not args.username.strip() or len(args.username) > 80:
        parser.error("Use a username of 1 to 80 characters")
    password = getpass.getpass("New password: ")
    if len(password) < 8:
        parser.error("Use at least 8 characters")
    if password != getpass.getpass("Repeat password: "):
        parser.error("Passwords did not match")
    from controller import DATA, atomic_json
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True)
    atomic_json(DATA / "web-auth.json", dict(
        id=secrets.token_hex(16), username=args.username,
        password_hash=generate_password_hash(password),
    ))
    print(f"Local account '{args.username}' saved. Existing sessions are invalidated; no restart needed.")


if __name__ == "__main__":
    set_password()
