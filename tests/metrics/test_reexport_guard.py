"""Guard: расчётное ядро метрики живёт в одной копии (PRI-249).

Офлайн-харнесс eval/solve_task_metrics обязан РЕЭКСПОРТИРОВАТЬ продакшн-модуль,
а не держать вторую реализацию формул: иначе онлайн- и офлайн-числа разъедутся
незаметно, и сравнение «до/после» (критерий 4 PRI-251) перестанет быть валидным.
"""
from __future__ import annotations

from eval.solve_task_metrics import briefs as eval_briefs
from eval.solve_task_metrics import classify as eval_classify
from eval.solve_task_metrics import config as eval_config
from eval.solve_task_metrics import context_core as eval_context_core
from eval.solve_task_metrics import ground_truth as eval_ground_truth
from eval.solve_task_metrics import recall as eval_recall
from reviewer.metrics.brief_quality import briefs as prod_briefs
from reviewer.metrics.brief_quality import classify as prod_classify
from reviewer.metrics.brief_quality import config as prod_config
from reviewer.metrics.brief_quality import context_core as prod_context_core
from reviewer.metrics.brief_quality import ground_truth as prod_ground_truth
from reviewer.metrics.brief_quality import recall as prod_recall


def test_eval_reexports_production_objects():
    """Объекты офлайн-харнесса — те же самые объекты, что в reviewer/."""
    assert eval_classify.is_core_production_path is prod_classify.is_core_production_path
    assert eval_classify.categorize_miss is prod_classify.categorize_miss
    assert eval_recall.evaluate_task is prod_recall.evaluate_task
    assert eval_recall.aggregate is prod_recall.aggregate
    assert eval_recall.TaskQuality is prod_recall.TaskQuality
    assert eval_recall.BULK_CORE_THRESHOLD == prod_recall.BULK_CORE_THRESHOLD
    assert eval_briefs.extract_section_paths is prod_briefs.extract_section_paths
    assert eval_briefs.extract_task_key is prod_briefs.extract_task_key
    assert eval_context_core.derive_context_core is prod_context_core.derive_context_core
    assert eval_context_core.node_paths is prod_context_core.node_paths
    assert eval_config.BriefQualityConfig is prod_config.BriefQualityConfig
    assert eval_config.DEFAULT is prod_config.DEFAULT
    assert eval_ground_truth.collect is prod_ground_truth.collect
    assert eval_ground_truth.filter_pr_merges is prod_ground_truth.filter_pr_merges


def test_production_core_does_not_import_eval():
    """reviewer/** не тянет eval/** — инвариант направления зависимости.

    Проверяется по форме оператора импорта, а не по подстроке: 'eval' встречается
    в прозе докстрингов и в literal_eval, и подстрочная проверка красила бы тест
    на ровном месте.
    """
    import pathlib
    import re

    import_re = re.compile(r"^\s*(?:from\s+eval[\s.]|import\s+eval[\s.,]|import\s+eval$)", re.M)
    root = pathlib.Path(prod_classify.__file__).resolve().parents[3]
    offenders = [
        str(path.relative_to(root))
        for path in (root / "reviewer").rglob("*.py")
        if import_re.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
