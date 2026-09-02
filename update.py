#!/usr/bin/python3
"""Write Grok and Cursor remaining-quota records for omarchy.agents.

Uses the same JSON contract as the stock Claude/Codex/Fireworks collectors.
Credentials stay on this machine and are sent only to that product's usage
API. Nothing in this file is a user secret.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

USAGE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omarchy" / "agents" / "usage"
GROK_AUTH = Path(os.environ.get("GROK_HOME", Path.home() / ".grok")) / "auth.json"
GROK_BOT_SECRETS = Path.home() / ".config" / "Grok Bot" / "sand-secrets.json"
GROK_BILLING = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_USER = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"
GROK_SETTINGS = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_OIDC_TOKEN = "https://auth.x.ai/oauth2/token"
CURSOR_BACKEND = "https://api2.cursor.sh"
# Public Cursor CLI OAuth client id (not a user secret).
CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
PRODUCT_TITLES = {
  "GrokBuild": "Build",
  "GrokChat": "Chat",
  "GrokVoice": "Voice",
  "Api": "API",
  "Imagine": "Imagine",
}


def now_utc() -> datetime:
  return datetime.now(timezone.utc)


def now_iso() -> str:
  return now_utc().isoformat()


def warn(message: str) -> None:
  print(f"spacexai-usage: {message}", file=sys.stderr)


def write_record(agent_id: str, record: dict[str, Any]) -> None:
  USAGE_DIR.mkdir(parents=True, exist_ok=True)
  path = USAGE_DIR / f"{agent_id}.json"
  fd, tmp_name = tempfile.mkstemp(prefix=f".{agent_id}.", suffix=".tmp", dir=USAGE_DIR)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(record, handle, indent=2)
      handle.write("\n")
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)
  except Exception:
    try:
      os.unlink(tmp_name)
    except OSError:
      pass
    raise
  path.chmod(0o600)


def drop_record(agent_id: str) -> None:
  try:
    (USAGE_DIR / f"{agent_id}.json").unlink()
  except FileNotFoundError:
    pass


def remaining_record(
  agent_id: str,
  name: str,
  *,
  ready: bool,
  tier_label: str = "",
  auth_help: str = "",
  limits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
  return {
    "schemaVersion": 1,
    "id": agent_id,
    "name": name,
    "updatedAt": now_iso(),
    "ready": ready,
    "hasLocalStats": False,
    "hasPromptStats": False,
    "tierLabel": tier_label,
    "usageStatusText": "",
    "authHelpText": auth_help,
    "limits": limits or [],
  }


def used_fraction(value: Any) -> float | None:
  try:
    n = float(value)
  except (TypeError, ValueError):
    return None
  if n != n or n < 0:
    return None
  if n > 1:
    n = n / 100.0
  return min(1.0, n)


def remaining_pct(used: float) -> int:
  return max(0, min(100, round((1.0 - used) * 100)))


def limit_entry(label: str, used: float, resets_at: str = "", title: str = "") -> dict[str, Any]:
  return {
    "label": label,
    "title": title or label,
    "percent": used,
    "resetsAt": resets_at,
  }


def iso_from_any(value: Any) -> str:
  if value is None or value == "":
    return ""
  if isinstance(value, (int, float)):
    ts = float(value)
    if ts > 1e12:
      ts /= 1000.0
    try:
      return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
      return ""
  raw = str(value).strip()
  if raw.isdigit():
    return iso_from_any(int(raw))
  try:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
  except ValueError:
    return raw


def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, data: bytes | None = None, timeout: int = 15) -> tuple[int, Any]:
  req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      body = resp.read()
      status = int(resp.status)
  except urllib.error.HTTPError as exc:
    body = exc.read()
    status = int(exc.code)
  except (urllib.error.URLError, TimeoutError, OSError):
    return -1, {}
  if not body:
    return status, {}
  try:
    parsed = json.loads(body)
  except json.JSONDecodeError:
    return status, {}
  return status, parsed if isinstance(parsed, dict) else {}


def number_field(payload: dict[str, Any], *names: str) -> float | None:
  for name in names:
    if name not in payload or payload.get(name) is None:
      continue
    try:
      value = float(payload[name])
    except (TypeError, ValueError):
      continue
    if value == value:
      return value
  return None


# ---------------------------------------------------------------- AES-128-CBC (no OpenSSL; key never in argv)

_SBOX = bytes([
  0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
  0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
  0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
  0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
  0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
  0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
  0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
  0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
  0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
  0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
  0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
  0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
  0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
  0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
  0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
  0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
])
_INV_SBOX = bytes([_SBOX.index(i) for i in range(256)])
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
  return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF


def _mul(a: int, b: int) -> int:
  out = 0
  for _ in range(8):
    if b & 1:
      out ^= a
    hi = a & 0x80
    a = (a << 1) & 0xFF
    if hi:
      a ^= 0x1B
    b >>= 1
  return out


def _expand_key(key: bytes) -> list[int]:
  w = list(key)
  for i in range(4, 44):
    t = w[(i - 1) * 4:(i - 1) * 4 + 4]
    if i % 4 == 0:
      t = [_SBOX[t[1]] ^ _RCON[i // 4], _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]
    prev = w[(i - 4) * 4:(i - 4) * 4 + 4]
    w.extend(p ^ q for p, q in zip(prev, t))
  return w


def _add_round_key(state: list[int], key: list[int], round_n: int) -> None:
  off = round_n * 16
  for i in range(16):
    state[i] ^= key[off + i]


def _inv_shift_rows(state: list[int]) -> None:
  state[1], state[5], state[9], state[13] = state[13], state[1], state[5], state[9]
  state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
  state[3], state[7], state[11], state[15] = state[7], state[11], state[15], state[3]


def _inv_sub_bytes(state: list[int]) -> None:
  for i in range(16):
    state[i] = _INV_SBOX[state[i]]


def _inv_mix_columns(state: list[int]) -> None:
  for c in range(4):
    i = c * 4
    a, b, d, e = state[i], state[i + 1], state[i + 2], state[i + 3]
    state[i] = _mul(a, 14) ^ _mul(b, 11) ^ _mul(d, 13) ^ _mul(e, 9)
    state[i + 1] = _mul(a, 9) ^ _mul(b, 14) ^ _mul(d, 11) ^ _mul(e, 13)
    state[i + 2] = _mul(a, 13) ^ _mul(b, 9) ^ _mul(d, 14) ^ _mul(e, 11)
    state[i + 3] = _mul(a, 11) ^ _mul(b, 13) ^ _mul(d, 9) ^ _mul(e, 14)


def _decrypt_block(block: bytes, key: list[int]) -> bytes:
  state = list(block)
  _add_round_key(state, key, 10)
  for round_n in range(9, 0, -1):
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, key, round_n)
    _inv_mix_columns(state)
  _inv_shift_rows(state)
  _inv_sub_bytes(state)
  _add_round_key(state, key, 0)
  return bytes(state)


def aes128_cbc_decrypt(key: bytes, data: bytes, iv: bytes) -> bytes | None:
  if len(key) != 16 or len(iv) != 16 or len(data) < 16 or len(data) % 16:
    return None
  expanded = _expand_key(key)
  prev = iv
  out = bytearray()
  for i in range(0, len(data), 16):
    block = data[i:i + 16]
    plain = bytes(a ^ b for a, b in zip(_decrypt_block(block, expanded), prev))
    out.extend(plain)
    prev = block
  if not out:
    return None
  pad = out[-1]
  if pad < 1 or pad > 16 or out[-pad:] != bytes([pad]) * pad:
    return bytes(out)
  return bytes(out[:-pad])


def decrypt_chromium_v10(blob_b64: str, password: str) -> bytes | None:
  try:
    raw = base64.b64decode(blob_b64)
  except (ValueError, TypeError):
    return None
  if not raw.startswith(b"v10") or len(raw) <= 19:
    return None
  ct = raw[3:]
  iv = b" " * 16
  for iterations in (1, 1003):
    key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", iterations, dklen=16)
    plain = aes128_cbc_decrypt(key, ct, iv)
    if plain:
      return plain
  return None


def jwt_from_bytes(data: bytes) -> str | None:
  match = JWT_RE.search(data.decode("utf-8", "replace"))
  return match.group(0) if match else None


def jwt_claims(token: str) -> dict[str, Any]:
  try:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return claims if isinstance(claims, dict) else {}
  except (IndexError, ValueError, json.JSONDecodeError):
    return {}


def jwt_expired(token: str, skew_seconds: int = 60) -> bool:
  exp = jwt_claims(token).get("exp")
  try:
    return float(exp) <= time.time() + skew_seconds
  except (TypeError, ValueError):
    return False


def libsecret_password(application: str) -> str | None:
  try:
    proc = subprocess.run(
      ["secret-tool", "lookup", "application", application],
      capture_output=True,
      text=True,
      timeout=3,
    )
  except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
    return None
  if proc.returncode != 0:
    return None
  secret = (proc.stdout or "").strip()
  return secret or None


def decrypt_grok_bot_secret(blob_b64: str) -> str | None:
  passwords = []
  stored = libsecret_password("Grok Bot")
  if stored:
    passwords.append(stored)
  # Chromium's well-known Linux default OSCrypt password, not a user secret.
  passwords.append("peanuts")
  seen: set[str] = set()
  for password in passwords:
    if password in seen:
      continue
    seen.add(password)
    plain = decrypt_chromium_v10(blob_b64, password)
    if not plain:
      continue
    token = jwt_from_bytes(plain)
    if token:
      return token
  return None


# ---------------------------------------------------------------- Grok CLI / SuperGrok

def grok_auth_entry() -> tuple[str, dict[str, Any]] | None:
  if not GROK_AUTH.is_file():
    return None
  try:
    data = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return None
  if not isinstance(data, dict):
    return None
  for key, entry in data.items():
    if isinstance(entry, dict) and entry.get("key"):
      return str(key), entry
  return None


def grok_expires_at(entry: dict[str, Any], token: str) -> datetime | None:
  raw = str(entry.get("expires_at") or "")
  if raw:
    try:
      return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
      pass
  exp = jwt_claims(token).get("exp")
  try:
    return datetime.fromtimestamp(float(exp), timezone.utc)
  except (TypeError, ValueError):
    return None


def grok_refresh(slot: str, entry: dict[str, Any]) -> dict[str, Any] | None:
  refresh_token = str(entry.get("refresh_token") or "")
  client_id = str(entry.get("oidc_client_id") or "")
  if not refresh_token or not client_id:
    return None
  body = urllib.parse.urlencode({
    "grant_type": "refresh_token",
    "refresh_token": refresh_token,
    "client_id": client_id,
  }).encode()
  status, payload = http_json(
    GROK_OIDC_TOKEN,
    method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    data=body,
  )
  if status != 200 or not payload.get("access_token"):
    return None
  updated = dict(entry)
  updated["key"] = payload["access_token"]
  if payload.get("refresh_token"):
    updated["refresh_token"] = payload["refresh_token"]
  expires_in = payload.get("expires_in")
  try:
    updated["expires_at"] = (now_utc() + timedelta(seconds=int(expires_in))).isoformat()
  except (TypeError, ValueError):
    updated["expires_at"] = (now_utc() + timedelta(hours=1)).isoformat()
  try:
    with GROK_AUTH.open("r+", encoding="utf-8") as handle:
      fcntl.flock(handle, fcntl.LOCK_EX)
      data = json.load(handle)
      if isinstance(data, dict):
        data[slot] = updated
        handle.seek(0)
        handle.truncate()
        json.dump(data, handle, indent=2)
        handle.write("\n")
  except OSError:
    pass
  return updated


def grok_bearer() -> str | None:
  loaded = grok_auth_entry()
  if not loaded:
    return None
  slot, entry = loaded
  token = str(entry.get("key") or "")
  expires = grok_expires_at(entry, token)
  if expires and expires <= now_utc() + timedelta(minutes=15):
    refreshed = grok_refresh(slot, entry)
    if refreshed and refreshed.get("key"):
      return str(refreshed["key"])
  return token or None


def grok_headers(token: str) -> dict[str, str]:
  return {
    "Authorization": "Bearer " + token,
    "Accept": "application/json",
    "X-XAI-Token-Auth": "xai-grok-cli",
  }


def collect_grok() -> dict[str, Any] | None:
  if not GROK_AUTH.is_file() and not (Path.home() / ".grok").exists():
    return None
  token = grok_bearer()
  if not token:
    return remaining_record("grok", "Grok", ready=False, auth_help="Run `grok login` so SuperGrok remaining usage can be read.")
  billing_status, billing = http_json(GROK_BILLING, headers=grok_headers(token))
  if billing_status in (401, 403):
    loaded = grok_auth_entry()
    if loaded:
      refreshed = grok_refresh(*loaded)
      if refreshed and refreshed.get("key"):
        token = str(refreshed["key"])
        billing_status, billing = http_json(GROK_BILLING, headers=grok_headers(token))
  if billing_status == -1:
    return remaining_record("grok", "Grok", ready=False, auth_help="Could not reach Grok usage stats.")
  if billing_status != 200:
    return remaining_record("grok", "Grok", ready=False, auth_help="Grok usage stats were rejected. Run `grok login` and retry.")

  _, user = http_json(GROK_USER, headers=grok_headers(token))
  _, settings = http_json(GROK_SETTINGS, headers=grok_headers(token))
  cfg = billing.get("config", billing) if isinstance(billing, dict) else {}
  if not isinstance(cfg, dict):
    cfg = {}
  period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
  resets = iso_from_any(period.get("end") or cfg.get("billingPeriodEnd"))
  period_type = str(period.get("type") or "")
  title = "Weekly" if "WEEKLY" in period_type.upper() else "Monthly" if "MONTHLY" in period_type.upper() else "Weekly"
  used = used_fraction(cfg.get("creditUsagePercent"))
  if used is None and isinstance(cfg.get("onDemandCap"), dict) and cfg["onDemandCap"].get("val"):
    cap = float(cfg["onDemandCap"]["val"] or 0)
    spent = float((cfg.get("onDemandUsed") or {}).get("val") or 0)
    if cap > 0:
      used = min(1.0, max(0.0, spent / cap))
  if used is None:
    used = 0.0

  limits = [limit_entry("Grok", used, resets, "Grok")]
  for product in cfg.get("productUsage") or []:
    if not isinstance(product, dict) or product.get("usagePercent") is None:
      continue
    product_used = used_fraction(product.get("usagePercent"))
    if product_used is None or abs(product_used - used) < 0.0001:
      continue
    raw_name = str(product.get("product") or "Product")
    product_title = PRODUCT_TITLES.get(raw_name, raw_name)
    limits.append(limit_entry(product_title, product_used, resets, product_title))

  plan = ""
  if isinstance(settings, dict):
    plan = str(settings.get("subscription_tier_display") or "").strip()
  if not plan and isinstance(user, dict):
    plan = str(user.get("subscriptionTier") or "").strip()
  leftover = remaining_pct(used)
  tier = f"{leftover}% remaining"
  if plan:
    tier += f" · {plan}"
  return remaining_record("grok", "Grok", ready=True, tier_label=tier, limits=limits)


# ---------------------------------------------------------------- Cursor / Grok Bot

def cursor_checksum(machine_id: str, now_ms: int | None = None) -> str:
  if now_ms is None:
    now_ms = int(time.time() * 1000)
  ks = now_ms // 1_000_000
  buf = bytearray([
    (ks >> 40) & 255,
    (ks >> 32) & 255,
    (ks >> 24) & 255,
    (ks >> 16) & 255,
    (ks >> 8) & 255,
    ks & 255,
  ])
  prev = 165
  for i, byte in enumerate(buf):
    buf[i] = ((byte ^ prev) + (i % 256)) & 255
    prev = buf[i]
  return base64.urlsafe_b64encode(bytes(buf)).decode("ascii").rstrip("=") + machine_id


def cursor_rpc(method: str, token: str) -> tuple[int, Any]:
  machine = str(uuid.uuid4())
  headers = {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + token,
    "Connect-Protocol-Version": "1",
    "x-cursor-checksum": cursor_checksum(machine),
    "x-cursor-client-type": "sand",
    "x-cursor-client-version": "0.1.0",
    "x-sand-box-namespace": "prod",
    "x-ghost-mode": "true",
    "x-request-id": str(uuid.uuid4()),
  }
  return http_json(
    f"{CURSOR_BACKEND}/aiserver.v1.DashboardService/{method}",
    method="POST",
    headers=headers,
    data=b"{}",
  )


def cursor_refresh(refresh_token: str) -> str | None:
  body = json.dumps({
    "client_id": CURSOR_CLIENT_ID,
    "grant_type": "refresh_token",
    "refresh_token": refresh_token,
  }).encode()
  status, payload = http_json(
    f"{CURSOR_BACKEND}/oauth/token",
    method="POST",
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    data=body,
  )
  if status != 200:
    return None
  access = payload.get("access_token") or payload.get("accessToken")
  return str(access) if access else None


def grok_bot_tokens() -> tuple[str | None, str | None]:
  if not GROK_BOT_SECRETS.is_file():
    return None, None
  try:
    secrets = json.loads(GROK_BOT_SECRETS.read_text(encoding="utf-8"))
    accounts = json.loads(secrets.get("cursor-accounts") or "{}")
  except (OSError, json.JSONDecodeError, TypeError):
    return None, None
  if not isinstance(accounts, dict):
    return None, None
  active = accounts.get("active")
  bucket = (accounts.get("accounts") or {}).get(active) if active else None
  if not isinstance(bucket, dict):
    return None, None
  access = decrypt_grok_bot_secret(str(bucket.get("cursor-access-token") or ""))
  refresh = decrypt_grok_bot_secret(str(bucket.get("cursor-refresh-token") or ""))
  return access, refresh


def cursor_auth_file_tokens() -> tuple[str | None, str | None]:
  for path in (
    Path.home() / ".config" / "cursor" / "auth.json",
    Path.home() / ".cursor" / "auth.json",
  ):
    if not path.is_file():
      continue
    try:
      data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
      continue
    if not isinstance(data, dict):
      continue
    access = data.get("accessToken") or data.get("access_token")
    refresh = data.get("refreshToken") or data.get("refresh_token")
    return (str(access) if access else None, str(refresh) if refresh else None)
  return None, None


def usable_cursor_token() -> str | None:
  access, refresh = grok_bot_tokens()
  if not access:
    access, refresh = cursor_auth_file_tokens()
  if access and jwt_expired(access) and refresh:
    rotated = cursor_refresh(refresh)
    if rotated:
      access = rotated
  if access and jwt_expired(access, skew_seconds=0):
    return None
  return access


def collect_grok_bot() -> dict[str, Any] | None:
  if not GROK_BOT_SECRETS.parent.is_dir():
    return None
  token = usable_cursor_token()
  if not token:
    return remaining_record("grok-bot", "Grok Bot", ready=False, auth_help="Sign in to Grok Bot so its remaining weekly allowance can be read.")
  status, payload = cursor_rpc("GetSandUsageStatus", token)
  if status == -1:
    return remaining_record("grok-bot", "Grok Bot", ready=False, auth_help="Could not reach Grok Bot usage stats.")
  if status in (401, 403):
    return remaining_record("grok-bot", "Grok Bot", ready=False, auth_help="Grok Bot usage stats were rejected. Sign in to Grok Bot again.")
  if status != 200:
    return remaining_record("grok-bot", "Grok Bot", ready=False, auth_help="Grok Bot did not return usage stats.")

  used = used_fraction(payload.get("usagePercent"))
  if used is None:
    used = 0.0 if payload.get("hasAvailableUsage") else 1.0
  resets = iso_from_any(payload.get("nextResetTimestampUtc"))
  plan = str(payload.get("grokPlanLabel") or "Grok Bot").strip()
  leftover = remaining_pct(used)
  auth_help = ""
  if payload.get("hasAvailableUsage") is False:
    auth_help = "Weekly Grok Bot allowance is exhausted until reset."
  return remaining_record(
    "grok-bot",
    "Grok Bot",
    ready=True,
    tier_label=f"{leftover}% remaining" + (f" · {plan}" if plan else ""),
    auth_help=auth_help,
    limits=[limit_entry("Grok Bot", used, resets, "Grok Bot")],
  )


def collect_cursor() -> dict[str, Any] | None:
  token = usable_cursor_token()
  cursor_present = any(
    path.exists()
    for path in (Path.home() / ".cursor", Path.home() / ".config" / "Cursor", Path.home() / ".config" / "cursor")
  )
  if not token:
    if not cursor_present and not GROK_BOT_SECRETS.is_file():
      return None
    return remaining_record("cursor", "Cursor", ready=False, auth_help="Sign in to Cursor or Grok Bot so remaining plan usage can be read.")

  usage_status, usage = cursor_rpc("GetCurrentPeriodUsage", token)
  plan_status, plan_info = cursor_rpc("GetPlanInfo", token)
  if usage_status == -1:
    return remaining_record("cursor", "Cursor", ready=False, auth_help="Could not reach Cursor usage stats.")
  if usage_status != 200:
    return remaining_record("cursor", "Cursor", ready=False, auth_help="Cursor usage stats were not available for this login.")

  plan_usage = usage.get("planUsage") if isinstance(usage.get("planUsage"), dict) else {}
  spend = usage.get("spendLimitUsage") if isinstance(usage.get("spendLimitUsage"), dict) else {}
  resets = iso_from_any(usage.get("billingCycleEnd"))
  auto_used = used_fraction(plan_usage.get("autoPercentUsed"))
  api_used = used_fraction(plan_usage.get("apiPercentUsed"))
  if auto_used is None and api_used is None:
    auto_used = used_fraction(plan_usage.get("totalPercentUsed"))
    if auto_used is None:
      match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(usage.get("displayMessage") or ""))
      if match:
        auto_used = used_fraction(match.group(1))

  limits: list[dict[str, Any]] = []
  if auto_used is not None:
    limits.append(limit_entry("Cursor Models", auto_used, resets, "Cursor Models"))
  if api_used is not None:
    limits.append(limit_entry("Other Models", api_used, resets, "Other Models"))

  on_demand_remaining = number_field(spend, "individualRemaining", "remaining")
  on_demand_limit = number_field(spend, "individualLimit", "limit")
  if on_demand_limit and on_demand_limit > 0:
    remaining = on_demand_remaining if on_demand_remaining is not None else on_demand_limit
    on_demand_used = min(1.0, max(0.0, 1.0 - remaining / on_demand_limit))
    limits.append(limit_entry("On Demand", on_demand_used, resets, "On Demand"))
  elif on_demand_remaining is not None:
    limits.append(limit_entry("On Demand", 0.0, resets, "On Demand"))

  included = [entry["percent"] for entry in limits if entry["title"] in ("Cursor Models", "Other Models")]
  leftover = remaining_pct(max(included) if included else 0.0)
  plan_name = ""
  if plan_status == 200:
    info = plan_info.get("planInfo") if isinstance(plan_info.get("planInfo"), dict) else {}
    plan_name = str(info.get("planName") or "").strip()
  tier = f"{leftover}% remaining"
  if plan_name:
    tier += f" · {plan_name}"
  return remaining_record("cursor", "Cursor", ready=bool(limits), tier_label=tier, limits=limits)


def merge_grok_records(grok: dict[str, Any] | None, bot: dict[str, Any] | None) -> dict[str, Any] | None:
  if not grok and not bot:
    return None
  limits: list[dict[str, Any]] = []
  helps: list[str] = []
  used_parts: list[float] = []
  if grok:
    for entry in grok.get("limits") or []:
      if not isinstance(entry, dict):
        continue
      title = str(entry.get("title") or entry.get("label") or "")
      limits.append(entry)
      if title == "Grok":
        used_parts.append(float(entry.get("percent") or 0))
    help_text = str(grok.get("authHelpText") or "").strip()
    if help_text:
      helps.append(help_text)
  if bot:
    for entry in bot.get("limits") or []:
      if not isinstance(entry, dict):
        continue
      titled = dict(entry)
      titled["label"] = "Grok Bot"
      titled["title"] = "Grok Bot"
      limits.append(titled)
      used_parts.append(float(titled.get("percent") or 0))
    help_text = str(bot.get("authHelpText") or "").strip()
    if help_text:
      helps.append(help_text)
  leftover = remaining_pct(max(used_parts) if used_parts else 0.0)
  plan = ""
  for record in (grok, bot):
    if not record:
      continue
    label = str(record.get("tierLabel") or "")
    if "·" in label:
      plan = label.split("·", 1)[1].strip()
      if plan:
        break
  tier = f"{leftover}% remaining"
  if plan:
    tier += f" · {plan}"
  ready = bool((grok and grok.get("ready")) or (bot and bot.get("ready")))
  return remaining_record("grok", "Grok", ready=ready, tier_label=tier, auth_help=" ".join(helps), limits=limits)


def main() -> int:
  grok = bot = cursor = None
  for collector, slot in ((collect_grok, "grok"), (collect_grok_bot, "bot"), (collect_cursor, "cursor")):
    try:
      record = collector()
    except Exception:
      warn(f"{collector.__name__} failed")
      continue
    if slot == "grok":
      grok = record
    elif slot == "bot":
      bot = record
    else:
      cursor = record

  merged = merge_grok_records(grok, bot)
  if merged:
    write_record("grok", merged)
  drop_record("grok-bot")
  if cursor:
    write_record("cursor", cursor)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
