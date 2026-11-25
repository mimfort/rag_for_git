"""Сохранить начало исходного фрагмента при экспериментальной замене окна."""

from reviewer.mcp.brief_evidence import _HEADER, _NUMBERED


def _paths(section: str) -> list[str]:
    paths = []
    for block in section.split("\n\n"):
        header, _, body = block.partition("\n")
        match = _HEADER.fullmatch(header)
        if not match or not body or not all(_NUMBERED.fullmatch(row) for row in body.splitlines()):
            raise ValueError("Ожидаются полные фрагменты с номерами строк")
        paths.append(match["path"])
    if len(set(paths)) != len(paths):
        raise ValueError("Входные пути должны быть уникальными")
    return paths


def _render(header: str, numbered: list[str]) -> str:
    first = _NUMBERED.fullmatch(numbered[0])[1]
    last = _NUMBERED.fullmatch(numbered[-1])[1]
    prefix, _, _ = header.rpartition(":")
    return "\n".join([f"{prefix}:{first}-{last})", *numbered])


def _fits(candidate: str, original: str) -> bool:
    return len(candidate) <= len(original) and len(candidate.encode()) <= len(original.encode())


def _combine(original: str, selected: str) -> str:
    if original == selected:
        return original
    original_header, *original_rows = original.splitlines()
    selected_header, *selected_rows = selected.splitlines()
    anchor = next((row for row in original_rows if _NUMBERED.fullmatch(row)[2].strip()), None)
    if anchor is None:
        return original
    anchor_number, anchor_text = _NUMBERED.fullmatch(anchor).groups()
    for row in selected_rows:
        selected_number, selected_text = _NUMBERED.fullmatch(row).groups()
        if selected_number == anchor_number:
            same_row = selected_text.strip() == anchor_text.strip()
            return selected if same_row and _fits(selected, original) else original
    original_excerpt = _render(original_header, [anchor])
    while selected_rows:
        pair = f"{original_excerpt}\n\n{_render(selected_header, selected_rows)}"
        if _fits(pair, original):
            return pair
        selected_rows.pop()
    return original


def preserve_original_anchor(base: str, selected: str) -> str:
    """В фиксированном бюджете оставить первую непустую строку исходного окна."""
    if _paths(base) != _paths(selected):
        raise ValueError("Пути и их порядок должны совпадать")
    return "\n\n".join(
        _combine(original, candidate)
        for original, candidate in zip(base.split("\n\n"), selected.split("\n\n"), strict=True)
    )
