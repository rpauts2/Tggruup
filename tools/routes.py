"""Dev helper: list the routes registered on the FastAPI app."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tgfarm.api import create_app  # noqa: E402

app = create_app()
for route in app.routes:
    methods = ",".join(sorted(getattr(route, "methods", []) or []))
    print(f"{methods:<18} {route.path}")