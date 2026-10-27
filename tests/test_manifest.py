"""Tests for manifest derived methods, schema round-trip, reconcile helpers,
and the active-pointer / latest-selection logic."""
import datetime as dt

import pytest

from conductor import manifest as M
from conductor import config


def _repo(name, classification, **kw):
    return M.RepoEntry(repo=name, classification=classification, **kw)


def _manifest(repos, label="2026-06-09", actions=None):
    return M.Manifest(
        release_version_id="1",
        release_label=label,
        generated_at=M.now_iso(),
        repos=repos,
        actions=actions or [],
    )


# ─── derived helpers ─────────────────────────────────────────────────────────

def test_classify_test_path_platform():
    mf = _manifest([_repo("api", "platform"), _repo("service-nginx", "service")])
    assert mf.classify_test_path() == "platform"


def test_classify_test_path_services_only():
    mf = _manifest([_repo("service-nginx", "service")])
    assert mf.classify_test_path() == "services"


def test_classify_test_path_none():
    mf = _manifest([_repo("charts", "meta")])
    assert mf.classify_test_path() == "none"


def test_services_jenkins_params_composes_version_prefix():
    mf = _manifest([
        _repo("service-jenkins", "service", target_tag="master_abc", version_txt="6.5.0"),
        _repo("service-nginx", "service", target_tag="master_def"),
    ])
    params = mf.services_jenkins_params()
    assert params["CI_IMAGE"] == "6.5.0-master_abc"     # version-prefixed
    assert params["WEBSERVER_IMAGE"] == "master_def"     # no version_txt


def test_services_jenkins_params_prefers_published_image_tag():
    # The real Docker Hub tag carries a base-image prefix (nginx 1.31.2-…) that
    # {version_txt}-{target_tag} can't reconstruct - the registry is the source
    # of truth, so published_image_tag wins over the composed value.
    mf = _manifest([
        _repo("service-nginx", "service", target_tag="master_e43186",
              version_txt="6.10.0",
              published_image_tag="1.31.2-6.10.0-master_e43186"),
    ])
    params = mf.services_jenkins_params()
    assert params["WEBSERVER_IMAGE"] == "1.31.2-6.10.0-master_e43186"


def test_services_jenkins_params_falls_back_without_published_tag():
    # No published_image_tag (e.g. built without Docker creds) → reconstruct.
    mf = _manifest([
        _repo("service-nginx", "service", target_tag="master_e43186",
              version_txt="6.10.0"),
    ])
    params = mf.services_jenkins_params()
    assert params["WEBSERVER_IMAGE"] == "6.10.0-master_e43186"


def test_argocd_sync_order_prefix_first():
    mf = _manifest([_repo("service-nginx", "service"), _repo("api", "platform"),
                    _repo("workloads-controller", "platform")])
    order = mf.argocd_sync_order()
    assert order[:2] == ["workloads-controller", "api"]


def test_prebump_done_when_tagged():
    r = _repo("service-jenkins", "service", release_tag="6.7.0", version_txt="6.7.0",
              latest_release="6.7.0")
    assert r.prebump_done() is True


def test_prebump_done_when_version_ahead_of_last_release():
    # Manually bumped VERSION.txt, not tagged yet → ahead of last release ⇒ done.
    r = _repo("service-jenkins", "service", release_tag=None, version_txt="6.7.0",
              latest_release="6.6.0")
    assert r.prebump_done() is True


def test_prebump_not_done_when_version_equals_last_release():
    r = _repo("service-jenkins", "service", release_tag=None, version_txt="6.6.0",
              latest_release="6.6.0")
    assert r.prebump_done() is False


def test_prebump_not_done_with_no_version_info():
    r = _repo("service-jenkins", "service", release_tag=None)
    assert r.prebump_done() is False


def test_actions_for_filters_by_stage():
    mf = _manifest([], actions=[
        {"stage": "release_tag", "summary": "a"},
        {"stage": "promote", "summary": "b"},
        {"stage": "release_tag", "summary": "c"},
    ])
    assert [a["summary"] for a in mf.actions_for("release_tag")] == ["a", "c"]


# ─── schema round-trip + reconcile ───────────────────────────────────────────

def test_roundtrip_preserves_new_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    mf = _manifest([
        _repo("service-jenkins", "service", release_tag="6.5.0",
              tag_source="override", pinned=True),
    ], actions=[{"at": "x", "stage": "release_tag", "summary": "cut", "url": None, "repo": "x"}])
    path = M.write_manifest(mf, None)
    back = M.read_manifest(path)
    r = back.repos[0]
    assert r.pinned is True and r.tag_source == "override" and r.release_tag == "6.5.0"
    assert back.actions[0]["stage"] == "release_tag"


def test_pinned_overrides_extracts_only_pinned():
    mf = _manifest([
        _repo("service-jenkins", "service", release_tag="6.5.0", pinned=True),
        _repo("api", "platform", release_tag="5.208.0", pinned=False),  # auto, not pinned
        _repo("cli", "other", release_tag=None, pinned=True),            # pinned but no tag
    ])
    assert M.pinned_overrides(mf) == {"service-jenkins": "6.5.0"}


def test_record_action_appends_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    path = M.write_manifest(_manifest([]), None)
    M.record_action(path, "release_tag", "cut api 5.208.0", url="http://x", repo="api")
    back = M.read_manifest(path)
    assert len(back.actions) == 1
    assert back.actions[0]["stage"] == "release_tag"
    assert back.actions[0]["repo"] == "api"


# ─── selection / active pointer ──────────────────────────────────────────────

def test_label_date_parsing():
    assert M._label_date("2026-06-09") == dt.date(2026, 6, 9)
    assert M._label_date("not-a-date") is None
    assert M._label_date("2026-13-99") is None  # invalid month/day


def test_latest_and_active(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    M.write_manifest(_manifest([], label="2026-06-09"), None)
    M.write_manifest(_manifest([], label="2026-06-11"), None)
    latest = M.find_latest_manifest()
    assert latest.name == "release-2026-06-11.json"      # newest by date
    # default == latest when no pin
    assert M.default_manifest_path().name == "release-2026-06-11.json"
    # pin the older one → default follows the pin
    M.set_active(tmp_path / "release-2026-06-09.json")
    assert M.default_manifest_path().name == "release-2026-06-09.json"
    assert M.get_active_path().name == "release-2026-06-09.json"
    # overlap detection sees both
    assert len(M.recent_manifests()) == 2
    M.clear_active()
    assert M.get_active_path() is None
    assert M.default_manifest_path().name == "release-2026-06-11.json"


def test_manifest_writes_its_format_and_reads_old_files(tmp_path):
    import json as _json
    from conductor import manifest as _M
    mf = _M.Manifest(release_version_id="1", release_label="2026-10-20", generated_at="x")
    p = _M.write_manifest(mf, tmp_path / "m.json")
    data = _json.loads(p.read_text())
    assert data["format"] == _M.FORMAT
    data.pop("format")
    p.write_text(_json.dumps(data))
    assert _M.read_manifest(p).release_label == "2026-10-20"




def test_shipped_by_names_the_earlier_train_that_carried_the_tag():
    from conductor import manifest as _M
    def mf(label, tag):
        return _M.Manifest(release_version_id="1", release_label=label, generated_at="x",
                           repos=[_M.RepoEntry(repo="service-nginx", classification="service",
                                               release_tag=tag)])
    _M.write_manifest(mf("2026-10-15", "6.12.0"))
    _M.write_manifest(mf("2026-10-20", "6.12.0"))
    assert _M.shipped_by("service-nginx", "6.12.0", "2026-10-20") == "2026-10-15"
    assert _M.shipped_by("service-nginx", "6.12.0", "2026-10-15") is None
    assert _M.shipped_by("service-nginx", "6.13.0", "2026-10-20") is None
    assert _M.shipped_by("api", "6.12.0", "2026-10-20") is None


def test_sole_ticket_is_the_hotfix_ticket_only_without_a_steps_ticket():
    from conductor import manifest as _M

    def mf(*summaries):
        return _M.Manifest(release_version_id="1", release_label="2026-10-23", generated_at="x",
                           tickets=[_M.TicketEntry(key=f"REL-{i}", issue_id=str(i), summary=s, status="x")
                                    for i, s in enumerate(summaries, 1)])
    assert mf("Upgrade webserver nginx to 1.31.7").sole_ticket() == "REL-1"
    assert mf("a", "b").sole_ticket() is None
    assert mf("[Release] Release Steps for Release Train for 2026-10-27").sole_ticket() is None
    assert mf().sole_ticket() is None
