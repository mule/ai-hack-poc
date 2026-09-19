"""The live Groq tests are opt-in twice over, and ordinary tests never dial out.

Each case runs pytest in a subprocess with a controlled environment and a
loopback "tripwire" listener standing in for Groq (``GROQ_API_BASE_URL`` points
at it). That lets the suite prove, without spending anything:

* credentials alone never enable the live tests or any network call;
* only ``RUN_LIVE_GROQ=1`` *plus* a key does;
* the live tests themselves are correct against a faithful fake Groq (so a
  real run failing points at Groq or the prompt, not at a broken test).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DIRECTOR_DIR = Path(__file__).resolve().parents[1]
GOOD_KEY = "gsk_fake_good_key"


def run_pytest(extra_env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("GROQ_", "RUN_LIVE_"))
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *args],
        cwd=DIRECTOR_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.fixture
def tripwire() -> Iterator[tuple[str, socket.socket]]:
    """A loopback listener that fails the test if anything connects to it."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}/openai/v1"
    yield base_url, listener
    listener.close()


def assert_untouched(listener: socket.socket) -> None:
    with pytest.raises(TimeoutError):
        listener.accept()


# --- gating -------------------------------------------------------------------


def test_credentials_alone_never_enable_live_tests_or_any_network_call(tripwire):
    base_url, listener = tripwire

    result = run_pytest(
        {"GROQ_API_KEY": GOOD_KEY, "GROQ_API_BASE_URL": base_url}, "tests/test_groq_live.py"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 skipped" in result.stdout
    assert "passed" not in result.stdout
    assert "RUN_LIVE_GROQ=1" in result.stdout, "the skip reason must say how to opt in"
    assert_untouched(listener)


@pytest.mark.parametrize("value", ["0", "true", "yes", "11", ""])
def test_only_the_exact_opt_in_value_enables_live_tests(tripwire, value):
    base_url, listener = tripwire

    result = run_pytest(
        {"GROQ_API_KEY": GOOD_KEY, "GROQ_API_BASE_URL": base_url, "RUN_LIVE_GROQ": value},
        "tests/test_groq_live.py",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 skipped" in result.stdout
    assert_untouched(listener)


def test_opt_in_without_a_key_skips_instead_of_failing(tripwire):
    base_url, listener = tripwire

    result = run_pytest(
        {"GROQ_API_BASE_URL": base_url, "RUN_LIVE_GROQ": "1"}, "tests/test_groq_live.py"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 skipped" in result.stdout
    assert_untouched(listener)


def test_the_whole_offline_groq_suite_never_dials_out_even_with_key_and_opt_in(tripwire):
    base_url, listener = tripwire

    result = run_pytest(
        {"GROQ_API_KEY": GOOD_KEY, "GROQ_API_BASE_URL": base_url, "RUN_LIVE_GROQ": "1"},
        "tests/test_groq.py",
        "tests/test_registry.py",
        "tests/test_api.py",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout and "failed" not in result.stdout
    assert_untouched(listener)


# --- the live tests against a faithful fake Groq --------------------------------


class FakeGroq(BaseHTTPRequestHandler):
    """Just enough of Groq's chat-completions endpoint to satisfy the live tests."""

    def log_message(self, *_args) -> None:  # keep test output quiet
        pass

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if self.path != "/openai/v1/chat/completions":
            return self._reply(404, {"error": {"message": "not found"}})
        if self.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            return self._reply(
                401, {"error": {"message": f"invalid key {self.headers.get('Authorization')}"}}
            )
        assert request["response_format"]["json_schema"]["strict"] is True
        assert request["reasoning_effort"] == "low" and request["include_reasoning"] is False
        state = json.loads(request["messages"][1]["content"])
        options = state.get("options", {})
        room = {
            "room_id": state["room_id"],
            "depth": state["depth"],
            "room_type": "chamber",
            "size": "small",
            "danger": min(2, options.get("max_danger", 5)),
            "exits": [
                {"direction": state["back_direction"], "kind": state["back_kind"], "locked": False}
            ],
            "enemy_density": 0.2,
            "loot_density": 0.3,
            "secret_probability": 0.0,
            "has_secret": False,
            "environmental_tags": [],
            "description": None,
        }
        self._reply(
            200,
            {
                "id": "chatcmpl-fake",
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(room)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "queue_time": 0.01,
                    "prompt_tokens": 700,
                    "prompt_time": 0.002,
                    "completion_tokens": 90,
                    "completion_time": 0.05,
                    "total_tokens": 790,
                    "total_time": 0.052,
                },
                "x_groq": {"id": "req_fake"},
            },
        )


@pytest.fixture
def fake_groq() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGroq)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/openai/v1"
    server.shutdown()
    server.server_close()


def test_live_tests_pass_against_a_faithful_local_fake(fake_groq):
    result = run_pytest(
        {"GROQ_API_KEY": GOOD_KEY, "GROQ_API_BASE_URL": fake_groq, "RUN_LIVE_GROQ": "1"},
        "tests/test_groq_live.py",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    assert GOOD_KEY not in result.stdout + result.stderr
