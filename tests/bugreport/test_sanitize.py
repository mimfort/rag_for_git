"""Анонимизация репорта: по классу чувствительных данных, с negative-ассертами (PRI-239).

Каждый тест обязан утверждать ОТСУТСТВИЕ подстроки, а не только присутствие плейсхолдера:
замена, оставившая хвост оригинала, прошла бы позитивный ассерт и утекла бы в публичный issue.
"""
from reviewer.bugreport.sanitize import PLACEHOLDER, Sanitizer, sanitize_text


def test_absolute_paths_are_removed():
    text = "не открылся /Users/kate/work/acme-billing/src/app/models.py"
    out = sanitize_text(text)
    assert "kate" not in out
    assert "acme-billing" not in out
    assert "models.py" not in out
    assert PLACEHOLDER["path"] in out


def test_windows_path_is_removed():
    out = sanitize_text(r"файл C:\Users\Kate\acme\billing.py не найден")
    assert "Kate" not in out and "acme" not in out and "billing.py" not in out


def test_tool_paths_survive_because_the_bug_is_unreproducible_without_them():
    out = sanitize_text("исключение в reviewer/mcp/service.py:1157")
    assert "reviewer/mcp/service.py" in out


def test_absolute_path_keeps_only_the_tool_tail():
    out = sanitize_text("/Users/kate/work/acme/reviewer/mcp/service.py")
    assert out.strip() == "reviewer/mcp/service.py"
    assert "kate" not in out and "acme" not in out


def test_repo_owner_and_name_are_removed_even_without_a_slash():
    clean = Sanitizer.build(repos=["acmecorp/billing-api"])
    out = clean.text("репозиторий acmecorp/billing-api, а также просто billing-api и acmecorp")
    assert "acmecorp" not in out
    assert "billing-api" not in out


def test_branch_names_are_removed():
    out = Sanitizer.build(branches=["feature/acme-secret-pricing"]).text(
        "ветка feature/acme-secret-pricing отстала")
    assert "acme-secret-pricing" not in out


def test_task_keys_and_task_urls_are_removed():
    out = sanitize_text("задача BILL-4412 на https://acme.yougile.com/team/abc#BILL-4412")
    assert "BILL-4412" not in out
    assert "acme.yougile.com" not in out
    assert PLACEHOLDER["task"] in out


def test_self_hosted_hosts_are_removed():
    clean = Sanitizer.build(hosts=["gitlab.acme.internal"])
    out = clean.text("self-hosted gitlab.acme.internal:8443 недоступен")
    assert "acme.internal" not in out


def test_emails_and_names_in_them_are_removed():
    out = sanitize_text("автор kate.ivanova@acme-corp.com не подтвердил")
    assert "kate.ivanova" not in out
    assert "acme-corp.com" not in out
    assert PLACEHOLDER["email"] in out


def test_tokens_of_every_known_shape_are_removed():
    samples = [
        "ghp_" + "a" * 36,
        "github_pat_" + "b" * 40,
        "glpat-" + "c" * 20,
        "pa-" + "d" * 30,
        "sk-" + "e" * 32,
        "xoxb-" + "1" * 24,
        "Bearer abcdefghijklmnopqrstuvwx",
    ]
    for sample in samples:
        out = sanitize_text(f"заголовок содержал {sample} и упал")
        assert sample.split("-")[-1] not in out or PLACEHOLDER["token"] in out
        assert sample not in out, sample


def test_literal_secrets_are_redacted_longest_first():
    clean = Sanitizer.build(secrets=["abc", "abcdef123456"])
    out = clean.text("ключ abcdef123456 в запросе")
    assert "abcdef123456" not in out
    assert "def123456" not in out       # короткий префикс не оставил хвост длинного


def test_source_code_lines_are_removed_wholesale():
    text = "\n".join([
        "фрагмент:",
        "def calculate_customer_discount(order, tier):",
        "    return order.total * TIER_RATES[tier]",
    ])
    out = sanitize_text(text)
    assert "calculate_customer_discount" not in out
    assert "TIER_RATES" not in out
    assert out.count(PLACEHOLDER["code"]) == 2


def test_prose_starting_with_a_keyword_is_not_mistaken_for_code():
    out = sanitize_text("If the tool returns nothing the skill stops")
    assert PLACEHOLDER["code"] not in out
    assert "the skill stops" in out


def test_traceback_header_lines_survive_as_structure():
    out = sanitize_text('Traceback (most recent call last):')
    assert "Traceback" in out


def test_tool_config_file_names_survive():
    out = sanitize_text("значение из .review.yml не применилось")
    assert ".review.yml" in out


def test_user_file_names_do_not_survive():
    out = sanitize_text("сломался billing_rules.py")
    assert "billing_rules" not in out
    assert PLACEHOLDER["file"] in out


def test_sanitizer_is_deterministic():
    text = "/srv/acme/app/models.py упал у kate@acme.com в ветке release/acme"
    clean = Sanitizer.build(branches=["release/acme"])
    assert clean.text(text) == clean.text(text)
