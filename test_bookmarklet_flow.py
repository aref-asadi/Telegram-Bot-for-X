"""End-to-end verification for the bookmarklet auth flow.

Run with:  python test_bookmarklet_flow.py
"""
import asyncio
import json
import os
import re
import sys

os.environ["BOT_TOKEN"] = "123456789:AATestTokenForUnitTestsOnly"
os.environ["PUBLIC_BASE_URL"] = "https://bot.example.com"
os.environ["POLL_INTERVAL_SECONDS"] = "180"

import src.twitter_client as tc

real_verify = tc.verify_credentials  # keep the real one for the live probe


async def fake_verify(auth_token: str, ct0: str) -> bool:
    return auth_token != "0" * 40


tc.verify_credentials = fake_verify

from src.web_server import (  # noqa: E402
    create_app,
    pop_pending_auth,
    set_bot_username,
)


async def web_tests() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    set_bot_username("TestBookmarkBot")
    client = TestClient(TestServer(create_app()))
    await client.start_server()

    # 1) Installer page renders with a working single-line bookmarklet.
    resp = await client.get("/bookmarklet?c=42")
    assert resp.status == 200, resp.status
    page = await resp.text()
    for leftover in ("__BOOKMARKLET_HREF__", "__STATUS__", "__BASE_URL__",
                     "__BOT_USERNAME__", "__CHAT_ID__"):
        assert leftover not in page, f"placeholder {leftover} not substituted"
    assert 'href="javascript:' in page, "bookmarklet href missing"
    js_attr = page.split('href="javascript:', 1)[1].split('"', 1)[0]
    assert "\n" not in js_attr, "javascript: href contains raw newlines"
    # Quotes inside the javascript: href are HTML-escaped - unescape first.
    unescaped = page.replace("&quot;", '"').replace("&amp;", "&")
    assert '"https://bot.example.com"' in unescaped, "base URL not baked into JS"
    assert '"/auth"' in unescaped, "POST path not found in JS"
    assert "https://t.me/" in unescaped, "deep-link code missing"
    assert "?start=auth_" in unescaped, "start parameter missing"
    print("PASS 1: /bookmarklet page renders with substituted bookmarklet")

    # 2) CORS pre-flight for x.com.
    resp = await client.options(
        "/auth",
        headers={"Origin": "https://x.com",
                 "Access-Control-Request-Method": "POST"},
    )
    assert resp.status in (200, 204), resp.status
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    print("PASS 2: OPTIONS /auth preflight with CORS")

    # 3) Invalid JSON body.
    resp = await client.post(
        "/auth", data="{nope", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400, resp.status
    print("PASS 3: POST /auth rejects non-JSON body")

    # 4) Bad cookie formats.
    resp = await client.post("/auth", json={"auth_token": "short", "ct0": "x"})
    assert resp.status == 400, resp.status
    print("PASS 4: POST /auth rejects malformed cookies")

    # 5) Live-verified rejection (fake verify says no).
    resp = await client.post(
        "/auth", json={"auth_token": "0" * 40, "ct0": "a" * 32, "chat_id": 42}
    )
    assert resp.status == 401, resp.status
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    print("PASS 5: POST /auth returns 401 for rejected cookies")

    # 6) Success path: code minted, bound to chat, single-use.
    resp = await client.post(
        "/auth", json={"auth_token": "a" * 40, "ct0": "b" * 32, "chat_id": 42}
    )
    assert resp.status == 200, resp.status
    data = await resp.json()
    assert data["ok"] is True and re.fullmatch(r"[0-9a-f]{12}", data["code"]), data
    creds = pop_pending_auth(data["code"])
    assert creds is not None, "code not stored"
    assert creds["chat_id"] == 42 and creds["auth_token"] == "a" * 40
    assert pop_pending_auth(data["code"]) is None, "code must be single-use"
    print("PASS 6: POST /auth mints a bound single-use handover code")

    # 7) Health + root still fine.
    assert (await client.get("/health")).status == 200
    assert (await client.get("/")).status == 200
    print("PASS 7: /health and / still respond")

    await client.close()


async def main() -> int:
    await web_tests()

    # --- wizard parser / guide / handler wiring ------------------------------
    from src.bot_wizard import (
        BOOKMARKLET,
        CT0,
        GROK_KEY,
        SET_GROK_KEY,
        _bookmarklet_guide,
        _match_handover_code,
        _parse_credentials_payload,
        get_conversation_handler,
    )

    at, ct = "a" * 40, "b" * 32
    cases = {
        "clipboard payload": f"auth_token={at}\nct0={ct}",
        "semicolon cookies": f"auth_token={at}; ct0={ct}",
        "json payload": json.dumps({"auth_token": at, "ct0": ct}),
        "bare two lines": f"{at}\n{ct}",
        "quoted json": '{"auth_token": "%s", "ct0": "%s"}' % (at, ct),
    }
    for label, payload in cases.items():
        got_at, got_ct = _parse_credentials_payload(payload)
        assert got_at == at, f"{label}: auth_token {got_at!r}"
        assert got_ct == ct, f"{label}: ct0 {got_ct!r}"
    print("PASS 8: payload parser accepts all five shapes")

    assert _parse_credentials_payload("hello") == (None, None)
    assert _parse_credentials_payload(f"auth_token={at}") == (at, None)
    print("PASS 9: parser rejects garbage / half payloads")

    assert _match_handover_code("auth_deadbeefcafe") == "deadbeefcafe"
    assert _match_handover_code("deadbeefcafe") == "deadbeefcafe"
    assert _match_handover_code("hello world") is None
    print("PASS 10: handover code matcher")

    guide, keyboard = _bookmarklet_guide(42)
    assert keyboard is not None
    url = keyboard.inline_keyboard[0][0].url
    assert url == "https://bot.example.com/bookmarklet?c=42", url
    print("PASS 11: guide carries the chat-bound installer link")

    handler = get_conversation_handler()
    assert set(handler.states.keys()) == {BOOKMARKLET, CT0, GROK_KEY, SET_GROK_KEY}
    print("PASS 12: conversation states wired (BOOKMARKLET replaces AUTH_TOKEN)")

    # --- minifier -------------------------------------------------------------
    from pathlib import Path

    from src.web_server import _minify_bookmarklet as minify

    src = Path("src/bookmarklet.js").read_text(encoding="utf-8")
    out = minify(
        src.replace("__BASE_URL__", "https://bot.example.com")
        .replace("__BOT_USERNAME__", "TestBot")
        .replace("__CHAT_ID__", "42")
    )
    assert "\n" not in out and "/*" not in out, "minifier left comments/newlines"
    assert out.startswith("(function"), out[:40]
    print("PASS 13: bookmarklet minifier produces single-line JS")

    # --- optional live probe with fake cookies --------------------------------
    try:
        valid = await real_verify("f" * 40, "e" * 32)
        print(
            "LIVE: verify_credentials(fake cookies) -> {!r} "
            "(False expected; True only means X endpoint churn was tolerated)"
            .format(valid)
        )
    except Exception as exc:  # network may be unavailable in CI
        print(f"LIVE: skipped ({type(exc).__name__}: {exc})")

    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
