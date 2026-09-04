#!/usr/bin/python3
"""Prove authenticated requests do not follow redirects or leave the allowlist."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request

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


if __name__ == "__main__":
  test_allowlist()
  test_redirects_do_not_forward_credentials()
  test_same_origin_success_still_works()
  print("ok")
