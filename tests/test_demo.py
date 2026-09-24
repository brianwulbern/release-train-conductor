"""The demo is the showcase's end-to-end test: the real pipeline code against the fakes."""
import importlib.util
from pathlib import Path


def _load_demo():
    path = Path(__file__).resolve().parent.parent / "demo.py"
    spec = importlib.util.spec_from_file_location("demo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_runs_the_train_end_to_end(capsys):
    assert _load_demo().main() == 0
    out = capsys.readouterr().out

    # the charts bump touched only the two version lines
    assert '-    tag: "5.229.0"' in out and '+    tag: "master_1a2b3c"' in out
    assert "DEFAULT_CI_IMAGE" in out and "-      DEFAULT_CI_IMAGE" not in out

    # the scope freeze posted once, even when asked twice
    assert "attempt 2: 1 scope-freeze post(s)" in out

    # the checklist closed the out-of-line step (highest key, shown first) as a prior
    assert "REL-2009 Bump service VERSION.txt files: Open → Closed" in out

    # drift caught against the frozen test scope, then walled out to NEXT
    assert "['REL-2103'] rode in after the test scope was frozen" in out
    assert "after the next Refresh: drift = []" in out

    # finalize advanced the ladder exactly once and minted the next train + its checklist
    assert "2030-04-04 (CURRENT)" in out and "2030-04-16 (SUBSEQUENT)" in out
    assert "[Release] Release Steps for Release Train for 2030-04-16" in out
    assert "prerelease=False, service-backup 5.41.0 prerelease=False" in out
    assert "Nothing to do - version is already finalized." in out

    # a degraded ladder gets a date-anchored repair plan
    assert "rename '2030-04-09 (NEXT)' -> '2030-04-09 (CURRENT)'" in out
