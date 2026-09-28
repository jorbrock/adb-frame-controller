"""Shared, lazy Wyze session. Credentials and raw API errors never enter frame logs."""
import os
import re
import threading
import time


class PowerError(RuntimeError):
    pass


def normalize_mac(value):
    if not isinstance(value, str):
        raise ValueError("Wyze plug MAC must be text")
    value = value.strip().replace(":", "").replace("-", "").upper()
    if value and not re.fullmatch(r"(?:[0-9A-F]{12}|[0-9A-F]{16})", value):
        raise ValueError("Wyze plug MAC must contain 12 or 16 hexadecimal digits")
    return value


_sdk_configured = False


def configure_sdk():
    """Bound unattended SDK 2.3.8 calls without changing application-wide requests."""
    global _sdk_configured
    if _sdk_configured:
        return
    from wyze_sdk.service import auth_service
    from wyze_sdk.service.base import BaseServiceClient

    original = BaseServiceClient._do_request

    class TimedSession:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def send(self, request, **kwargs):
            kwargs["timeout"] = (10, 30)
            return self.session.send(request, **kwargs)

    def bounded_request(client, session, request):
        return original(client, TimedSession(session), request)

    def unattended_mfa(prompt):
        raise PowerError("Interactive Wyze MFA is unavailable. Configure WYZE_TOTP_KEY "
                         "or WYZE_ACCESS_TOKEN and WYZE_REFRESH_TOKEN.")

    # The pinned SDK ignores its timeout field and otherwise calls input() for MFA.
    BaseServiceClient._do_request = bounded_request
    auth_service.input = unattended_mfa
    _sdk_configured = True


class WyzePower:
    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.retry_at = 0
        self.devices = {}

    def _connect(self):
        from wyze_sdk import Client

        configure_sdk()
        token = os.environ.get("WYZE_ACCESS_TOKEN")
        if token:
            self.client = Client(token=token, refresh_token=os.environ.get("WYZE_REFRESH_TOKEN") or None)
            return
        required = ("WYZE_EMAIL", "WYZE_PASSWORD", "WYZE_KEY_ID", "WYZE_API_KEY")
        if not all(os.environ.get(key) for key in required):
            raise PowerError("Configure WYZE_EMAIL, WYZE_PASSWORD, WYZE_KEY_ID and WYZE_API_KEY, "
                             "or WYZE_ACCESS_TOKEN, in Docker Compose.")
        client = Client()
        client.login(email=os.environ["WYZE_EMAIL"], password=os.environ["WYZE_PASSWORD"],
                     key_id=os.environ["WYZE_KEY_ID"], api_key=os.environ["WYZE_API_KEY"],
                     totp_key=os.environ.get("WYZE_TOTP_KEY") or None)
        self.client = client

    def _switch(self, mac, on):
        plugs = self.client.plugs
        if mac not in self.devices:
            # Resolve the model and the API's original MAC, including its casing.
            for plug in plugs.list():
                try:
                    key = normalize_mac(plug.mac)
                except ValueError:
                    continue
                self.devices[key] = (plug.mac, plug.product.model)
        if mac not in self.devices:
            raise PowerError("Wyze plug was not found in this account. Check its device MAC and sharing permissions.")
        device_mac, model = self.devices[mac]
        command = plugs.turn_on if on else plugs.turn_off
        command(device_mac=device_mac, device_model=model)

    def set_power(self, mac, on):
        from wyze_sdk.errors import WyzeApiError

        with self.lock:
            if time.monotonic() < self.retry_at:
                raise PowerError("Wyze is temporarily unavailable; retrying shortly. Check credentials and connectivity.")
            try:
                if self.client is None:
                    self._connect()
                try:
                    self._switch(mac, on)
                except WyzeApiError as exc:
                    response = exc.response if isinstance(exc.response, dict) else {}
                    if (str(response.get("code")) != "2001"
                            and response.get("msg") != "AccessTokenError"):
                        raise
                    self.client.refresh_token()
                    self._switch(mac, on)
            except Exception as exc:
                self.retry_at = time.monotonic() + 60
                # SDK exceptions may contain full API responses and credentials.
                if isinstance(exc, PowerError):
                    raise
                raise PowerError("Wyze power command failed. Check credentials, MFA configuration, "
                                 "plug connectivity and account access.") from None


POWER = WyzePower()
