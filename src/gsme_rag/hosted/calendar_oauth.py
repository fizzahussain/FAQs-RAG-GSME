"""Run locally once to authorize the dedicated organizer's Google calendar.

Create a Google Cloud Desktop OAuth client and enable Calendar API first.
Set GSME_CALENDAR_CLIENT_ID and GSME_CALENDAR_CLIENT_SECRET locally, then run
python -m gsme_rag.hosted.calendar_oauth. Never run this interactive tool on Render.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
from base64 import urlsafe_b64encode
from hashlib import sha256
import os
from pathlib import Path
import secrets
from urllib.parse import urlencode, urlsplit, parse_qs
import webbrowser

import httpx


def main():
    from dotenv import load_dotenv
    load_dotenv()
    client_id = os.environ["GSME_CALENDAR_CLIENT_ID"]
    client_secret = os.environ["GSME_CALENDAR_CLIENT_SECRET"]
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain temporary authorization codes.

        def do_GET(self):
            params = parse_qs(urlsplit(self.path).query)
            valid = (urlsplit(self.path).path == "/callback" and
                     secrets.compare_digest(params.get("state", [""])[0], state))
            if not valid:
                self.send_error(400)
                return
            result["code"] = params.get("code", [""])[0]
            result["done"] = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"You can close this tab and return to your terminal.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
            "scope": "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy",
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge":challenge,"code_challenge_method":"S256"})
        print("Sign in with the dedicated booking organizer account in the browser.")
        print(url)
        webbrowser.open(url)
        # A fixed total deadline prevents stray requests from extending the wait.
        import time
        deadline = time.monotonic() + 180
        while not result.get("done") and time.monotonic() < deadline:
            server.timeout = min(5, max(0.1, deadline-time.monotonic()))
            server.handle_request()
    if not result.get("code"):
        raise SystemExit("Calendar authorization cancelled or timed out")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id, "client_secret": client_secret, "code": result["code"],
            "redirect_uri": redirect, "grant_type": "authorization_code","code_verifier":verifier})
    if response.status_code != 200 or not response.json().get("refresh_token"):
        raise SystemExit("Authorization failed; check the Google consent screen and client settings")
    directory = Path(os.getenv("GSME_DATA_DIR", "./data"))
    directory.mkdir(parents=True, exist_ok=True)
    token_file = directory / "calendar-refresh-token.txt"
    # Refuse to overwrite an existing organizer connection silently.
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(response.json()["refresh_token"])
    print(f"Saved credential privately to {token_file.resolve()}.")
    print("Local: set GSME_CALENDAR_REFRESH_TOKEN_FILE to this path.")
    print("Render: copy the file's value into the secret GSME_CALENDAR_REFRESH_TOKEN setting.")


if __name__ == "__main__":
    main()
