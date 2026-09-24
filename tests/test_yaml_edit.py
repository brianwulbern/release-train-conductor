"""Tests for the surgical YAML editor - the riskiest pure logic in the tool,
since a wrong edit lands in the production charts repo."""
from conductor import yaml_edit


def test_find_value_line_nested():
    text = "global:\n  image:\n    tag: 5.199.0\n"
    assert yaml_edit.find_value_line(text, "global.image.tag") == 3


def test_replace_raw_tag_preserves_indent_and_key():
    text = "global:\n  image:\n    tag: 5.199.0\n"
    new, line, err = yaml_edit.edit_value_at_key(text, "global.image.tag", "5.200.0")
    assert err is None and line == 3
    assert new == "global:\n  image:\n    tag: 5.200.0\n"


def test_replace_preserves_inline_comment():
    text = "client:\n  image:\n    tag: client-old  # pinned\n"
    new, _line, err = yaml_edit.edit_value_at_key(text, "client.image.tag", "client-new")
    assert err is None
    assert new == "client:\n  image:\n    tag: client-new  # pinned\n"


def test_replace_preserves_quote_style():
    text = 'a:\n  b: "1.0.0"\n'
    new, _line, err = yaml_edit.edit_value_at_key(text, "a.b", "2.0.0")
    assert err is None
    assert new == 'a:\n  b: "2.0.0"\n'


def test_image_with_tag_value_replaced_wholesale_by_caller_pattern():
    # yaml_edit only swaps the value; composing namespace/repo:tag is the
    # caller's job. Here we confirm a full image string round-trips.
    text = "configs:\n  envs:\n    APP_DOCKER_VPN: acme/vpn:master_aaa\n"
    new, _line, err = yaml_edit.edit_value_at_key(
        text, "configs.envs.APP_DOCKER_VPN", "acme/vpn:master_bbb"
    )
    assert err is None
    assert "acme/vpn:master_bbb" in new
    assert "master_aaa" not in new


def test_only_target_line_changes():
    text = "a:\n  b: 1\n  c: 2\nd: 3\n"
    new, _line, err = yaml_edit.edit_value_at_key(text, "a.c", "99")
    assert err is None
    # every other line identical
    assert new.splitlines()[0] == "a:"
    assert new.splitlines()[1] == "  b: 1"
    assert new.splitlines()[2] == "  c: 99"
    assert new.splitlines()[3] == "d: 3"


def test_missing_key_returns_error_not_crash():
    text = "a:\n  b: 1\n"
    new, line, err = yaml_edit.edit_value_at_key(text, "a.nope", "x")
    assert new is None and line is None
    assert "not found" in err
