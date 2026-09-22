"""Suite-wide offline guard.

Ordinary tests must never reach a real provider, even on a machine that has
provider credentials exported. For every test that is not marked ``live``:

* ambient OpenTelemetry exporter settings are removed for every test, including
  live provider tests, so test runs cannot leak telemetry to another service;
* hosted-provider keys and live opt-ins are removed from the environment, so a
  developer's real key cannot silently make a provider "available";
* outbound sockets to anything but loopback (and DNS lookups for anything but
  loopback) are refused, and the test fails at teardown if anything tried.
  Recording the attempt matters: adapters catch ``Exception`` and turn it into
  a classified provider error, which would otherwise hide the escape.

Tests marked ``live`` opt out; they are gated separately by their own
``RUN_LIVE_*`` variable plus credentials.
"""

import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_LOOPBACK_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}
_TELEMETRY_ENV = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "DIRECTOR_OTEL_ENABLED",
    "OTEL_SERVICE_NAME",
    "DIRECTOR_OTEL_NAMESPACE",
    "DIRECTOR_OTEL_ENVIRONMENT",
    "DIRECTOR_OTEL_SERVICE_VERSION",
    "DIRECTOR_OTEL_TRACES_ENABLED",
    "DIRECTOR_OTEL_METRICS_ENABLED",
    "DIRECTOR_OTEL_LOGS_ENABLED",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
    "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_METRIC_EXPORT_INTERVAL",
    "OTEL_RESOURCE_ATTRIBUTES",
)


def _is_local_host(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    return host in _LOOPBACK_HOSTS or str(host).startswith("127.")


def _is_local_address(address: Any) -> bool:
    if isinstance(address, str | bytes):  # AF_UNIX path
        return True
    return _is_local_host(address[0])


@pytest.fixture(autouse=True)
def offline_guard(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[str] | None]:
    """Yield the list of refused network attempts (``None`` for live tests).

    A test that deliberately provokes the guard must ``clear()`` the list.
    """
    for name in _TELEMETRY_ENV:
        monkeypatch.delenv(name, raising=False)

    if request.node.get_closest_marker("live"):
        yield None
        return

    for name in [name for name in os.environ if name.startswith("GROQ_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("RUN_LIVE_GROQ", raising=False)
    for name in [name for name in os.environ if name.startswith("TYPESAFE_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("RUN_LIVE_TYPESAFE_JEV", raising=False)

    attempts: list[str] = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local_host(host):
            attempts.append("dns")
            raise OSError("network access is disabled in offline tests")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _is_local_address(address):
            attempts.append("connect")
            raise OSError("network access is disabled in offline tests")
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if not _is_local_address(address):
            attempts.append("connect")
            return 1
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    yield attempts

    assert not attempts, (
        f"offline test tried to reach the network ({len(attempts)} attempt(s)); "
        "inject a fake transport or mark the test 'live'"
    )
