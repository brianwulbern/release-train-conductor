"""Loader for bundled repos.yaml configuration.

repos.yaml is the single source of truth for per-repo facts: classification,
docker image name, Jenkins parameter, pre-bump files, and chart locations. The
derived accessors here (platform_repos, service_repos, jenkins_param_map) replace
what used to be duplicated frozensets in config.py - edit repos.yaml only.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any, Dict

import yaml


@lru_cache(maxsize=1)
def _load_full() -> Dict[str, Any]:
    """Parse the bundled repos.yaml once (cached). Treat as read-only."""
    text = resources.files("conductor").joinpath("repos.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


@lru_cache(maxsize=1)
def load_repos_config() -> Dict[str, Any]:
    """The `repos:` section of repos.yaml (cached - read once).

    Treat the result as read-only; it is shared across all callers.
    """
    return _load_full().get("repos", {})


def docker_visibility(image_name: str) -> str:
    """'public' | 'private' | 'unknown' for a Docker Hub image.

    Keyed by the DOCKER IMAGE name (post-override, e.g. 'admin', 'jenkins'), not
    the github repo. 'unknown' for images not listed in repos.yaml. Used to give
    precise guidance in the `bump prd` image check (private → needs creds;
    public-but-unreadable → network/transient).
    """
    vis = _load_full().get("docker_visibility", {}) or {}
    return vis.get(image_name, "unknown")


def get_repo_config(repo_name: str) -> Dict[str, Any]:
    """Return per-repo config dict, or empty dict if unknown."""
    return load_repos_config().get(repo_name, {})


def known_repos() -> frozenset:
    """Every repo name declared in repos.yaml (the `repos:` keys). Used to filter
    loosely-sourced signals (e.g. PR URLs harvested from a ticket description) down
    to repos conductor actually tracks, so a stray link can't inject an unknown repo."""
    return frozenset(load_repos_config().keys())


def repos_by_classification(classification: str) -> frozenset:
    """All repo names with the given classification (platform|service|meta|other)."""
    cfg = load_repos_config()
    return frozenset(
        name for name, c in cfg.items()
        if (c or {}).get("classification") == classification
    )


def platform_repos() -> frozenset:
    return repos_by_classification("platform")


def service_repos() -> frozenset:
    return repos_by_classification("service")


def jenkins_param_map() -> Dict[str, str]:
    """{repo: jenkins_param} for every repo that declares one (services)."""
    cfg = load_repos_config()
    return {
        name: (c or {}).get("jenkins_param")
        for name, c in cfg.items()
        if (c or {}).get("jenkins_param")
    }


def repo_classification(repo_name: str) -> str:
    """platform | service | other"""
    return get_repo_config(repo_name).get("classification", "other")


def repo_needs_pre_bump(repo_name: str) -> bool:
    return "pre_bump" in get_repo_config(repo_name)


def repo_pre_bump_files(repo_name: str) -> list[str]:
    return get_repo_config(repo_name).get("pre_bump", {}).get("files", [])


def repo_jenkins_param(repo_name: str) -> str | None:
    return get_repo_config(repo_name).get("jenkins_param")


def docker_image_name(repo_name: str) -> str:
    """Docker Hub image name for a repo (defaults to the repo name itself)."""
    return get_repo_config(repo_name).get("docker_image", repo_name)


def repo_uses_version_prefix(repo_name: str) -> bool:
    """True if the repo's docker tags embed VERSION.txt as a prefix
    (e.g. service-provisioning's `3.18.0-master_xxxxxx`). Inferred from the
    presence of VERSION.txt in the pre_bump.files list.
    """
    files = (get_repo_config(repo_name).get("pre_bump") or {}).get("files") or []
    return "VERSION.txt" in files
