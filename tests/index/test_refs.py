from reviewer.index.refs import base_ref


def test_base_ref_empty_is_legacy_base():
    assert base_ref("") == "base"


def test_base_ref_branch_is_namespaced():
    assert base_ref("main") == "base:main"
    assert base_ref("release/v1") == "base:release/v1"
