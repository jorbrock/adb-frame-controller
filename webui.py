"""One local account, signed session cookies, and per-frame reboot forms."""
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


def create_app(config, frames, data):
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
    by_name = {frame.cfg["name"]: frame for frame in frames}
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
        return render_template("index.html", frames=[frame.snapshot() for frame in frames],
                               timezone=config["timezone"], scheduled=config.get("enabled", False))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.post("/frames/<name>/reboot")
    def reboot(name):
        frame = by_name.get(name)
        if frame is None:
            abort(404)
        token = request.form.get("request_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            abort(400, "Invalid request ID")
        try:
            frame.request_reboot(token)
        except RuntimeError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Reboot requested for {name}. Progress will appear below.", "success")
        return redirect(url_for("index"), code=303)

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
