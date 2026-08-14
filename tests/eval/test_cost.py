"""Unit-тесты взвешенной цены: сумма токенов не пропорциональна стоимости."""
import pytest

from eval.solve_task_metrics import cost

BUCKETS = {
    "fresh_in": 10_000.0,
    "output": 100_000.0,
    "cache_write": 200_000.0,
    "cache_read": 2_000_000.0,
}


def test_weights_are_the_documented_multipliers():
    assert cost.WEIGHTS == {
        "fresh_in": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.1,
    }


def test_raw_is_plain_sum():
    assert cost.raw(BUCKETS) == pytest.approx(2_310_000.0)


def test_weighted_applies_multipliers():
    # 10_000*1 + 100_000*5 + 200_000*1.25 + 2_000_000*0.1 = 960_000
    assert cost.weighted(BUCKETS) == pytest.approx(960_000.0)


def test_weighted_is_lower_than_raw_when_cache_read_dominates():
    assert cost.weighted(BUCKETS) < cost.raw(BUCKETS)


def test_inflation_is_ratio_of_raw_to_weighted():
    assert cost.inflation(cost.raw(BUCKETS), cost.weighted(BUCKETS)) == pytest.approx(
        2.40625
    )


def test_inflation_none_when_weighted_is_zero():
    assert cost.inflation(0.0, 0.0) is None


def test_sum_buckets_merges_several_models():
    merged = cost.sum_buckets([BUCKETS, BUCKETS])

    assert merged["output"] == pytest.approx(200_000.0)
    assert merged["cache_read"] == pytest.approx(4_000_000.0)


def test_sum_buckets_of_nothing_is_zeroed():
    assert cost.sum_buckets([]) == {
        "fresh_in": 0.0,
        "output": 0.0,
        "cache_write": 0.0,
        "cache_read": 0.0,
    }
