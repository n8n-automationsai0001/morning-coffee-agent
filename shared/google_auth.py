#!/usr/bin/env python3
"""
Shared Google OAuth2 authentication for all Google Workspace skills.

Usage in any skill script:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / '_shared'))
    from google_auth import get_credentials

    from googleapiclient.discovery import build
    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)
"""

import os
import sys
import base64
import json
from pathlib import Path

# ── Load .env (walk up directory tree to find project root) ─────────────────
def _load_env():
    current = Path(__file__).resolve().parent
    for _ in range(10):
        env_path = current / '.env'
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, value = line.partition('=')
                        os.environ.setdefault(key.strip(), value.strip())
            return current
        current = current.parent
    return None

_load_env()

# ── Auto-install dependencies ─────────────────────────────────────────────────
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    import subprocess
    subprocess.check_call([
        sys.executable, '-m', 'pip', 'install',
        'google-auth', 'google-auth-oauthlib', 'google-api-python-client'
    ])
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow


# ── CCR SSL workaround ────────────────────────────────────────────────────────
# CCR's security proxy does TLS inspection with a self-signed CA that isn't
# trusted by httplib2's bundled cacerts.txt (nor reliably by the system
# bundle). Disable cert validation for googleapis calls when running in the
# cloud environment; the proxy layer still enforces security.
if os.environ.get('CLAUDE_CODE_REMOTE') == 'true':
    try:
        import httplib2
        _orig_httplib2_init = httplib2.Http.__init__

        def _patched_httplib2_init(self, *args, **kwargs):
            kwargs.setdefault('disable_ssl_certificate_validation', True)
            _orig_httplib2_init(self, *args, **kwargs)

        httplib2.Http.__init__ = _patched_httplib2_init
    except ImportError:
        pass

    try:
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _orig_requests_request = requests.Session.request

        def _patched_requests_request(self, *args, **kwargs):
            kwargs.setdefault('verify', False)
            return _orig_requests_request(self, *args, **kwargs)

        requests.Session.request = _patched_requests_request
    except ImportError:
        pass

# ── Scopes — all Google Workspace services ────────────────────────────────────
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/presentations',
    'https://www.googleapis.com/auth/forms.body',
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube',
]

ACCOUNT = 'aiautomations0001@gmail.com'
TOKEN_PATH = Path(__file__).parent / 'token.json'


def get_credentials():
    """Get or refresh Google OAuth2 credentials covering all Workspace services.

    Loads credentials in this order:
      1. GOOGLE_TOKEN_JSON_B64 env var (base64-encoded token.json) — used on CCR cloud runs
      2. TOKEN_PATH file — used on local runs
    """
    creds = None

    token_b64 = os.environ.get('GOOGLE_TOKEN_JSON_B64')
    if token_b64:
        info = json.loads(base64.b64decode(token_b64).decode('utf-8'))
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    elif TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_id = os.environ.get('GOOGLE_CLIENT_ID')
            client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')

            if not client_id or not client_secret:
                print("Error: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env")
                sys.exit(1)

            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token"
                }
            }

            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            print(f"\nOpening browser for Google Workspace authorization...")
            print(f"Account: {ACCOUNT}")
            print("Requesting access to: Calendar, Gmail, Drive, Docs, Sheets, Slides, Forms, YouTube\n")
            creds = flow.run_local_server(port=8080)

        # Persist only when running locally (CCR filesystem is ephemeral;
        # refresh tokens rarely rotate so the env-var seed stays valid).
        if not token_b64:
            TOKEN_PATH.write_text(creds.to_json())

    return creds
