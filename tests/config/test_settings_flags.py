from reviewer.config.settings import Settings


def test_tier3_flags_have_defaults():
    s = Settings(_env_file=None)
    assert s.review_agentic_verify is True
    assert s.review_synthesis is True
    assert s.review_verify_min_severity == "medium"
    assert s.review_verify_max_iterations == 3
