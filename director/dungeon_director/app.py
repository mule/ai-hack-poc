"""Director service entrypoint.

Placeholder only: exposes a health check so the service can be started and
probed locally. The canonical dungeon-decision endpoint and provider adapters
are added by later issues under epic #1.
"""

from fastapi import FastAPI

app = FastAPI(title="Dungeon Director")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "dungeon-director"}
