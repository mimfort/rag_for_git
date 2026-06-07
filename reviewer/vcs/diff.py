import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

def commentable_lines(patch: str | None) -> dict[str, set[int]]:
    """Возвращает {'RIGHT': {new_lines}, 'LEFT': {old_lines}} для строк внутри хунков."""
    right: set[int] = set()
    left: set[int] = set()
    if not patch:
        return {"RIGHT": right, "LEFT": left}
    old_ln = new_ln = 0
    for line in patch.splitlines():
        m = _HUNK.match(line)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            continue
        if not line:
            continue
        tag = line[0]
        if tag == "+":
            right.add(new_ln); new_ln += 1
        elif tag == "-":
            left.add(old_ln); old_ln += 1
        elif tag == " ":
            right.add(new_ln); old_ln += 1; new_ln += 1
    return {"RIGHT": right, "LEFT": left}
