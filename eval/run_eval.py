"""Прогон набора PR-кейсов: для каждого кейса известны ожидаемые проблемы (файл+строка+категория).
Считает precision/recall сматченных findings. Кейсы кладутся в eval/cases/<name>/ с:
  - before/ и after/ (снапшоты кода),
  - patch.diff,
  - expected.json [{file,line,category}].
Запуск: python eval/run_eval.py
"""
import pathlib


def score(expected: list[dict], produced: list[dict]) -> dict:
    exp = {(e["file"], e["line"], e["category"]) for e in expected}
    prod = {(p["file"], p["line"], p["category"]) for p in produced}
    tp = len(exp & prod)
    precision = tp / len(prod) if prod else 0.0
    recall = tp / len(exp) if exp else 0.0
    return {"precision": precision, "recall": recall, "tp": tp}


def main() -> None:
    cases = sorted(pathlib.Path("eval/cases").glob("*"))
    print(f"Кейсов: {len(cases)} (TODO: подключить реальный прогон агента на каждом)")


if __name__ == "__main__":
    main()
