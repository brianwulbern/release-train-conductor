"""YAML traversal helpers for chart_location lookups and bump previews."""
from __future__ import annotations

import re
from typing import Any, Optional

import yaml


def stage_for_file(path: str) -> Optional[str]:
    """Return 'stg', 'prd', or None for a yaml file path.

    Handles three conventions:
      - foo/stg.yaml, foo/prd.yaml                 (most repos)
      - foo/stg/common.yaml, foo/prd/common.yaml   (backup-controller)
      - foo/stg-anything.yaml, foo/prd-anything.yaml  (future-proofing)
    """
    p = str(path).replace("\\", "/").lower()
    # Strip the filename for the "/stg/" or "/prd/" directory test.
    if "/stg/" in p or p.endswith("/stg.yaml"):
        return "stg"
    if "/prd/" in p or p.endswith("/prd.yaml"):
        return "prd"
    return None


def has_placeholder(path: str) -> bool:
    """True if the path contains <ANGLE_BRACKETS> indicating unverified config."""
    return "<" in str(path) and ">" in str(path)


def get_value_at_key(yaml_text: str, dotted_key: str) -> Optional[Any]:
    """Return the leaf value at the given dotted key path, or None if not found.

    Used for read-only preview. Surgical edits use a different helper that
    preserves formatting.
    """
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return None
    if parsed is None:
        return None
    cur: Any = parsed
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def parse_image_tag_prefix(current_value: Any) -> str:
    """The packaged-version prefix of an `image_with_tag` current value - the part
    before the trailing X.Y.Z (and any `-master_<hash>`). Returns '' when there's
    no prefix or it can't be parsed. Examples:

        'acme/app-server:7.4-5.12.0'            → '7.4-'
        'acme/app-server:7.4-5.13.0-master_abc' → '7.4-'
        'acme/nginx:1.31.2-6.10.0'               → '1.31.2-'
        'acme/jenkins:2.541.3-lts-jdk21-6.4.0'   → '2.541.3-lts-jdk21-'
        'acme/backup:5.15.0'                     → ''

    Used both to reconstruct a bumped tag (compute_new_value) and to pick the
    right published release image when several base-track variants share a
    version (e.g. app-server ships 7.1-/7.2-/7.3-/7.4- for the same X.Y.Z)."""
    current_tag = ""
    if isinstance(current_value, str) and ":" in current_value:
        _, current_tag = current_value.split(":", 1)
    elif isinstance(current_value, str):
        current_tag = current_value
    # Strip trailing -master_{hash} to get the {prefix?}{version} portion.
    m_hash = re.match(r"^(.*?)-master_[0-9a-f]+$", current_tag)
    without_hash = m_hash.group(1) if m_hash else current_tag
    # Pull out a leading "<prefix>-" before the final X.Y.Z version, if any. The
    # leading .* is greedy, so jenkins "2.541.3-lts-jdk21-6.4.0" correctly yields
    # prefix "2.541.3-lts-jdk21-" and version "6.4.0".
    m_pref = re.match(r"^(.*-)?(\d+\.\d+\.\d+)$", without_hash)
    return m_pref.group(1) if (m_pref and m_pref.group(1)) else ""


def resolve_image_base(configured_prefix: str, stg_base: str, current_value) -> tuple:
    """Pick the base-track prefix for an `image_with_tag` bump AND say where it
    came from - the single source of truth for that precedence, shared by the
    prd-bump decision and the preview reasoning (so they can't drift).

    Precedence: configured `tag_prefix` → the stg track (prd follows what stg
    tested) → the line's own current value. Returns (base, source) where source
    is one of "configured tag_prefix" | "stg track" | "current value" | "none".
    (stg-bump callers pass stg_base="" - for them it collapses to configured →
    current.)"""
    if configured_prefix:
        return configured_prefix, "configured tag_prefix"
    if stg_base:
        return stg_base, "stg track"
    cur = parse_image_tag_prefix(current_value)
    if cur:
        return cur, "current value"
    return "", "none"


def compute_new_value(
    format_type: str,
    current_value: Any,
    new_tag: str,
    tag_prefix: str = "",
    version_txt: str = "",
    published_tag: str = "",
) -> str:
    """Build the new value string for a chart_location, given its format.

    `published_tag`, when supplied (image_with_tag only), is the authoritative
    full tag CI actually pushed (looked up on Docker Hub by the caller) - it is
    used verbatim as the post-colon tag, so a 3rd-party version bumped THIS
    release (e.g. jenkins 2.541.3→2.555.3) is reflected instead of preserving the
    chart's now-stale prefix. Falls back to prefix-preservation when absent.

    raw_tag:
        "5.199.0", new_tag="master_abc"                  → "master_abc"
        "client-2.29.0", new_tag="master_abc",
            tag_prefix="client-"                          → "client-master_abc"
        "3.18.0", new_tag="master_abc",
            version_txt="3.18.0"                          → "3.18.0-master_abc"

    image_with_tag:
        "acme/vpn:6.1.0",  new_tag="master_abc"  → "acme/vpn:master_abc"
        (tag_prefix and version_txt both prepend to the post-colon tag)

        For image_with_tag we ALSO parse the current value to recover any
        packaged-version prefix that should be preserved verbatim. Service
        repos carry these - they're independent of the release cycle:
          nginx:        1.31.0-{ver}-master_{hash}        → preserve "1.31.0"
          jenkins:      2.541.3-lts-jdk21-{ver}-master_{hash} → preserve "2.541.3-lts-jdk21"
          elasticsearch:8.19.12-{ver}-master_{hash}       → preserve "8.19.12"
          app-server:  7.4-{ver}-master_{hash}           → preserve "7.4"
          backup:       {ver}-master_{hash}               → no prefix
        Devs bump those underlying versions independently (jenkins LTS upgrade,
        nginx point release, etc.) - we just keep whatever's there now.

        A config-supplied tag_prefix wins over the parsed one when set; this
        lets us hard-code a prefix when an image_with_tag chart_location needs
        one we can't infer.

    Composition order: prefix first, then `{version_txt}-`, then new_tag.
    """
    if format_type == "image_with_tag":
        namespace = ""
        current_tag = ""
        if isinstance(current_value, str) and ":" in current_value:
            namespace, current_tag = current_value.split(":", 1)
        elif isinstance(current_value, str):
            current_tag = current_value

        # Authoritative path: use the tag CI actually published (recovers a
        # 3rd-party version bumped this release). Namespace preserved from current.
        if published_tag:
            return f"{namespace}:{published_tag}" if namespace else published_tag

        effective_prefix = tag_prefix or parse_image_tag_prefix(current_value)
        pieces = [effective_prefix]
        if version_txt:
            pieces.append(f"{version_txt}-")
        pieces.append(new_tag)
        final_tag = "".join(pieces)
        return f"{namespace}:{final_tag}" if namespace else final_tag

    # raw_tag: tag_prefix from config + optional version_txt + new_tag
    pieces = [tag_prefix]
    if version_txt:
        pieces.append(f"{version_txt}-")
    pieces.append(new_tag)
    return "".join(pieces)
