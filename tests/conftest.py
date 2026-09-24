"""Shared test fixtures.

The important one isolates on-disk state. Several command `run()` paths call
`M.record_action` / `M.default_manifest_path`, which read and WRITE the operator's real
`~/.conductor/state` manifests. Redirecting `config.STATE_DIR` to a per-test temp dir makes the
suite physically unable to touch real state, whether or not an individual test remembered to
mock those calls. The credential env vars are cleared too, so no test can reach a live service
with a token from the developer's shell.
"""
import pytest

from conductor import config

_CRED_ENVS = (
    config.JIRA_TOKEN_ENV, config.JIRA_EMAIL_ENV, config.GITHUB_TOKEN_ENV, "GITHUB_TOKEN",
    config.SLACK_TOKEN_ENV, config.SLACK_WEBHOOK_ENV,
)


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "conductor-state"
    state.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state)
    for name in _CRED_ENVS:
        monkeypatch.delenv(name, raising=False)
    return state
