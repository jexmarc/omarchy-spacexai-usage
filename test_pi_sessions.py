#!/usr/bin/python3
"""Pi xAI session scan uses a temp PI_AGENT_DIR and never reads the home agent dir."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import update


DAY = "2024-06-15T12:00:00"


def _line(role: str, provider: str, usage: dict, model: str | None = "grok-3") -> str:
  message: dict = {"role": role, "provider": provider, "usage": usage}
  if model is not None:
    message["model"] = model
  return json.dumps({"timestamp": DAY, "message": message}) + "\n"


def _scan(root: Path) -> update.TokenStats:
  original = update.PI_AGENT_DIR
  update.PI_AGENT_DIR = root
  try:
    return update.scan_pi_sessions()
  finally:
    update.PI_AGENT_DIR = original


def test_xai_assistant_splits_usage_on_the_timestamp_day() -> None:
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    sessions = root / "sessions" / "nested"
    sessions.mkdir(parents=True)
    usage = {"input": 11, "output": 22, "cacheRead": 33, "cacheWrite": 44, "reasoning": 99}
    codex = {
      "timestamp": DAY,
      "message": {
        "role": "assistant",
        "provider": "openai-codex",
        "model": "gpt-5",
        "usage": {"input": 1000, "output": 2000, "cacheRead": 0, "cacheWrite": 0, "reasoning": 5},
      },
    }
    user = {
      "timestamp": DAY,
      "message": {"role": "user", "provider": "xai", "model": "grok-3", "content": "secret text"},
    }
    path = sessions / "sess-one.jsonl"
    path.write_text(
      "".join([
        _line("assistant", "xai", usage),
        json.dumps(codex) + "\n",
        json.dumps(user) + "\n",
        "{not json\n",
      ]),
      encoding="utf-8",
    )
    stats = _scan(root)
    bucket = stats.model_usage["grok-3"]
    assert bucket["inputTokens"] == 11
    assert bucket["outputTokens"] == 22
    assert bucket["cacheReadInputTokens"] == 33
    assert bucket["cacheCreationInputTokens"] == 44
    assert "gpt-5" not in stats.model_usage
    assert stats.sessions == {"sess-one"}
    assert stats.active_days == {"2024-06-15"}
    assert sum(bucket.values()) == 11 + 22 + 33 + 44


def test_missing_model_becomes_grok() -> None:
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    sessions = root / "sessions"
    sessions.mkdir()
    (sessions / "no-model.jsonl").write_text(
      _line("assistant", "xai", {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}, model=None),
      encoding="utf-8",
    )
    stats = _scan(root)
    assert "grok" in stats.model_usage
    assert stats.model_usage["grok"]["inputTokens"] == 1
    assert stats.model_usage["grok"]["outputTokens"] == 2


def test_missing_sessions_directory_is_empty() -> None:
  with tempfile.TemporaryDirectory() as tmp:
    stats = _scan(Path(tmp) / "absent")
    assert stats.model_usage == {}
    assert stats.sessions == set()
    assert stats.prompts == 0


if __name__ == "__main__":
  test_xai_assistant_splits_usage_on_the_timestamp_day()
  test_missing_model_becomes_grok()
  test_missing_sessions_directory_is_empty()
  print("ok")
