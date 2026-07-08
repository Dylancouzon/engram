"""serve.py auth model — the browser-facing security boundary.

These cases run without a daemon: every rejection path is enforced before any
store call, which is exactly the property we want to guarantee.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from engram.serve import _BaseHandler


@pytest.fixture
def server(config):
    token = "test-token-123"

    class Handler(_BaseHandler):
        pass

    Handler.config = config
    Handler.token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Handler.port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{Handler.port}", token, Handler.port
    httpd.shutdown()


def _get(url, *, headers=None, host=None):
    req = urllib.request.Request(url, headers=headers or {})
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as r:  # noqa: S310 - localhost test
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_no_cookie_is_forbidden(server):
    base, _, _ = server
    status, _, _ = _get(base + "/")
    assert status == 403


def test_wrong_token_is_forbidden(server):
    base, _, _ = server
    status, _, _ = _get(base + "/?k=nope")
    assert status == 403


def test_good_token_sets_httponly_cookie_and_redirects(server):
    base, token, _ = server
    # urllib follows the 303 to "/" which then 403s (no cookie jar), so inspect
    # the redirect directly by disabling redirect handling.
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        opener.open(base + f"/?k={token}")
        raise AssertionError("expected 303")
    except urllib.error.HTTPError as e:
        assert e.code == 303
        cookie = e.headers.get("Set-Cookie", "")
        assert "engram_session=" + token in cookie
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_bad_host_is_forbidden_dns_rebinding(server):
    base, token, _ = server
    status, _, _ = _get(base + f"/?k={token}", host="evil.example.com")
    assert status == 403


def test_api_needs_cookie_and_header(server):
    base, token, _ = server
    cookie = f"engram_session={token}"
    # cookie but no custom header -> the CSRF gap -> forbidden
    assert _get(base + "/api/state", headers={"Cookie": cookie})[0] == 403
    # header but no cookie -> forbidden
    assert _get(base + "/api/state", headers={"X-Engram-Token": token})[0] == 403


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
