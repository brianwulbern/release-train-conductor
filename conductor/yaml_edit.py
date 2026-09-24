"""Surgical YAML edits.

The goal here is byte-minimal diffs: only the value on the target line should
change, everything else (comments, blank lines, quote styles, ordering,
trailing whitespace) stays exactly as it was. We use ruamel.yaml only to
*find* the line of the target value, then do simple text replacement.
This avoids the round-trip quirks ruamel can introduce when re-dumping.
"""
from __future__ import annotations

import re
from io import StringIO
from typing import Optional, Tuple

from ruamel.yaml import YAML


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 10_000
    return y


def find_value_line(yaml_text: str, dotted_key: str) -> Optional[int]:
    """Return 1-indexed line number where the value at dotted_key lives, or None.

    ruamel.yaml's `lc` (line/column) attribute on mapping nodes gives us the
    position of each key's *value*. We walk the key path and pull lc info from
    the parent of the leaf key.
    """
    try:
        data = _yaml().load(yaml_text)
    except Exception:
        return None
    if data is None:
        return None

    parts = dotted_key.split(".")
    cur = data
    for i, part in enumerate(parts):
        # Need a mapping that supports __contains__ and lc.value()
        if not hasattr(cur, "__contains__") or part not in cur:
            return None
        if i == len(parts) - 1:
            # Leaf - pull line from the parent's lc info for this key.
            try:
                line, _col = cur.lc.value(part)
                return line + 1  # 1-indexed
            except Exception:
                return None
        cur = cur[part]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Line-level surgical edit
# ─────────────────────────────────────────────────────────────────────────────

# Matches "    key: value" with optional inline comment, preserves quote style.
_LINE_RE = re.compile(
    r"""^
        (?P<indent>\s*)
        (?P<key>[^:\s][^:]*)
        :
        [ \t]*
        (?P<quote_open>["']?)
        (?P<value>.*?)
        (?P=quote_open)
        [ \t]*
        (?P<comment>\#.*)?
        \r?\n?$
    """,
    re.VERBOSE,
)


def _replace_value_in_line(line: str, new_value: str) -> Optional[str]:
    """Replace just the value portion of a `key: value` line.

    Preserves leading indent, the key text, any quote style around the value,
    inline comments, and trailing newline. Returns None if the line doesn't
    parse as a key/value pair.
    """
    m = _LINE_RE.match(line)
    if not m:
        return None

    indent = m.group("indent")
    key = m.group("key")
    quote = m.group("quote_open") or ""
    comment = m.group("comment")
    eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")

    new_line = f"{indent}{key}: {quote}{new_value}{quote}"
    if comment:
        new_line += f"  {comment}"
    new_line += eol
    return new_line


def edit_value_at_key(
    yaml_text: str,
    dotted_key: str,
    new_value: str,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Surgically replace the value at dotted_key.

    Returns (new_text, line_no, error).
        - On success: (new_text, 1-indexed line of the changed line, None)
        - On failure: (None, None, error message)

    Preserves the original text byte-for-byte except for the changed value.
    """
    line_no = find_value_line(yaml_text, dotted_key)
    if line_no is None:
        return None, None, f"key '{dotted_key}' not found"

    lines = yaml_text.splitlines(keepends=True)
    if line_no > len(lines):
        return None, None, f"line {line_no} out of range (file has {len(lines)} lines)"

    original = lines[line_no - 1]
    replaced = _replace_value_in_line(original, new_value)
    if replaced is None:
        return None, None, f"line {line_no} doesn't match expected key/value pattern: {original!r}"

    lines[line_no - 1] = replaced
    return "".join(lines), line_no, None
