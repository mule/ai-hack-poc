"""Exercise real Godot lifecycle HTTP against an isolated rules-only director.

Exit 2 means local game delivery succeeded but remote persistence is unverified.
The run ID in JSON can be used for independent OpenLIT storage verification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from benchmarks.openlit_smoke import build_identity, evidence_label


def run(game_project: Path, godot: str, port: int) -> tuple[int, dict]:
    import uvicorn
    from dungeon_director.app import create_app
    from dungeon_director.registry import ProviderRegistry
    from dungeon_director.rules import RulesProvider
    from dungeon_director.settings import DirectorSettings
    from dungeon_director.telemetry import TelemetrySettings, setup_telemetry

    marker = "game-smoke-" + uuid.uuid4().hex[:20]
    evidence: dict = {"status": "failed", "ingestion_verified": False, "run_id": marker}
    telemetry = None
    server = None
    worker = None
    listener = None
    try:
        settings = TelemetrySettings.from_env()
        if not settings.enabled or not all(
            settings.signal_enabled(s) for s in ("traces", "metrics", "logs")
        ):
            raise RuntimeError("export_required")
        resource = dict(settings.resource_attributes)
        resource["service.instance.id"] = marker
        telemetry = setup_telemetry(replace(settings, resource_attributes=tuple(resource.items())))
        if not telemetry.enabled:
            raise RuntimeError("export_setup_failed")
        registry = ProviderRegistry()
        registry.register(RulesProvider())
        app = create_app(DirectorSettings(), registry, telemetry)
        # Prebind: an occupied requested port is a failure, never reuse another server.
        listener = socket.socket()
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        actual_port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        worker = threading.Thread(
            target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
        )
        worker.start()
        startup_deadline = time.monotonic() + 10
        while not server.started:
            if not worker.is_alive() or time.monotonic() > startup_deadline:
                raise RuntimeError("director_start_failed")
            time.sleep(0.05)
        # Godot needs no provider/OTLP credentials. Give it only runtime essentials.
        child_env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG") if k in os.environ}
        child_env.update(
            DUNGEON_DIRECTOR_URL=f"http://127.0.0.1:{actual_port}",
            DUNGEON_TELEMETRY_ENABLED="1",
            DUNGEON_SMOKE_RUN_ID=marker,
        )
        with tempfile.TemporaryDirectory(prefix="game-openlit-smoke-") as temporary:
            logfile = Path(temporary) / "godot.log"
            common = [godot, "--headless", "--path", str(game_project), "--log-file", str(logfile)]
            # Register classes before loading the external harness script.
            warm = subprocess.run(
                [*common, "--editor", "--quit"],
                env=child_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if warm.returncode != 0:
                raise RuntimeError("godot_import_failed")
            started_ms = time.time_ns() // 1_000_000
            result = subprocess.run(
                [*common, "--script", str(Path(__file__).with_suffix(".gd"))],
                env=child_env,
                capture_output=True,
                text=True,
                timeout=25,
            )
            log = result.stdout + result.stderr
            if logfile.exists():
                log += logfile.read_text()
            if result.returncode or any(
                token in log for token in ("SCRIPT ERROR:", "Parse Error:", "Failed to load script")
            ):
                raise RuntimeError("godot_run_failed")
            lines = [
                line for line in result.stdout.splitlines() if line.startswith("GAME_SMOKE_RESULT=")
            ]
            if len(lines) != 1:
                raise RuntimeError("godot_result_missing")
            game = json.loads(lines[0].split("=", 1)[1])
            if game["run_id"] != marker or game["rooms"] < 2 or game["events_sent"] < 5:
                raise RuntimeError("godot_lifecycle_incomplete")
        telemetry.flush(5000)
        identity = telemetry.describe().get("resource", {})
        evidence.update(
            status="game_delivered_unverified",
            started_ms=started_ms,
            build=build_identity(),
            game_build=build_identity(game_project),
            service_name=evidence_label(identity.get("service.name")),
            service_version=evidence_label(identity.get("service.version")),
            environment=evidence_label(identity.get("deployment.environment")),
            provider=evidence_label(game["provider"]),
            model=evidence_label(game["model"]),
            events_sent=game["events_sent"],
            room_id=evidence_label(game["room_id"]),
            director_port=actual_port,
        )
        return 2, evidence
    except Exception as exc:
        # Fixed error categories only, never subprocess output or endpoint credentials.
        allowed = {
            "export_required",
            "export_setup_failed",
            "director_start_failed",
            "godot_import_failed",
            "godot_run_failed",
            "godot_result_missing",
            "godot_lifecycle_incomplete",
        }
        evidence["error"] = (
            str(exc)
            if isinstance(exc, RuntimeError) and str(exc) in allowed
            else "game_smoke_failed"
        )
        return 1, evidence
    finally:
        if server is not None:
            server.should_exit = True
        if worker is not None:
            worker.join(8)
        if listener is not None:
            listener.close()
        if telemetry is not None and worker is None:
            shutdown = threading.Thread(target=telemetry.shutdown, daemon=True)
            shutdown.start()
            shutdown.join(5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-project", type=Path, default=Path("game"))
    parser.add_argument("--godot", default="godot")
    parser.add_argument("--port", type=int, default=18000)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0-65535")
    logging.disable(logging.CRITICAL)
    code, evidence = run(args.game_project.resolve(), args.godot, args.port)
    print(json.dumps(evidence, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
