"""Повторить проверенный эксперимент с буквальными сигналами запроса."""

from eval.pri324.selector import _IDENTIFIER, _STOP_WORDS, _terms


def _whole_terms(text: str) -> set[str]:
    return {
        term for token in _IDENTIFIER.findall(text)
        if len(term := token.casefold()) >= 3 and term not in _STOP_WORDS
    }


def _symbol_terms(symbol: str) -> set[str]:
    terms = set()
    for name in symbol.split("."):
        if name.startswith("__") and name.endswith("__"):
            continue
        terms.update(_whole_terms(name))
        if "_" in name:
            terms.update(_whole_terms(name.replace("_", " ")))
    return terms


def _evidence(block: str, query_terms: set[str], *, baseline: bool) -> tuple[set[str], set[str]]:
    header, _, numbered = block.partition("\n")
    symbol = header.split(" (", 1)[0].split("#", 1)[-1]
    text = "\n".join(row.partition(" | ")[2] for row in numbered.splitlines())
    names = (_terms if baseline else _symbol_terms)(symbol)
    body = (_terms if baseline else _whole_terms)(text)
    return names & query_terms, body & query_terms


def require_literal_signal(base: str, selected: str, query: str) -> str:
    """Отбрасывать подстановку, если она не даёт более сильного буквального совпадения."""
    terms = _whole_terms(query)
    blocks = []
    for original, candidate in zip(base.split("\n\n"), selected.split("\n\n"), strict=True):
        original_names, original_body = _evidence(original, terms, baseline=True)
        candidate_names, candidate_body = _evidence(candidate, terms, baseline=False)
        promoted = candidate_names - original_names
        discovered = candidate_body - original_names - original_body
        blocks.append(candidate if promoted or discovered else original)
    return "\n\n".join(blocks)
