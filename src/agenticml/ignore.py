"""Minimal gitignore-style pattern matcher.

Supports the common subset:
- blank lines and lines starting with '#' are ignored
- '*' matches any chars except '/'; '**' matches across directories
- '?' matches a single char except '/'
- trailing '/' restricts the pattern to directories
- leading '!' negates a previous match
- leading '/' anchors to the repo root; otherwise the pattern can match at any depth

Not a perfect gitignore reimplementation, but covers the cases that matter for
keeping datasets, virtualenvs, secrets, and model checkpoints out of code
snapshots.
"""

from __future__ import annotations

import re
from pathlib import Path


def _translate(pattern: str) -> str:
    """Translate a gitignore-style glob to a regex."""
    i = 0
    out = []
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                # Handle '**/' or trailing '**'
                if i + 2 < len(pattern) and pattern[i + 2] == "/":
                    out.append(r"(?:.*/)?")
                    i += 3
                    continue
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        elif c == "?":
            out.append(r"[^/]")
        elif c == ".":
            out.append(r"\.")
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class _Rule:
    __slots__ = ("regex", "negate", "dir_only")

    def __init__(self, pattern: str):
        self.negate = pattern.startswith("!")
        if self.negate:
            pattern = pattern[1:]

        self.dir_only = pattern.endswith("/")
        if self.dir_only:
            pattern = pattern[:-1]

        anchored = pattern.startswith("/")
        if anchored:
            pattern = pattern[1:]

        body = _translate(pattern)
        if anchored:
            regex = f"^{body}(?:/.*)?$"
        else:
            # Match the pattern at any depth: as a full path or as a suffix
            # following a "/".
            regex = f"^(?:.*/)?{body}(?:/.*)?$"
        self.regex = re.compile(regex)


class IgnoreMatcher:
    """Combine pattern files (e.g. .gitignore, .agenticmlignore) into one matcher.

    Patterns are evaluated in order; later matches override earlier ones, so
    `!important.log` after `*.log` keeps `important.log` tracked.
    """

    def __init__(self, patterns: list[str] | None = None):
        self.rules: list[_Rule] = []
        if patterns:
            self.extend(patterns)

    def extend(self, patterns):
        for raw in patterns:
            line = raw.rstrip("\n").rstrip("\r")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.rules.append(_Rule(line))

    @classmethod
    def from_files(cls, repo_root: Path, filenames: list[str]) -> "IgnoreMatcher":
        matcher = cls()
        for name in filenames:
            path = Path(repo_root) / name
            if not path.exists():
                continue
            try:
                matcher.extend(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
        return matcher

    def matches(self, relative_path: str, *, is_dir: bool = False) -> bool:
        path = relative_path.replace("\\", "/").lstrip("/")
        ignored = False
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(path):
                ignored = not rule.negate
        return ignored
