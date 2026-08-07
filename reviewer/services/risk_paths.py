from dataclasses import dataclass
from typing import Literal, Sequence

from reviewer.vcs.base import ChangedFile

RiskReason = Literal[
    "credential_like", "migration", "ci_deploy_infra", "dependency"
]
RISK_PATH_LIMIT = 10

_REASON_ORDER: tuple[RiskReason, ...] = (
    "credential_like",
    "migration",
    "ci_deploy_infra",
    "dependency",
)
_DEPENDENCY_NAMES = {
    "uv.lock",
    "poetry.lock",
    "pipfile",
    "pipfile.lock",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "composer.json",
    "composer.lock",
    "gemfile",
    "gemfile.lock",
}
_CI_FILENAMES = {".gitlab-ci.yml", ".gitlab-ci.yaml", "jenkinsfile"}
_INFRA_SEGMENTS = {
    "deploy",
    "deployment",
    "helm",
    "charts",
    "k8s",
    "kubernetes",
    "terraform",
    "infra",
}


@dataclass(frozen=True)
class RiskPath:
    path: str
    status: str
    reasons: tuple[RiskReason, ...]


def _contains_pair(segments: tuple[str, ...], first: str, second: str) -> bool:
    return any(
        left == first and right == second
        for left, right in zip(segments, segments[1:])
    )


def _is_compose_file(basename: str) -> bool:
    return basename in {
        "compose.yml",
        "compose.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
    }


def classify_risk_path(path: str) -> tuple[RiskReason, ...]:
    normalized = path.replace("\\", "/").strip("/")
    lowered = normalized.lower()
    basename = lowered.rsplit("/", 1)[-1]
    segments = tuple(part for part in lowered.split("/") if part)
    if basename.endswith(".py"):
        return ()

    stem = basename.rsplit(".", 1)[0]
    reasons: set[RiskReason] = set()
    if (
        basename.startswith(".env")
        or basename in {".npmrc", ".pypirc", ".netrc"}
        or stem in {"secret", "secrets", "credential", "credentials"}
        or {"secret", "secrets", "credential", "credentials"} & set(segments)
    ):
        reasons.add("credential_like")
    if (
        basename.endswith(".sql")
        or "migrations" in segments
        or _contains_pair(segments, "alembic", "versions")
        or _contains_pair(segments, "db", "migrate")
    ):
        reasons.add("migration")
    if (
        basename in _CI_FILENAMES
        or basename.startswith("dockerfile")
        or _is_compose_file(basename)
        or _contains_pair(segments, ".github", "workflows")
        or bool(_INFRA_SEGMENTS & set(segments))
        or basename.endswith((".tf", ".tfvars"))
    ):
        reasons.add("ci_deploy_infra")
    if (
        basename in _DEPENDENCY_NAMES
        or (basename.startswith("requirements") and basename.endswith(".txt"))
    ):
        reasons.add("dependency")
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)


def _rank(candidate: tuple[ChangedFile, RiskPath]) -> tuple:
    file, signal = candidate
    patch = file.patch or ""
    reason_priority = min(_REASON_ORDER.index(reason) for reason in signal.reasons)
    return (
        reason_priority,
        -sum(line.startswith("@@") for line in patch.splitlines()),
        -len(patch),
        signal.path.replace("\\", "/").lower(),
        signal.path,
        signal.status,
    )


def select_risk_paths(
    files: Sequence[ChangedFile],
    limit: int = RISK_PATH_LIMIT,
) -> tuple[list[RiskPath], list[str]]:
    ranked = sorted(
        (
            (file, RiskPath(file.path, file.status, reasons))
            for file in files
            if (reasons := classify_risk_path(file.path))
        ),
        key=_rank,
    )
    unique: dict[str, RiskPath] = {}
    for _, signal in ranked:
        unique.setdefault(signal.path.replace("\\", "/").lower(), signal)
    signals = list(unique.values())
    selected = signals[: max(limit, 0)]
    skipped = [signal.path for signal in signals[max(limit, 0) :]]
    return selected, skipped
