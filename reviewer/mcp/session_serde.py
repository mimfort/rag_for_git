"""Сериализация PreparedReview в JSON-payload для SessionStore и обратно.

Живой ``vcs`` (VCSProvider с httpx-клиентом) НЕ сериализуется — он восстанавливается
вызывающим (MCPReviewService через _create_vcs_provider) и передаётся в from_payload.
``ctx`` (ToolContext) не сериализуется вовсе — он пересобирается из PreparedReview.
"""
from __future__ import annotations

from dataclasses import asdict

from reviewer.agent.state import ReviewUnit
from reviewer.policy.context_limits import ContextLimits
from reviewer.policy.policy import ReviewPolicy
from reviewer.services.review_service import PreparedReview
from reviewer.vcs.base import PullRequest, VCSProvider


def to_payload(prepared: PreparedReview) -> dict:
    """Собрать JSON-дружелюбный dict из PreparedReview, исключая ``vcs``.

    Не используем ``dataclasses.asdict(prepared)`` целиком — он попытался бы
    глубоко скопировать живой ``vcs``. Сериализуем поля явно.
    """
    return {
        "repo": prepared.repo,
        "branch": prepared.branch,
        "prq": asdict(prepared.prq),
        "units": [asdict(u) for u in prepared.units],
        "policy": asdict(prepared.policy),
        "patches": prepared.patches,
        "sources": prepared.sources,
        "changed_paths": prepared.changed_paths,
        "changed_node_ids": prepared.changed_node_ids,
        "skipped_paths": prepared.skipped_paths,
        "overlay_ref": prepared.overlay_ref,
        "changed_status": prepared.changed_status,
        "task_board": prepared.task_board,
        "task_keys": prepared.task_keys,
    }


def _policy_from_payload(d: dict) -> ReviewPolicy:
    """Восстановить ReviewPolicy из asdict-плоского payload.

    ``asdict(policy)`` рекурсивно разворачивает вложенный ``ContextLimits`` в
    обычные dict'ы — ``ReviewPolicy(**d)`` напрямую оставил бы там dict вместо
    типизированного ``ContextLimits``. Пересобираем его через тот же парсер,
    что и .review.yml.
    """
    fields = dict(d)
    context_limits = fields.pop("context_limits", None)
    policy = ReviewPolicy(**fields)
    if context_limits is not None:
        policy.context_limits = ContextLimits.from_review_yaml(
            {"context_limits": context_limits})
    return policy


def from_payload(d: dict, vcs: VCSProvider) -> PreparedReview:
    """Восстановить PreparedReview из payload; ``vcs`` подставляется отдельно.

    Бросает KeyError/TypeError при несовместимом payload (например, схема
    dataclass изменилась между версиями) — вызывающий ловит и трактует как
    промах регидрации.
    """
    return PreparedReview(
        repo=d["repo"],
        branch=d["branch"],
        prq=PullRequest(**d["prq"]),
        units=[ReviewUnit(**u) for u in d["units"]],
        policy=_policy_from_payload(d["policy"]),
        patches=d["patches"],
        sources=d["sources"],
        changed_paths=d["changed_paths"],
        changed_node_ids=d["changed_node_ids"],
        skipped_paths=d["skipped_paths"],
        overlay_ref=d["overlay_ref"],
        vcs=vcs,
        changed_status=d["changed_status"],
        task_board=d.get("task_board"),
        task_keys=d.get("task_keys"),
    )
