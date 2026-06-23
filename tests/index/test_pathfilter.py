from reviewer.index.pathfilter import is_ignored


def test_bare_dir_matches_subtree():
    assert is_ignored("vendor/lib/x.py", ["vendor"])
    assert is_ignored("vendor/x.py", ["vendor"])


def test_bare_dir_does_not_match_sibling_prefix():
    assert not is_ignored("vendored/x.py", ["vendor"])


def test_glob_pattern_matched_as_is():
    assert is_ignored("pkg/a.gen.py", ["*.gen.py"])
    assert is_ignored("migrations/0001.py", ["migrations/*"])


def test_no_match_returns_false():
    assert not is_ignored("reviewer/index/store.py", ["vendor", "migrations/*"])


def test_empty_patterns_and_blank():
    assert not is_ignored("a/b.py", [])
    assert not is_ignored("a/b.py", [""])


def test_bare_pattern_matches_self():
    assert is_ignored("vendor", ["vendor"])
    assert is_ignored("a/b", ["a/b"])


def test_bare_dir_matches_deep_subtree():
    assert is_ignored("vendor/a/b/c.py", ["vendor"])
