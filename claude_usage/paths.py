"""Where the tool keeps the files it makes: the parse cache, the daily price
check, this machine's id, and collect.py's data.json and dashboard.html.

They live in one data folder outside the code (~/.usage-lens), so a fresh
clone, a downloaded zip or a plugin update keeps the same machine id and a warm
cache. Kept with the code, each new copy would start a new id, and the team
merge would count one person's machine twice.

CLAUDE_USAGE_DATA_DIR, or --data-dir on either entry point, moves the folder.
Read on every call rather than at import, so the flag works however late it is
parsed.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "CLAUDE_USAGE_DATA_DIR"
DEFAULT_DATA_DIR = "~/.usage-lens"

# Where versions before the data folder kept their files: .cache/ beside the
# code. Only the machine id is carried over from it, once (see
# context.adopt_legacy_machine_id); the parse cache rebuilds itself.
LEGACY_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"


def data_dir() -> Path:
    # Absolute, so a relative CLAUDE_USAGE_DATA_DIR doesn't follow the current
    # directory from one run to the next.
    return Path(os.path.abspath(os.path.expanduser(os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)))


def cache_dir() -> Path:
    return data_dir() / "cache"


def set_data_dir(path) -> None:
    """--data-dir: the same as setting the variable, for this process."""
    os.environ[DATA_DIR_ENV] = os.path.abspath(os.path.expanduser(path))
