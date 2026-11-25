from conductor.docker_client import pick_on_track


def test_exact_base_wins():
    assert pick_on_track(["7.3-5.1.0", "7.4-5.1.0"], "7.4-") == "7.4-5.1.0"


def test_a_newer_major_never_replaces_the_chart_track():
    tags = ["9.5.7-5.11.0-master_abc123", "8.19.26-5.11.0-master_abc123"]
    assert pick_on_track(tags, "8.19.23-") == "8.19.26-5.11.0-master_abc123"


def test_a_base_that_moved_takes_the_newest():
    assert pick_on_track(["2.555.3-lts-jdk21-6.7.0"], "2.541.3-lts-jdk21-") == "2.555.3-lts-jdk21-6.7.0"
    assert pick_on_track(["9.5.7-5.11.0"], "8.19.23-") == "9.5.7-5.11.0"


def test_no_base_or_no_tags():
    assert pick_on_track(["9.5.7-5.11.0", "8.19.26-5.11.0"]) == "9.5.7-5.11.0"
    assert pick_on_track([], "8.") is None
