"""Subcommand implementations."""


class _NoEdits(int):
    """Sentinel exit code: a bump dry-run completed cleanly with NOTHING to bump
    (every repo already at target, or all skipped). Subclasses int and equals 0,
    so the CLI still exits 0; the interactive menu checks `rc is NO_EDITS` to skip
    the now-pointless "Apply + push?" confirmation (there's nothing to apply)."""
    __slots__ = ()


NO_EDITS = _NoEdits(0)
