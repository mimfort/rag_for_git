def test_generation_is_v2_after_read_mode_change():
    """PRI-245: смена способа чтения обязана инвалидировать старые фрагменты."""
    from reviewer.services.summary_fragments import _GENERATION

    assert _GENERATION == "summary-fragment-v2"
