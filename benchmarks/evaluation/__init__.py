"""POC evaluation protocol (issue #18).

Executable side of ``docs/evaluation-methodology.md``: a fixed replay corpus with a
checksummed manifest, a paired and interleaved runner that records versions and
configuration, and a report that keeps measured, derived and subjective material
apart. Latency/reliability/token summaries are *not* computed here: they come from
``python -m benchmarks.summarize`` (issue #15), which this package only consumes.
"""

from __future__ import annotations

BUNDLE_SCHEMA_VERSION = "1.0.0"
