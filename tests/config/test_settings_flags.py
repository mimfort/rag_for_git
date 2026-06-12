from reviewer.config.settings import Settings


def test_max_tool_result_chars_default():
    s = Settings(_env_file=None)
    assert s.max_tool_result_chars == 8000
