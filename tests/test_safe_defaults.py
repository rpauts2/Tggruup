from __future__ import annotations

from pathlib import Path
import re


def test_env_template_defaults_to_dry_mode_and_offline_generation():
    root = Path(__file__).resolve().parents[1]
    template = (root / ".env.example").read_text(encoding="utf-8")

    assert re.search(r"^TGFARM_MODE=dry$", template, flags=re.MULTILINE)
    assert re.search(r"^LLM_PROVIDERS=offline$", template, flags=re.MULTILINE)
    assert not re.search(r"^TGFARM_MODE=live$", template, flags=re.MULTILINE)
    assert not re.search(r"^LLM_PROVIDERS=(?!offline$).+", template, flags=re.MULTILINE)
