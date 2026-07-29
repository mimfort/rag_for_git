import pytest

from reviewer.services.risk_paths import RiskPath, select_risk_paths
from reviewer.vcs.base import ChangedFile


def _changed(
    path: str,
    status: str = "modified",
    patch: str = "@@ -1 +1 @@\n x",
) -> ChangedFile:
    return ChangedFile(path=path, status=status, patch=patch)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("db/migrations/001_drop.sql", "migration"),
        (".github/workflows/release.yml", "ci_deploy_infra"),
        ("infra/main.tf", "ci_deploy_infra"),
        ("uv.lock", "dependency"),
        ("pyproject.toml", "dependency"),
        (".env.production", "credential_like"),
        ("deploy/secrets.yaml", "credential_like"),
    ],
)
def test_classifies_supported_risk_families(path: str, reason: str) -> None:
    selected, skipped = select_risk_paths([_changed(path)])

    assert skipped == []
    assert len(selected) == 1
    assert reason in selected[0].reasons


@pytest.mark.parametrize(
    "path",
    ["config/app.yaml", "fixtures/data.json", "ruff.toml", "reviewer/app.py"],
)
def test_ordinary_config_and_python_are_quiet(path: str) -> None:
    assert select_risk_paths([_changed(path)]) == ([], [])


def test_deduplicates_normalized_paths_with_deterministic_retention() -> None:
    upper = _changed("INFRA\\SECRETS.ENV")
    lower = _changed("infra/secrets.env")

    expected = [
        RiskPath(
            "INFRA\\SECRETS.ENV",
            "modified",
            ("credential_like", "ci_deploy_infra"),
        )
    ]
    assert select_risk_paths([upper, lower])[0] == expected
    assert select_risk_paths([lower, upper])[0] == expected


def test_orders_by_reason_then_diff_importance_then_path_and_caps() -> None:
    files = [_changed(f"migrations/{name}.sql") for name in ("z", "a", "m")]

    selected, skipped = select_risk_paths(files, limit=2)

    assert [item.path for item in selected] == [
        "migrations/a.sql",
        "migrations/m.sql",
    ]
    assert skipped == ["migrations/z.sql"]


def test_removed_risk_file_is_preserved() -> None:
    selected, _ = select_risk_paths([_changed("Dockerfile", status="removed")])

    assert selected[0].status == "removed"
