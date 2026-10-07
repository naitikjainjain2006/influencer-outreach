"""Config + environment loading."""
import os
from pathlib import Path

import yaml

try:  # .env support is optional
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with p.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    for section in ("campaign", "discovery", "filtering", "lexicon", "enrichment",
                    "personalization", "sending", "storage"):
        if section not in cfg:
            raise ValueError(f"config.yaml is missing required section: '{section}'")
    return cfg


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
