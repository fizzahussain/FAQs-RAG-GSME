"""Supabase Auth adapter. The Auth server verifies tokens; metadata cannot grant roles."""

import os
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .access import Principal
from .identity import InvalidIdentity, IdentityUnavailable


class SupabaseAuth:
    def __init__(self):
        self.url = os.getenv("GSME_SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("GSME_SUPABASE_PUBLISHABLE_KEY", "")
        parsed = urlsplit(self.url)
        if self.url and (parsed.scheme != "https" or not parsed.hostname or parsed.path
                         or parsed.username or parsed.query or parsed.fragment):
            raise ValueError("Configure an HTTPS Supabase project origin")

    @property
    def enabled(self):
        return bool(self.url and self.key)

    def _request(self, method, path, *, token=None, body=None):
        if not self.enabled:
            raise IdentityUnavailable("Supabase Auth is not configured")
        headers = {"apikey": self.key}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                result = client.request(method, self.url + "/auth/v1/" + path, headers=headers, json=body)
            if result.status_code == 429 or result.status_code >= 500:
                raise IdentityUnavailable("Sign-in service temporarily unavailable")
            if result.status_code >= 400:
                raise InvalidIdentity("Sign-in could not be verified")
            data = result.json()
            if not isinstance(data, dict):
                raise IdentityUnavailable("Invalid sign-in service response")
            return data
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, InvalidIdentity):
                raise
            raise IdentityUnavailable("Sign-in service unavailable") from None

    def principal(self, token):
        if not token or len(token) > 16000:
            raise InvalidIdentity("Sign-in required")
        user = self._request("GET", "user", token=token)
        if not user.get("email_confirmed_at") or user.get("is_anonymous"):
            raise InvalidIdentity("Verified company sign-in required")
        try:
            subject = str(UUID(user["id"]))
        except (ValueError, KeyError, TypeError):
            raise InvalidIdentity("Invalid user identity") from None
        return Principal("supabase:" + subject)

    def send_code(self, email):
        # Users are invited in Supabase first; public self-registration is off.
        try:
            self._request("POST", "otp", body={"email": email, "create_user": False})
        except InvalidIdentity:
            # Keep the public response identical for invited/unknown addresses.
            return

    def verify_code(self, email, code):
        result = self._request("POST", "verify", body={"email": email, "token": code, "type": "email"})
        token = result.get("access_token")
        if not token:
            raise InvalidIdentity("Code expired or invalid")
        self.principal(token)
        return {"access_token": token, "expires_in": result.get("expires_in", 3600)}
