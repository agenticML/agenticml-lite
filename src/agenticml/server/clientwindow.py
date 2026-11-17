"""Client support window: reject old agenticml-lite clients on /v2 requests."""
from __future__ import annotations

import re

DEFAULT_MIN_CLIENT = "2.2"
_MINIMUM = re.compile(r"(\d{1,4})\.(\d{1,4})(?:\.\d{1,4})?")
_AGENT = re.compile(r"agenticml-lite/(\d{1,4})\.(\d{1,4})(?![0-9])")


def parse_min_client(value):
    """Parse ``X.Y`` (or ``X.Y.Z``) into ``(major, minor)``; ValueError if malformed."""
    text = str(value if value is not None else DEFAULT_MIN_CLIENT).strip()
    match = _MINIMUM.fullmatch(text)
    if match is None:
        raise ValueError("--min-client-version must look like 2.2")
    return int(match.group(1)), int(match.group(2))


def too_old(user_agent, minimum):
    """True when the User-Agent names an agenticml-lite release below ``minimum``.

    A missing or foreign User-Agent (curl, other tools) is never too old, so the
    protocol stays open to non-agenticml clients.
    """
    if not user_agent:
        return False
    match = _AGENT.match(user_agent.strip())
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) < minimum


def upgrade_message(minimum):
    return (f"this server needs agenticml-lite >= {minimum[0]}.{minimum[1]}; "
            "run: pip install -U agenticml-lite")
