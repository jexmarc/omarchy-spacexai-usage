#!/usr/bin/python3
"""Prove authenticated requests stay on the allowlist, ignore redirects, and refuse oversized bodies."""

from __future__ import annotations

import http.client
import http.server
import io
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import update


class _Recorder(http.server.BaseHTTPRequestHandler):
  def log_message(self, *_args: object) -> None:
    return

  def _record(self) -> None:
    self.server.seen.append({
      "path": self.path,
      "authorization": self.headers.get("Authorization"),
      "body": self.body,
    })

  def do_GET(self) -> None:
    self.body = b""
    self._dispatch()

  def do_POST(self) -> None:
    length = int(self.headers.get("Content-Length") or 0)
    self.body = self.rfile.read(length) if length else b""
    self._dispatch()

  def _dispatch(self) -> None:
    self._record()
    if self.path.startswith("/redirect"):
      code = int(self.path.rsplit("-", 1)[-1]) if self.path.rsplit("-", 1)[-1].isdigit() else 302
      self.send_response(code)
      self.send_header("Location", self.server.stolen_url)
      self.end_headers()
      return
    if self.path == "/stolen":
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(b'{"stolen":true}')
      return
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    self.wfile.write(b'{"ok":true}')


def _serve() -> tuple[http.server.HTTPServer, http.server.HTTPServer]:
  origin = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
  stolen = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
  origin.seen = []
  stolen.seen = []
  stolen_url = f"http://127.0.0.1:{stolen.server_port}/stolen"
  origin.stolen_url = stolen_url
  stolen.stolen_url = stolen_url
  threading.Thread(target=origin.serve_forever, daemon=True).start()
  threading.Thread(target=stolen.serve_forever, daemon=True).start()
  return origin, stolen


def _request(url: str, *, method: str = "GET", data: bytes | None = None) -> None:
  req = urllib.request.Request(
    url,
    data=data,
    headers={"Authorization": "Bearer secret-token"},
    method=method,
  )
  try:
    update._HTTP.open(req, timeout=2)
  except urllib.error.HTTPError as exc:
    if exc.code not in (301, 302, 303, 307, 308):
      raise


def test_allowlist() -> None:
  assert "cli-chat-proxy.grok.com" in update._ALLOWED_HOSTS
  assert "auth.x.ai" in update._ALLOWED_HOSTS
  assert "api2.cursor.sh" in update._ALLOWED_HOSTS
  assert update._allowed_request_url(update.GROK_BILLING)
  assert update._allowed_request_url(update.GROK_OIDC_TOKEN)
  assert update._allowed_request_url(f"{update.CURSOR_BACKEND}/oauth/token")
  assert not update._allowed_request_url("http://api2.cursor.sh/oauth/token")
  assert not update._allowed_request_url("https://evil.example/oauth/token")
  assert not update._allowed_request_url("https://user:pass@api2.cursor.sh/oauth/token")
  status, payload = update.http_json("https://evil.example/oauth/token", headers={"Authorization": "Bearer x"})
  assert status == -1 and payload == {}
  status, payload = update.http_json("http://cli-chat-proxy.grok.com/v1/billing", headers={"Authorization": "Bearer x"})
  assert status == -1 and payload == {}


def test_redirects_do_not_forward_credentials() -> None:
  origin, stolen = _serve()
  try:
    base = f"http://127.0.0.1:{origin.server_port}"
    for path, method, data in (
      ("/redirect-301", "GET", None),
      ("/redirect-302", "GET", None),
      ("/redirect-303", "GET", None),
      ("/redirect-307", "POST", b"refresh_token=super-secret"),
      ("/redirect-308", "POST", b"refresh_token=super-secret"),
    ):
      stolen.seen.clear()
      origin.seen.clear()
      _request(base + path, method=method, data=data)
      assert stolen.seen == [], f"{path} followed the redirect: {stolen.seen}"
      assert origin.seen and origin.seen[0]["authorization"] == "Bearer secret-token"
      if data:
        assert origin.seen[0]["body"] == data
  finally:
    origin.shutdown()
    stolen.shutdown()


def test_same_origin_success_still_works() -> None:
  origin, stolen = _serve()
  try:
    req = urllib.request.Request(f"http://127.0.0.1:{origin.server_port}/ok")
    with update._HTTP.open(req, timeout=2) as resp:
      payload = json.loads(resp.read())
    assert payload == {"ok": True}
    assert stolen.seen == []
  finally:
    origin.shutdown()
    stolen.shutdown()


class _ScriptedBody:
  """Bytes yielded only as read() asks. An unbounded read fails the test."""

  def __init__(self, logical_size: int, payload: bytes = b"", max_one_read: int | None = None):
    self.logical_size = logical_size
    self.payload = payload
    self.max_one_read = max_one_read
    self.pos = 0
    self.calls: list[int | None] = []

  def read(self, n: int = -1) -> bytes:
    if n is None or n < 0 or (self.max_one_read is not None and n > self.max_one_read):
      self.calls.append(None if n is None or (isinstance(n, int) and n < 0) else n)
      raise AssertionError(f"read asked for {n}")
    self.calls.append(int(n))
    take = min(int(n), self.logical_size - self.pos)
    start = self.pos
    self.pos += take
    if start >= len(self.payload):
      return b"x" * take
    if start + take <= len(self.payload):
      return self.payload[start:start + take]
    head = self.payload[start:]
    return head + (b"x" * (take - len(head)))

  def close(self) -> None:
    return None

  def __enter__(self) -> "_ScriptedBody":
    return self

  def __exit__(self, *_args: object) -> bool:
    return False


class _Resp:
  def __init__(self, status: int, headers: dict[str, str], body: _ScriptedBody):
    self.status = status
    self.headers = headers
    self._body = body

  def read(self, n: int = -1) -> bytes:
    return self._body.read(n)

  def close(self) -> None:
    self._body.close()

  def __enter__(self) -> "_Resp":
    return self

  def __exit__(self, *_args: object) -> bool:
    return False


class _Opener:
  def __init__(self, response: object = None, error: BaseException | None = None):
    self.response = response
    self.error = error

  def open(self, _request: object, timeout: float | None = None) -> object:
    if self.error is not None:
      raise self.error
    return self.response


@contextmanager
def _patched_http(opener: _Opener):
  original_http = update._HTTP
  original_loads = json.loads
  seen: list[object] = []

  def spy(raw: object, *args: object, **kwargs: object) -> object:
    seen.append(raw)
    return original_loads(raw, *args, **kwargs)

  update._HTTP = opener
  json.loads = spy
  try:
    yield seen
  finally:
    update._HTTP = original_http
    json.loads = original_loads


def test_oversized_content_length_is_rejected_without_reading_or_parsing() -> None:
  """A declared body larger than the cap is dropped before read() or json.loads."""
  limit = update.MAX_RESPONSE_BYTES
  declared = limit + 8_000_000
  # The unread tail would have completed a JSON object. Parsing either the
  # prefix or the full document would surface {"ok": true}.
  payload = b'{"ok":true}'
  body = _ScriptedBody(declared, payload, max_one_read=limit + 1)
  resp = _Resp(200, {"Content-Length": str(declared)}, body)
  with _patched_http(_Opener(resp)) as seen:
    status, parsed = update.http_json(update.GROK_BILLING, headers={"Authorization": "Bearer secret-token"})
  assert status == -1 and parsed == {}, (status, parsed)
  assert body.calls == [], body.calls
  assert body.pos == 0
  assert seen == []


def test_body_past_the_limit_reads_one_extra_byte_and_is_not_parsed() -> None:
  """No Content-Length: read at most limit + 1, then fail without parsing."""
  limit = update.MAX_RESPONSE_BYTES
  declared = limit + 4096
  payload = b'{"ok":true}'
  body = _ScriptedBody(declared, payload, max_one_read=limit + 1)
  resp = _Resp(200, {}, body)
  with _patched_http(_Opener(resp)) as seen:
    status, parsed = update.http_json(update.GROK_BILLING)
  assert status == -1 and parsed == {}, (status, parsed)
  assert body.calls == [limit + 1], body.calls
  assert body.pos == limit + 1
  assert body.pos < declared
  assert seen == []


def test_small_json_body_is_still_parsed() -> None:
  raw = b'{"ok":true}'
  body = _ScriptedBody(len(raw), raw, max_one_read=update.MAX_RESPONSE_BYTES + 1)
  resp = _Resp(200, {"Content-Length": str(len(raw))}, body)
  with _patched_http(_Opener(resp)) as seen:
    status, parsed = update.http_json(update.GROK_BILLING)
  assert status == 200 and parsed == {"ok": True}
  assert body.calls == [update.MAX_RESPONSE_BYTES + 1]
  assert seen and json.loads(seen[0]) == {"ok": True}


def test_small_http_error_body_keeps_its_status() -> None:
  raw = b'{"error":"unauthorized"}'
  err = urllib.error.HTTPError(
    update.GROK_BILLING,
    401,
    "unauthorized",
    {"Content-Length": str(len(raw))},
    io.BytesIO(raw),
  )
  with _patched_http(_Opener(error=err)) as seen:
    status, parsed = update.http_json(update.GROK_BILLING)
  assert status == 401 and parsed == {"error": "unauthorized"}
  assert seen and b"unauthorized" in seen[0]


def test_oversized_http_error_is_not_read_or_parsed() -> None:
  limit = update.MAX_RESPONSE_BYTES
  declared = limit + 8_000_000
  body = _ScriptedBody(declared, b'{"error":"no"}', max_one_read=limit + 1)
  err = urllib.error.HTTPError(
    update.GROK_BILLING,
    500,
    "big",
    {"Content-Length": str(declared)},
    body,
  )
  with _patched_http(_Opener(error=err)) as seen:
    status, parsed = update.http_json(update.GROK_BILLING)
  assert status == -1 and parsed == {}, (status, parsed)
  assert body.calls == []
  assert body.pos == 0
  assert seen == []


class _SocketBuffer:
  def __init__(self, raw: bytes):
    self.buffer = io.BytesIO(raw)

  def makefile(self, *_args: object, **_kwargs: object) -> io.BytesIO:
    return self.buffer

  def close(self) -> None:
    return None


def _response_from(raw: bytes) -> tuple[http.client.HTTPResponse, io.BytesIO]:
  sock = _SocketBuffer(raw)
  resp = http.client.HTTPResponse(sock)
  resp.begin()
  return resp, sock.buffer


def test_real_response_oversized_content_length_leaves_the_body_unread() -> None:
  marker = b'{"ok":true,"unread":true}'
  declared = update.MAX_RESPONSE_BYTES + 1
  raw = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(declared).encode("ascii") + b"\r\n"
    b"\r\n" + marker
  )
  resp, buffer = _response_from(raw)
  mark = buffer.tell()
  try:
    update._read_bounded(resp)
    raise AssertionError("oversized Content-Length was accepted")
  except update._ResponseTooLarge:
    pass
  assert buffer.tell() == mark
  assert buffer.read() == marker


def test_real_chunked_response_stops_after_one_extra_byte() -> None:
  limit = 11
  # First `limit` bytes are valid JSON. The rest must not be parsed or consumed.
  chunk = b'{"ok":true}' + (b"Y" * 40)
  assert chunk[:limit] == b'{"ok":true}'
  raw = (
    b"HTTP/1.1 200 OK\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"\r\n" + f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n0\r\n\r\n"
  )
  resp, buffer = _response_from(raw)
  try:
    update._read_bounded(resp, limit)
    raise AssertionError("extra body byte was accepted")
  except update._ResponseTooLarge:
    pass
  rest = buffer.read()
  assert b"Y" in rest
  assert rest.endswith(b"0\r\n\r\n")


def test_body_exactly_at_the_limit_is_returned() -> None:
  raw = b'{"ok":true}'
  body = _ScriptedBody(len(raw), raw, max_one_read=len(raw) + 1)
  resp = _Resp(200, {"Content-Length": str(len(raw))}, body)
  assert update._read_bounded(resp, len(raw)) == raw
  assert body.calls == [len(raw) + 1]
  assert body.pos == len(raw)


_AES_KEY = bytes(range(16))
_AES_IV = bytes(range(16, 32))
# openssl enc -aes-128-cbc of b"hello-token" with that key and iv.
_AES_GOOD = bytes.fromhex("b73813c9766b55ec18cb2160aa35b915")
# Same key and iv, one block whose plaintext padding is not PKCS#7.
_AES_BAD = bytes.fromhex("da99ccd4917577f22eaf1264c78a2235")


def test_aes_rejects_invalid_padding() -> None:
  assert update.aes128_cbc_decrypt(_AES_KEY, _AES_GOOD, _AES_IV) == b"hello-token"
  assert update.aes128_cbc_decrypt(_AES_KEY, _AES_BAD, _AES_IV) is None
  assert update.aes128_cbc_decrypt(_AES_KEY, _AES_GOOD[:-1], _AES_IV) is None


def _auth_file(tmp: Path) -> tuple[Path, dict[str, object]]:
  auth = tmp / "auth.json"
  original = {
    "user": {
      "key": "old-access",
      "refresh_token": "old-refresh",
      "oidc_client_id": "client",
    },
    "other": {"key": "keep-me"},
  }
  auth.write_text(json.dumps(original), encoding="utf-8")
  os.chmod(auth, 0o600)
  return auth, original


def test_grok_refresh_rejects_a_bad_token_without_rewriting_auth() -> None:
  original_auth = update.GROK_AUTH
  original_http = update.http_json
  try:
    with tempfile.TemporaryDirectory() as raw:
      tmp = Path(raw)
      auth, original = _auth_file(tmp)
      before = auth.read_bytes()
      update.GROK_AUTH = auth

      def fake_http(url: str, **kwargs: object) -> tuple[int, dict[str, object]]:
        return 200, {"access_token": {"not": "a string"}, "expires_in": 60}

      update.http_json = fake_http
      assert update.grok_refresh("user", original["user"]) is None
      assert auth.read_bytes() == before

      def fake_huge(url: str, **kwargs: object) -> tuple[int, dict[str, object]]:
        return 200, {"access_token": "a" * (update.MAX_TOKEN_CHARS + 1), "expires_in": 60}

      update.http_json = fake_huge
      assert update.grok_refresh("user", original["user"]) is None
      assert auth.read_bytes() == before

      def fake_newline(url: str, **kwargs: object) -> tuple[int, dict[str, object]]:
        return 200, {"access_token": "good\r\nX-Injected: 1", "expires_in": 60}

      update.http_json = fake_newline
      assert update.grok_refresh("user", original["user"]) is None
      assert auth.read_bytes() == before

      def fake_bad_rotation(url: str, **kwargs: object) -> tuple[int, dict[str, object]]:
        return 200, {"access_token": "new-access", "refresh_token": "bad\ntoken", "expires_in": 60}

      update.http_json = fake_bad_rotation
      assert update.grok_refresh("user", original["user"]) is None
      assert auth.read_bytes() == before
  finally:
    update.GROK_AUTH = original_auth
    update.http_json = original_http


def test_grok_refresh_replaces_auth_atomically() -> None:
  original_auth = update.GROK_AUTH
  original_http = update.http_json
  try:
    with tempfile.TemporaryDirectory() as raw:
      tmp = Path(raw)
      auth, original = _auth_file(tmp)
      update.GROK_AUTH = auth

      def fake_http(url: str, **kwargs: object) -> tuple[int, dict[str, object]]:
        return 200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 30}

      update.http_json = fake_http
      updated = update.grok_refresh("user", original["user"])
      assert updated is not None
      assert updated["key"] == "new-access"
      saved = json.loads(auth.read_text(encoding="utf-8"))
      assert saved["user"]["key"] == "new-access"
      assert saved["user"]["refresh_token"] == "new-refresh"
      assert saved["other"]["key"] == "keep-me"
      assert auth.stat().st_mode & 0o777 == 0o600
      assert list(tmp.glob("*.tmp")) == []
  finally:
    update.GROK_AUTH = original_auth
    update.http_json = original_http


def test_cursor_refresh_rejects_a_non_string_token() -> None:
  original_http = update.http_json
  try:
    update.http_json = lambda url, **kwargs: (200, {"access_token": {"nope": True}})
    assert update.cursor_refresh("refresh") is None
    update.http_json = lambda url, **kwargs: (200, {"accessToken": "cursor-access"})
    assert update.cursor_refresh("refresh") == "cursor-access"
  finally:
    update.http_json = original_http


def test_failed_cursor_fetch_does_not_replace_the_cache() -> None:
  original_cache = update.CACHE_DIR
  original_rpc = update.cursor_rpc
  try:
    with tempfile.TemporaryDirectory() as raw:
      tmp = Path(raw)
      update.CACHE_DIR = tmp
      events_path = tmp / "cursor-events.json"
      events_path.write_text('[{"model":"kept"}]\n', encoding="utf-8")
      os.utime(events_path, (1, 1))
      agg_path = tmp / "cursor-aggregation.json"
      agg_path.write_text('[{"model":"kept"}]\n', encoding="utf-8")
      os.utime(agg_path, (1, 1))
      update.cursor_rpc = lambda method, token, payload=None: (-1, {})
      assert update.cursor_usage_events("tok") == []
      assert update.cursor_usage_aggregation("tok") == []
      assert events_path.read_text(encoding="utf-8") == '[{"model":"kept"}]\n'
      assert agg_path.read_text(encoding="utf-8") == '[{"model":"kept"}]\n'
  finally:
    update.CACHE_DIR = original_cache
    update.cursor_rpc = original_rpc


def test_complete_cursor_fetch_still_writes_the_cache() -> None:
  original_cache = update.CACHE_DIR
  original_rpc = update.cursor_rpc
  try:
    with tempfile.TemporaryDirectory() as raw:
      tmp = Path(raw)
      update.CACHE_DIR = tmp

      def fake_rpc(method: str, token: str, payload: object = None) -> tuple[int, dict[str, object]]:
        if method == "GetFilteredUsageEvents":
          return 200, {"usageEventsDisplay": [{
            "timestamp": 1,
            "model": "gpt",
            "tokenUsage": {"inputTokens": 3, "outputTokens": 4},
          }]}
        return 200, {"aggregations": [{
          "modelIntent": "gpt",
          "inputTokens": 3,
          "outputTokens": 4,
          "cacheReadTokens": 0,
          "cacheWriteTokens": 0,
        }]}

      update.cursor_rpc = fake_rpc
      events = update.cursor_usage_events("tok")
      rows = update.cursor_usage_aggregation("tok")
      assert events[0]["inputTokens"] == 3
      assert rows[0]["outputTokens"] == 4
      assert json.loads((tmp / "cursor-events.json").read_text(encoding="utf-8"))[0]["model"] == "gpt"
      assert json.loads((tmp / "cursor-aggregation.json").read_text(encoding="utf-8"))[0]["model"] == "gpt"
  finally:
    update.CACHE_DIR = original_cache
    update.cursor_rpc = original_rpc


if __name__ == "__main__":
  test_allowlist()
  test_redirects_do_not_forward_credentials()
  test_same_origin_success_still_works()
  test_oversized_content_length_is_rejected_without_reading_or_parsing()
  test_body_past_the_limit_reads_one_extra_byte_and_is_not_parsed()
  test_small_json_body_is_still_parsed()
  test_small_http_error_body_keeps_its_status()
  test_oversized_http_error_is_not_read_or_parsed()
  test_real_response_oversized_content_length_leaves_the_body_unread()
  test_real_chunked_response_stops_after_one_extra_byte()
  test_body_exactly_at_the_limit_is_returned()
  test_aes_rejects_invalid_padding()
  test_grok_refresh_rejects_a_bad_token_without_rewriting_auth()
  test_grok_refresh_replaces_auth_atomically()
  test_cursor_refresh_rejects_a_non_string_token()
  test_failed_cursor_fetch_does_not_replace_the_cache()
  test_complete_cursor_fetch_still_writes_the_cache()
  print("ok")
