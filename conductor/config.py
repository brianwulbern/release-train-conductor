"""Configuration: env vars, hosts, and state location.

This is the showcase's configuration. The production tool's version also holds the internal
hosts, test-job paths, reviewer ids and project keys of its deployment; here every one of those
is a placeholder (`example.com`, a `REL` project), so nothing points at a real system. Secrets
never live in code: every credential is read from the environment at call time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Auth (env var NAMES only; the values come from the operator's shell)
# ─────────────────────────────────────────────────────────────────────────────

JIRA_TOKEN_ENV = "CONDUCTOR_JIRA_TOKEN"
JIRA_EMAIL_ENV = "CONDUCTOR_JIRA_EMAIL"
GITHUB_TOKEN_ENV = "CONDUCTOR_GITHUB_TOKEN"  # also accepts $GITHUB_TOKEN

# Slack delivery for the threaded release-cycle updates. Token PREFERRED when both are set:
#   CONDUCTOR_SLACK_TOKEN    a Slack USER token with chat:write. Posts AS the operator, so the
#                            message is editable, and it can reply into a thread.
#   CONDUCTOR_SLACK_WEBHOOK  an incoming-webhook URL. Posts as an app, bound to one channel,
#                            and can neither be edited nor threaded.
SLACK_WEBHOOK_ENV = "CONDUCTOR_SLACK_WEBHOOK"
SLACK_TOKEN_ENV = "CONDUCTOR_SLACK_TOKEN"
SLACK_CHANNEL_DEFAULT = "#releases"
# Slack member ids (U...) @-mentioned on the production-promote post so the charts reviewers
# get pinged. Empty here; a deployment lists its reviewers.
SLACK_PROMOTE_REVIEWERS: list = []
# Skin-tone modifier for skin-tonable emoji in posts. The API sends emoji literally, so it
# does not inherit the poster's own preference. "" = default yellow.
SLACK_SKIN_TONE = "🏻"  # light

# ─────────────────────────────────────────────────────────────────────────────
# Hosts and keys (placeholders)
# ─────────────────────────────────────────────────────────────────────────────

JIRA_HOST = "https://tracker.example.com"
JIRA_PROJECT = "REL"  # the tracker project that holds the release-train versions

# The tracker's "Git Pull Request" custom field (instance-specific id).
JIRA_GIT_PR_FIELD = "customfield_10000"

# The "[Release] Release Steps ... [TEMPLATE]" ticket cloned for each new train (a parent
# Task + its checklist subtasks). Its summary carries a literal `YYYY-MM-DD [TEMPLATE]` token
# that the clone swaps for the train date.
RELEASE_STEPS_TEMPLATE_KEY = "REL-47502"
RELEASE_STEPS_DATE_TOKEN = "YYYY-MM-DD"
RELEASE_STEPS_TEMPLATE_MARKER = " [TEMPLATE]"

GITHUB_API = "https://api.github.com"
GITHUB_ORG = "example-org"

# Deploy sync order from the runbook: the controller first, then the api, then the rest.
ARGOCD_SYNC_ORDER_PREFIX = ("workloads-controller", "api")
ARGOCD_OUT_OF_SYNC_URL = "https://argocd.example.com/applications?sync=OutOfSync"

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

STATE_DIR = Path.home() / ".conductor" / "state"


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


# ─────────────────────────────────────────────────────────────────────────────
# Non-exiting "is it configured?" checks (these never sys.exit)
# ─────────────────────────────────────────────────────────────────────────────


def jira_configured() -> bool:
    return bool(os.environ.get(JIRA_EMAIL_ENV)) and bool(os.environ.get(JIRA_TOKEN_ENV))


def slack_token_configured() -> bool:
    return bool(os.environ.get(SLACK_TOKEN_ENV))


def slack_webhook_configured() -> bool:
    return bool(os.environ.get(SLACK_WEBHOOK_ENV))


def slack_configured() -> bool:
    """True if EITHER a Slack user token or an incoming-webhook URL is set."""
    return slack_token_configured() or slack_webhook_configured()


def get_slack_token():
    """The Slack user token, or None if unset. Never exits."""
    return os.environ.get(SLACK_TOKEN_ENV) or None


def get_slack_webhook():
    """The Slack webhook URL, or None if unset. Never exits: Slack is optional."""
    return os.environ.get(SLACK_WEBHOOK_ENV) or None


def github_configured() -> bool:
    return bool(os.environ.get(GITHUB_TOKEN_ENV) or os.environ.get("GITHUB_TOKEN"))


def missing_required_creds() -> list:
    """Short labels of required credentials that look unconfigured (cheap check)."""
    missing = []
    if not jira_configured():
        missing.append("Jira")
    if not github_configured():
        missing.append("GitHub")
    return missing


# ─────────────────────────────────────────────────────────────────────────────
# Token accessors (exit with a clear message when a required credential is missing)
# ─────────────────────────────────────────────────────────────────────────────


def get_jira_token() -> str:
    token = os.environ.get(JIRA_TOKEN_ENV)
    if not token:
        sys.exit(f"error: ${JIRA_TOKEN_ENV} not set. Add it to your shell rc:\n"
                 f"  export {JIRA_TOKEN_ENV}=your_token_here")
    return token


def get_jira_email() -> str:
    email = os.environ.get(JIRA_EMAIL_ENV)
    if not email:
        sys.exit(f"error: ${JIRA_EMAIL_ENV} not set.\n"
                 f"Tracker API tokens authenticate via Basic auth, with your account email\n"
                 f"as the username. Add to your shell rc:\n"
                 f"  export {JIRA_EMAIL_ENV}=you@example.com")
    return email


def get_github_token() -> str:
    """$CONDUCTOR_GITHUB_TOKEN, else $GITHUB_TOKEN. (The production tool also falls back to
    `gh auth token`, which handles org SSO for you.)"""
    token = os.environ.get(GITHUB_TOKEN_ENV) or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    sys.exit(f"error: no GitHub credentials available. Set ${GITHUB_TOKEN_ENV} "
             f"(or $GITHUB_TOKEN) to a token with repo scope.")
