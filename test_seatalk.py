#!/usr/bin/env python3
"""
Minimal SeaTalk connectivity test.

Tests: credential loading -> token fetch -> email resolve -> send private message.
Run this on the online service to verify SeaTalk auth works end-to-end.

Usage:
    python3 test_seatalk.py
"""

import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

HOST = "https://openapi.seatalk.io"


def post(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def main():
    # 1. Load credentials
    cred_path = CONFIG_DIR / "seatalk_credentials.json"
    if not cred_path.exists():
        sys.exit(f"[FAIL] Not found: {cred_path}")
    with open(cred_path) as f:
        st = json.load(f).get("seatalk_open_platform", {})
    app_id = st.get("app_id")
    app_secret = st.get("app_secret")
    email = st.get("self_email", "youwei.wang@shopee.com")
    if not app_id or not app_secret:
        sys.exit("[FAIL] Missing app_id or app_secret in credentials")
    print(f"[OK] Credentials loaded (app_id={app_id[:8]}...)")

    # 2. Get token
    resp = post(f"{HOST}/auth/app_access_token",
                {"app_id": app_id, "app_secret": app_secret})
    if resp.get("code") != 0:
        sys.exit(f"[FAIL] Token request failed: {resp}")
    token = resp["app_access_token"]
    print(f"[OK] Token obtained (expires in {resp.get('expire', '?')}s)")

    # 3. Resolve email -> employee_code
    resp = post(f"{HOST}/contacts/v2/get_employee_code_with_email",
                {"emails": [email]},
                headers={"Authorization": f"Bearer {token}"})
    if resp.get("code") != 0:
        sys.exit(f"[FAIL] Email resolve failed: {resp}")
    emp_code = None
    for emp in resp.get("employees", []):
        if emp.get("email") == email:
            emp_code = emp.get("employee_code")
    if not emp_code:
        sys.exit(f"[FAIL] Could not resolve {email}")
    print(f"[OK] Resolved {email} -> {emp_code}")

    # 4. Send test message
    msg = f"SeaTalk connectivity test OK\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    resp = post(f"{HOST}/messaging/v2/single_chat",
                {
                    "employee_code": emp_code,
                    "message": {"tag": "text", "text": {"content": msg, "format": 1}},
                },
                headers={"Authorization": f"Bearer {token}"})
    if resp.get("code") != 0:
        sys.exit(f"[FAIL] Send failed: {resp}")
    print(f"[OK] Message sent (msg_id={resp.get('message_id', '?')})")
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
