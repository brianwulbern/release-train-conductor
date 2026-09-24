"""Slack client - post the release announcement, two ways.

Delivery is chosen by which credential is set (token PREFERRED):
  - USER TOKEN ($CONDUCTOR_SLACK_TOKEN, xoxp-…) → `chat.postMessage`, posts AS the
    authorizing user, so the message is editable/deletable in the UI. Needs a
    channel (default #releases). Supports THREADING and returns the posted
    message's `ts`, so a caller can reply into the thread it just created.
  - INCOMING WEBHOOK ($CONDUCTOR_SLACK_WEBHOOK) → posts as the app (bot), bound by
    Slack to one channel; the message CANNOT be edited after sending, cannot be
    threaded, and returns no `ts`.

Message text uses Slack mrkdwn: links are `<url|text>`, and custom emoji like
`:warning-02:` pass through verbatim. Both delivery paths render mrkdwn.
"""
from __future__ import annotations

from typing import Optional, Tuple

import requests

from . import config

CHAT_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
PINS_ADD = "https://slack.com/api/pins.add"
PINS_REMOVE = "https://slack.com/api/pins.remove"

# (ok, error, ts) - `ts` is the posted message's Slack timestamp id, the handle a
# follow-up reply passes as `thread_ts` to land in the same thread. Only the token
# path returns a real `ts`; the webhook path returns None (it can't be threaded).
PostResult = Tuple[bool, Optional[str], Optional[str]]

# (ok, error, ts, channel_id) - the "ex" variant also reports the RESOLVED channel
# id Slack echoes back (chat.postMessage accepts a #name but pins.add needs the id).
PostResultEx = Tuple[bool, Optional[str], Optional[str], Optional[str]]


def configured() -> bool:
    return config.slack_configured()


def method(token: Optional[str] = None, webhook: Optional[str] = None) -> Optional[str]:
    """Which delivery path will be used given explicit args + env: 'token' |
    'webhook' | None. Token wins when both are available."""
    if token or config.get_slack_token():
        return "token"
    if webhook or config.get_slack_webhook():
        return "webhook"
    return None


def post_message(
    text: str,
    channel: Optional[str] = None,
    token: Optional[str] = None,
    webhook_url: Optional[str] = None,
    thread_ts: Optional[str] = None,
    broadcast: bool = False,
) -> PostResult:
    """Post `text` to Slack. Prefers the user token (posts as you, editable);
    falls back to the incoming webhook (posts as the app).

    Returns (ok, error, ts). `ts` is the new message's timestamp id - pass it back
    as `thread_ts` on a later call to reply into that thread. `thread_ts` (post
    this AS a threaded reply) is honored only on the token path; a webhook ignores
    it and returns ts=None, since incoming webhooks can neither thread nor report
    the posted message's id. `broadcast=True` (Slack's `reply_broadcast`) also
    surfaces a threaded reply back to the channel - the "Also sent to the channel"
    checkbox - and only takes effect together with `thread_ts`."""
    ok, err, ts, _channel_id = post_message_ex(
        text, channel=channel, token=token, webhook_url=webhook_url,
        thread_ts=thread_ts, broadcast=broadcast)
    return ok, err, ts


def post_message_ex(
    text: str,
    channel: Optional[str] = None,
    token: Optional[str] = None,
    webhook_url: Optional[str] = None,
    thread_ts: Optional[str] = None,
    broadcast: bool = False,
) -> PostResultEx:
    """Like `post_message`, but also returns the RESOLVED channel id Slack echoes
    back - needed to `pins.add` a message posted to a `#name` channel. The webhook
    path reports no channel id (None)."""
    tok = token or config.get_slack_token()
    if tok:
        return _post_via_token(
            text, tok, channel or config.SLACK_CHANNEL_DEFAULT,
            thread_ts=thread_ts, broadcast=broadcast)
    url = webhook_url or config.get_slack_webhook()
    if url:
        return _post_via_webhook(text, url, channel)
    return False, (
        f"no Slack credentials - set ${config.SLACK_TOKEN_ENV} (user token, posts as you) "
        f"or ${config.SLACK_WEBHOOK_ENV} (webhook, posts as an app)"
    ), None, None


def pin_message(channel: str, ts: str, token: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Pin a message via `pins.add` - best-effort, returns (ok, error). Only the
    token path can pin (a webhook has no API token); needs the `pins:write` user
    scope. `already_pinned` counts as success (idempotent). `channel` should be the
    RESOLVED channel id from the post (see post_message_ex); a `#name` may be
    rejected by pins.add with channel_not_found."""
    tok = token or config.get_slack_token()
    if not tok:
        return False, "no Slack user token"
    if not (channel and ts):
        return False, "missing channel/ts"
    try:
        resp = requests.post(
            PINS_ADD,
            headers={"Authorization": f"Bearer {tok}"},
            json={"channel": channel, "timestamp": ts},
            timeout=15,
        )
    except requests.RequestException as e:
        return False, str(e)
    try:
        data = resp.json()
    except ValueError:
        return False, f"HTTP {resp.status_code}: non-JSON response from Slack"
    if data.get("ok"):
        return True, None
    err = data.get("error") or "unknown"
    if err == "already_pinned":
        return True, None  # idempotent - the announcement is already pinned
    hint = ""
    if err == "missing_scope":
        hint = " - add the pins:write user scope to the app and reinstall"
    elif err == "no_pin_permission":
        hint = " - this Slack limits who can pin in the channel"
    return False, f"Slack API error: {err}{hint}"


def unpin_message(channel: str, ts: str, token: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Un-pin a message via `pins.remove` - best-effort, returns (ok, error). Needs
    the same `pins:write` scope as pinning. A message that ISN'T pinned (no_pin /
    not_pinned) counts as success - the goal state (not pinned) already holds."""
    tok = token or config.get_slack_token()
    if not tok:
        return False, "no Slack user token"
    if not (channel and ts):
        return False, "missing channel/ts"
    try:
        resp = requests.post(
            PINS_REMOVE,
            headers={"Authorization": f"Bearer {tok}"},
            json={"channel": channel, "timestamp": ts},
            timeout=15,
        )
    except requests.RequestException as e:
        return False, str(e)
    try:
        data = resp.json()
    except ValueError:
        return False, f"HTTP {resp.status_code}: non-JSON response from Slack"
    if data.get("ok"):
        return True, None
    err = data.get("error") or "unknown"
    if err in ("no_pin", "not_pinned", "message_not_found"):
        return True, None  # already not pinned - the desired end state holds
    hint = " - add the pins:write user scope to the app and reinstall" if err == "missing_scope" else ""
    return False, f"Slack API error: {err}{hint}"


def _post_via_token(
    text: str, token: str, channel: str,
    thread_ts: Optional[str] = None, broadcast: bool = False,
) -> PostResultEx:
    """POST chat.postMessage with a user token → the message is authored by the
    user, so it's editable in the UI. Slack returns 200 with a JSON body carrying
    `ok`; on success it also carries `ts` (the message id, used for threading). On
    failure `error` names the cause (e.g. not_in_channel, channel_not_found,
    invalid_auth). `thread_ts` (when set) posts this as a reply in that thread;
    `broadcast` adds `reply_broadcast` so that reply also shows in the channel
    (only meaningful alongside `thread_ts`)."""
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
        if broadcast:
            payload["reply_broadcast"] = True
    try:
        resp = requests.post(
            CHAT_POST_MESSAGE,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=15,
        )
    except requests.RequestException as e:
        return False, str(e), None, None
    try:
        data = resp.json()
    except ValueError:
        return False, f"HTTP {resp.status_code}: non-JSON response from Slack", None, None
    if data.get("ok"):
        return True, None, data.get("ts"), data.get("channel")
    err = data.get("error") or "unknown"
    hint = ""
    if err == "not_in_channel":
        hint = " - invite yourself/the token's user to the channel, or add channels:write"
    elif err == "invalid_auth":
        hint = f" - check ${config.SLACK_TOKEN_ENV} (a user token starts with xoxp-)"
    elif err == "missing_scope":
        hint = " - the token needs the chat:write user scope"
    return False, f"Slack API error: {err}{hint}", None, None


def _post_via_webhook(text: str, url: str, channel: Optional[str]) -> PostResultEx:
    payload = {"text": text}
    if channel:
        payload["channel"] = channel  # legacy-webhook override; modern ones ignore it
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as e:
        return False, str(e), None, None
    if resp.status_code == 200 and (resp.text or "").strip().lower() == "ok":
        return True, None, None, None
    return False, f"HTTP {resp.status_code}: {(resp.text or '').strip()[:200]}", None, None
