"""Minimal .env loader (standard library only).

Format: one KEY=VALUE per line. Blank lines and lines starting with # are
ignored, an optional leading `export ` is allowed, and a value may be wrapped
in single or double quotes. Variables already set in the real environment
win over the file. Values are never logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV_BYTES = 64 * 1024


class EnvFileError(ValueError):
    pass


def parse_env(text: str, source: str = ".env") -> dict[str, str]:
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise EnvFileError(f"{source} line {lineno}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()  # trailing comment on an unquoted value
        out[key] = value
    return out


def load_env(path: Path | str, environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Read `path` if it exists and add its variables to `environ` (default os.environ)
    without overriding ones already set. Returns the variables the file defined."""
    env = os.environ if environ is None else environ
    p = Path(path)
    try:
        if p.stat().st_size > MAX_ENV_BYTES:
            raise EnvFileError(f"{p.name} is larger than {MAX_ENV_BYTES} bytes")
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise EnvFileError(f"cannot read {p}: {exc}") from None
    values = parse_env(text, p.name)
    for k, v in values.items():
        env.setdefault(k, v)
    return values
