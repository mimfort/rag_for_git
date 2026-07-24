"""Чистая конвертация узкого подмножества Jira ADF и канонического markdown.

Модуль намеренно ничего не знает о Jira REST: он получает и возвращает обычные
словарь/строку. Неподдержанные ADF-узлы прозрачны для текста, но оставляют
детерминированное предупреждение, чтобы вызывающий код мог его залогировать.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AdfConversion:
    """Результат конвертации и предупреждения о неизвестных ADF-узлах."""

    value: str | dict
    warnings: tuple[str, ...] = ()


_HEADING_RE = re.compile(r"^(#{1,6}) (.*)$")
_BULLET_RE = re.compile(r"^- (.*)$")
_ORDERED_RE = re.compile(r"^(\d+)\. (.*)$")
_MARK_ORDER = {"strong": 0, "em": 1, "code": 2, "link": 3}
_MARKDOWN_ESCAPES = "\\*" + chr(96) + "[]()"


def _escape_markdown_text(text: str) -> str:
    return "".join(f"\\{char}" if char in _MARKDOWN_ESCAPES else char for char in text)


def _escape_block_starts(text: str) -> str:
    """Не дать обычному тексту стать блоком при обратном разборе."""
    return re.sub(r"(?m)^(?=(?:#{1,6} |- |>|\d+\. ))", r"\\", text)


def _escape_link_target(href: str) -> str:
    return _escape_markdown_text(href)


class _AdfReader:
    """ADF-walker с накоплением уникальных предупреждений в порядке обхода."""

    def __init__(self) -> None:
        self._warnings: list[str] = []
        self._warning_types: set[str] = set()

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    def _warn_unknown(self, node_type: object) -> None:
        name = str(node_type or "<без type>")
        if name not in self._warning_types:
            self._warning_types.add(name)
            self._warnings.append(f"Неизвестный ADF-узел: {name}")

    @staticmethod
    def _content(node: Mapping[str, object]) -> list[Mapping[str, object]]:
        content = node.get("content")
        if not isinstance(content, list):
            return []
        return [child for child in content if isinstance(child, Mapping)]

    def document(self, document: Mapping[str, object]) -> str:
        return self.block(document)

    def block(self, node: Mapping[str, object]) -> str:
        node_type = node.get("type")
        content = self._content(node)
        if node_type == "doc":
            return "\n\n".join(part for part in (self.block(child) for child in content) if part)
        if node_type == "paragraph":
            return self.inline_children(content)
        if node_type == "heading":
            attrs = node.get("attrs")
            level = attrs.get("level") if isinstance(attrs, Mapping) else 1
            level = level if isinstance(level, int) and 1 <= level <= 6 else 1
            return "#" * level + " " + self.inline_children(content)
        if node_type in ("bulletList", "orderedList"):
            return self.list_block(content, ordered=node_type == "orderedList", attrs=node.get("attrs"))
        if node_type == "listItem":
            return "\n\n".join(part for part in (self.block(child) for child in content) if part)
        if node_type == "blockquote":
            text = "\n\n".join(part for part in (self.block(child) for child in content) if part)
            return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
        if node_type == "codeBlock":
            attrs = node.get("attrs")
            language = attrs.get("language") if isinstance(attrs, Mapping) else ""
            suffix = language if isinstance(language, str) else ""
            code = self.text_content(content)
            longest_run = max((len(run) for run in re.findall(f"{chr(96)}+", code)), default=0)
            fence = chr(96) * max(3, longest_run + 1)
            return f"{fence}{suffix}\n{code}\n{fence}"
        if node_type in ("text", "hardBreak"):
            return self.inline(node)

        self._warn_unknown(node_type)
        return "\n\n".join(part for part in (self.block(child) for child in content) if part)

    def list_block(
        self,
        items: list[Mapping[str, object]],
        *,
        ordered: bool,
        attrs: object,
    ) -> str:
        start = 1
        if ordered and isinstance(attrs, Mapping):
            order = attrs.get("order")
            if isinstance(order, int) and order > 0:
                start = order
        lines: list[str] = []
        for index, item in enumerate(items):
            if item.get("type") != "listItem":
                self._warn_unknown(item.get("type"))
            item_lines = self.list_item_lines(item)
            if not item_lines:
                continue
            prefix = f"{start + index}. " if ordered else "- "
            lines.append(prefix + item_lines[0])
            lines.extend(f"  {line}" if line else "" for line in item_lines[1:])
        return "\n".join(lines)

    def list_item_lines(self, item: Mapping[str, object]) -> list[str]:
        lines: list[str] = []
        for child in self._content(item):
            text = self.block(child)
            if not text:
                continue
            if lines:
                lines.append("")
            lines.extend(text.splitlines())
        return lines

    def inline_children(self, content: list[Mapping[str, object]]) -> str:
        return _escape_block_starts("".join(self.inline(child) for child in content))

    def inline(self, node: Mapping[str, object]) -> str:
        node_type = node.get("type")
        if node_type == "hardBreak":
            return "\n"
        if node_type != "text":
            self._warn_unknown(node_type)
            return self.inline_children(self._content(node))

        text = node.get("text")
        result = _escape_markdown_text(text) if isinstance(text, str) else ""
        marks = node.get("marks")
        if not isinstance(marks, list):
            return result
        known: dict[str, Mapping[str, object]] = {}
        for mark in marks:
            if not isinstance(mark, Mapping):
                continue
            mark_type = mark.get("type")
            if mark_type in ("strong", "em", "code", "link"):
                known[str(mark_type)] = mark
        if "code" in known:
            result = f"`{result}`"
        if "strong" in known:
            result = f"**{result}**"
        if "em" in known:
            result = f"*{result}*"
        link = known.get("link")
        attrs = link.get("attrs") if link else None
        href = attrs.get("href") if isinstance(attrs, Mapping) else None
        if isinstance(href, str) and href:
            result = f"[{result}]({_escape_link_target(href)})"
        return result

    def text_content(self, content: list[Mapping[str, object]]) -> str:
        parts: list[str] = []
        for node in content:
            if node.get("type") == "text":
                value = node.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif node.get("type") == "hardBreak":
                parts.append("\n")
            else:
                self._warn_unknown(node.get("type"))
                parts.append(self.text_content(self._content(node)))
        return "".join(parts)


def adf_to_markdown(document: Mapping[str, object] | None) -> AdfConversion:
    """Преобразовать ADF в канонический markdown без изменения входного словаря."""
    if not isinstance(document, Mapping):
        return AdfConversion("")
    reader = _AdfReader()
    return AdfConversion(reader.document(document), reader.warnings)


def _mark(mark_type: str, *, href: str | None = None) -> dict:
    mark = {"type": mark_type}
    if href is not None:
        mark["attrs"] = {"href": href}
    return mark


def _normalise_marks(marks: list[dict]) -> list[dict]:
    unique = {str(mark.get("type")): mark for mark in marks if mark.get("type") in _MARK_ORDER}
    return [unique[mark_type] for mark_type in _MARK_ORDER if mark_type in unique]


def _with_mark(marks: list[dict], mark: dict) -> list[dict]:
    return _normalise_marks([*marks, mark])


def _append_text(out: list[dict], text: str, marks: list[dict]) -> None:
    if not text:
        return
    marks = _normalise_marks(marks)
    if out and out[-1].get("type") == "text" and out[-1].get("marks", []) == marks:
        out[-1]["text"] += text
        return
    node: dict = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    out.append(node)


def _find_unescaped(text: str, token: str, start: int) -> int:
    position = start
    while position <= len(text) - len(token):
        if text[position] == "\\":
            position += 2
        elif text.startswith(token, position):
            return position
        else:
            position += 1
    return -1


def _unescape_text(text: str) -> str:
    out: list[str] = []
    position = 0
    while position < len(text):
        if text[position] == "\\" and position + 1 < len(text):
            out.append(text[position + 1])
            position += 2
        else:
            out.append(text[position])
            position += 1
    return "".join(out)


def _parse_inline(text: str, marks: list[dict] | None = None) -> list[dict]:
    """Разобрать только инлайн-формы, которые сам ADF-конвертер выдаёт."""
    inherited = _normalise_marks(list(marks or []))
    out: list[dict] = []
    position = 0
    plain: list[str] = []

    def flush_plain() -> None:
        _append_text(out, "".join(plain), inherited)
        plain.clear()

    while position < len(text):
        if text[position] == "\\":
            if position + 1 < len(text):
                plain.append(text[position + 1])
                position += 2
            else:
                plain.append("\\")
                position += 1
            continue
        if text[position] == "[":
            close_label = _find_unescaped(text, "](", position + 1)
            close_href = _find_unescaped(text, ")", close_label + 2) if close_label != -1 else -1
            if close_href != -1:
                flush_plain()
                href = _unescape_text(text[close_label + 2:close_href])
                out.extend(_parse_inline(text[position + 1:close_label], _with_mark(inherited, _mark("link", href=href))))
                position = close_href + 1
                continue
        if text.startswith("***", position):
            close = _find_unescaped(text, "***", position + 3)
            if close != -1:
                flush_plain()
                combined = _with_mark(_with_mark(inherited, _mark("strong")), _mark("em"))
                out.extend(_parse_inline(text[position + 3:close], combined))
                position = close + 3
                continue
        if text.startswith("**", position):
            close = _find_unescaped(text, "**", position + 2)
            if close != -1:
                flush_plain()
                out.extend(_parse_inline(text[position + 2:close], _with_mark(inherited, _mark("strong"))))
                position = close + 2
                continue
        if text[position] == "*" and not text.startswith("**", position):
            close = _find_unescaped(text, "*", position + 1)
            if close != -1:
                flush_plain()
                out.extend(_parse_inline(text[position + 1:close], _with_mark(inherited, _mark("em"))))
                position = close + 1
                continue
        if text[position] == chr(96):
            close = _find_unescaped(text, chr(96), position + 1)
            if close != -1:
                flush_plain()
                out.extend(_parse_inline(text[position + 1:close], _with_mark(inherited, _mark("code"))))
                position = close + 1
                continue
        plain.append(text[position])
        position += 1
    flush_plain()
    return out


def _paragraph(text: str) -> dict:
    content: list[dict] = []
    for index, line in enumerate(text.split("\n")):
        if index:
            content.append({"type": "hardBreak"})
        content.extend(_parse_inline(line))
    return {"type": "paragraph", "content": content}


def _code_block(lines: list[str], language: str) -> dict:
    return {
        "type": "codeBlock",
        "attrs": {"language": language} if language else {},
        "content": [{"type": "text", "text": "\n".join(lines)}] if lines else [],
    }


def _fence_parts(line: str) -> tuple[str, str] | None:
    marker = chr(96)
    if not line.startswith(marker):
        return None
    length = 0
    while length < len(line) and line[length] == marker:
        length += 1
    if length < 3:
        return None
    return marker * length, line[length:].strip()


def _list_match(line: str, *, ordered: bool) -> re.Match[str] | None:
    return (_ORDERED_RE if ordered else _BULLET_RE).match(line)


def _parse_list(lines: list[str], index: int, *, ordered: bool) -> tuple[dict, int]:
    first = _list_match(lines[index], ordered=ordered)
    assert first is not None
    start = int(first.group(1)) if ordered else 1
    items: list[dict] = []

    while index < len(lines):
        match = _list_match(lines[index], ordered=ordered)
        if match is None:
            break
        item_lines = [match.group(2) if ordered else match.group(1)]
        index += 1
        while index < len(lines):
            line = lines[index]
            if _list_match(line, ordered=ordered):
                break
            if line.startswith("  "):
                item_lines.append(line[2:])
                index += 1
                continue
            if not line and index + 1 < len(lines) and lines[index + 1].startswith("  "):
                item_lines.append("")
                index += 1
                continue
            break
        items.append({"type": "listItem", "content": _parse_markdown_blocks("\n".join(item_lines))})

    node: dict = {"type": "orderedList" if ordered else "bulletList", "content": items}
    if ordered and start != 1:
        node["attrs"] = {"order": start}
    return node, index


def _parse_markdown_blocks(markdown: str) -> list[dict]:
    lines = markdown.rstrip("\n").split("\n") if markdown else []
    blocks: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        fence_parts = _fence_parts(line)
        if fence_parts:
            fence, language = fence_parts
            index += 1
            code: list[str] = []
            while index < len(lines) and lines[index] != fence:
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(_code_block(code, language))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append({"type": "heading", "attrs": {"level": len(heading.group(1))}, "content": _parse_inline(heading.group(2))})
            index += 1
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            block, index = _parse_list(lines, index, ordered=False)
            blocks.append(block)
            continue
        ordered = _ORDERED_RE.match(line)
        if ordered:
            block, index = _parse_list(lines, index, ordered=True)
            blocks.append(block)
            continue
        if line.startswith(">"):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].startswith(">"):
                quote_lines.append(lines[index][2:] if lines[index].startswith("> ") else lines[index][1:])
                index += 1
            blocks.append({"type": "blockquote", "content": [_paragraph("\n".join(quote_lines))]})
            continue

        paragraph_lines = [line]
        index += 1
        while index < len(lines) and lines[index]:
            candidate = lines[index]
            if (candidate.startswith("```") or _HEADING_RE.match(candidate) or _BULLET_RE.match(candidate)
                    or _ORDERED_RE.match(candidate) or candidate.startswith(">")):
                break
            paragraph_lines.append(candidate)
            index += 1
        blocks.append(_paragraph("\n".join(paragraph_lines)))
    return blocks


def markdown_to_adf(markdown: str) -> AdfConversion:
    """Преобразовать каноническое подмножество markdown в новый ADF document."""
    return AdfConversion({"type": "doc", "version": 1, "content": _parse_markdown_blocks(markdown)})


def _contains_link(node: object, href: str) -> bool:
    if isinstance(node, Mapping):
        if node.get("type") == "link":
            attrs = node.get("attrs")
            if isinstance(attrs, Mapping) and attrs.get("href") == href:
                return True
        return any(_contains_link(value, href) for value in node.values())
    if isinstance(node, list):
        return any(_contains_link(value, href) for value in node)
    return False


def adf_contains_link(document: Mapping[str, object] | None, href: str) -> bool:
    """Проверить наличие именно ADF link-mark с указанным ``href``."""
    return isinstance(document, Mapping) and _contains_link(document, href)


def append_link_paragraph(
    document: Mapping[str, object] | None,
    href: str,
    *,
    label: str,
    note: str | None,
) -> dict:
    """Добавить ссылку и отдельную заметку, не меняя входной ADF document."""
    result = deepcopy(dict(document)) if isinstance(document, Mapping) else {"type": "doc", "version": 1}
    content = result.get("content")
    if not isinstance(content, list):
        content = []
        result["content"] = content
    if adf_contains_link(result, href):
        return result
    content.append({"type": "paragraph", "content": [{"type": "text", "text": label, "marks": [_mark("link", href=href)]}]})
    if note is not None:
        content.append(_paragraph(note))
    result.setdefault("type", "doc")
    result.setdefault("version", 1)
    return result
