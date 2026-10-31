#!/usr/bin/env python3
"""
Armadillo auth module.

Handles Azure AD B2C login via Playwright + SMS MFA.
Token caching and refresh are handled by the HA component.
"""

import os
import time
from pathlib import Path

ADDON_MODE = os.getenv("ADDON_MODE", "false").lower() == "true"
_DATA_DIR  = Path(os.getenv("DATA_DIR", "/data"))

PORTAL_URL = "https://spot-on-energy.arm-hub.com/"


# ── Addon-mode MFA helper (file IPC with webui.py) ───────────────────────────

def _wait_for_mfa_file(timeout: int = 600) -> str:
    pending   = _DATA_DIR / "mfa_pending"
    code_file = _DATA_DIR / "mfa_code"

    pending.write_text("Check your phone for an SMS code.")
    print("→ Waiting for MFA code (open the add-on panel in HA)...")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if code_file.exists():
            code = code_file.read_text().strip()
            code_file.unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
            print("✓ MFA code received")
            return code
        time.sleep(2)

    pending.unlink(missing_ok=True)
    raise RuntimeError("Timed out waiting for MFA code (10 min limit).")


# ── Playwright browser login ──────────────────────────────────────────────────

def get_token_data(email: str, password: str) -> dict:
    """
    Perform a full B2C browser login and return
    {access_token, refresh_token, expires_on}.
    Token caching and refresh are the caller's responsibility.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    print(f"\n🌐 Launching browser for B2C login ({email})...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=ADDON_MODE, slow_mo=100)
        context = browser.new_context()
        page    = context.new_page()

        _intercepted: dict = {}

        def _on_response(response):
            if "/oauth2/v2.0/token" in response.url:
                try:
                    body = response.json()
                    if body.get("access_token"):
                        print("✓ Token intercepted from B2C network response")
                        _intercepted.update(body)
                    else:
                        print(f"⚠️  B2C token endpoint returned status={response.status} body={str(body)[:300]}")
                except Exception as e:
                    print(f"⚠️  B2C token endpoint status={response.status} (could not parse body: {e})")

        page.on("response", _on_response)

        print("→ Navigating to portal...")
        for attempt in range(3):
            try:
                page.goto(PORTAL_URL, wait_until="load", timeout=30000)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"   Retrying ({attempt + 1}/3): {e}")
                page.wait_for_timeout(3000)

        print("→ Waiting for B2C login page...")
        try:
            page.wait_for_selector("input#signInName", timeout=60000)
        except PWTimeout:
            raise RuntimeError("B2C login page did not appear")

        detected_policy = page.evaluate("() => (typeof SETTINGS !== 'undefined' && SETTINGS.hosts) ? SETTINGS.hosts.policy : null")
        if detected_policy:
            print(f"→ Detected B2C policy: {detected_policy}")

        page.locator("input#signInName").fill(email)
        page.locator("input#password").fill(password)
        page.locator("button#next").click()

        print("→ Waiting for MFA page...")
        try:
            page.wait_for_selector("input#verificationCode", timeout=20000)

            error_el = page.locator("#errorMessage")
            if error_el.is_visible():
                msg = error_el.inner_text().strip()
                if "limit" in msg.lower() or "back" in msg.lower():
                    browser.close()
                    raise RuntimeError(f"B2C SMS rate limit: {msg}")

            print("📱 MFA screen detected.")
            otp = _wait_for_mfa_file() if ADDON_MODE else input("   Enter SMS code: ").strip()
            inp = page.locator("input#verificationCode")
            inp.fill(otp)
            # Try the dedicated verify button first; fall back to Enter if it times out
            try:
                page.locator("button#verifyCode").click(timeout=10000)
                print("✓ Clicked verifyCode button")
            except PWTimeout:
                print("⚠️  verifyCode button not clickable — submitting via Enter")
                inp.press("Enter")

        except PWTimeout:
            print("⚠️  No MFA prompt — continuing")

        print("→ Waiting for B2C token response...")
        deadline = time.time() + 30
        while not _intercepted.get("access_token") and time.time() < deadline:
            page.wait_for_timeout(500)

        browser.close()

    if not _intercepted.get("access_token"):
        raise RuntimeError(
            "Login completed but no token received from B2C. "
            "The /oauth2/v2.0/token request may not have fired."
        )

    return {
        "access_token":             _intercepted["access_token"],
        "refresh_token":            _intercepted.get("refresh_token"),
        "expires_on":               _intercepted.get("expires_on") or
                                    (time.time() + _intercepted.get("expires_in", 3600)),
        "refresh_token_expires_in": _intercepted.get("refresh_token_expires_in"),
        "b2c_policy":               detected_policy or _B2C_POLICY,
    }
