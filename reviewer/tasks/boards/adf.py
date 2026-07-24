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
            return f"```{suffix}\n{self.text_content(content)}\n```"
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
            text = self.block(item)
            if not text:
                continue
            prefix = f"{start + index}. " if ordered else "- "
            first, *rest = text.splitlines()
            lines.append(prefix + first)
            lines.extend(f"  {line}" if line else "" for line in rest)
        return "\n".join(lines)

    def inline_children(self, content: list[Mapping[str, object]]) -> str:
        return "".join(self.inline(child) for child in content)

    def inline(self, node: Mapping[str, object]) -> str:
        node_type = node.get("type")
        if node_type == "hardBreak":
            return "\n"
        if node_type != "text":
            self._warn_unknown(node_type)
            return self.inline_children(self._content(node))

        text = node.get("text")
        result = text if isinstance(text, str) else ""
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
            result = f"[{result}]({href})"
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


def _append_text(out: list[dict], text: str, marks: list[dict]) -> None:
    if not text:
        return
    if out and out[-1].get("type") == "text" and out[-1].get("marks", []) == marks:
        out[-1]["text"] += text
        return
    node: dict = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    out.append(node)


def _parse_inline(text: str, marks: list[dict] | None = None) -> list[dict]:
    """Разобрать только инлайн-формы, которые сам ADF-конвертер выдаёт."""
    inherited = list(marks or [])
    out: list[dict] = []
    position = 0
    plain_start = 0
    while position < len(text):
        if text.startswith("**", position):
            close = text.find("**", position + 2)
            if close != -1:
                _append_text(out, text[plain_start:position], inherited)
                out.extend(_parse_inline(text[position + 2:close], inherited + [_mark("strong")]))
                position = close + 2
                plain_start = position
                continue
        if text[position] == "*" and not text.startswith("**", position):
            close = text.find("*", position + 1)
            if close != -1:
                _append_text(out, text[plain_start:position], inherited)
                out.extend(_parse_inline(text[position + 1:close], inherited + [_mark("em")]))
                position = close + 1
                plain_start = position
                continue
        if text[position] == "`":
            close = text.find("`", position + 1)
            if close != -1:
                _append_text(out, text[plain_start:position], inherited)
                _append_text(out, text[position + 1:close], inherited + [_mark("code")])
                position = close + 1
                plain_start = position
                continue
        if text[position] == "[":
            close_label = text.find("](", position + 1)
            close_href = text.find(")", close_label + 2) if close_label != -1 else -1
            if close_href != -1:
                _append_text(out, text[plain_start:position], inherited)
                label = text[position + 1:close_label]
                href = text[close_label + 2:close_href]
                out.extend(_parse_inline(label, inherited + [_mark("link", href=href)]))
                position = close_href + 1
                plain_start = position
                continue
        position += 1
    _append_text(out, text[plain_start:], inherited)
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


def _parse_markdown_blocks(markdown: str) -> list[dict]:
    lines = markdown.rstrip("\n").split("\n") if markdown else []
    blocks: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        if line.startswith("```"):
            language = line[3:].strip()
            index += 1
            code: list[str] = []
            while index < len(lines) and lines[index] != "```":
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
            items: list[dict] = []
            while index < len(lines):
                match = _BULLET_RE.match(lines[index])
                if not match:
                    break
                items.append({"type": "listItem", "content": [_paragraph(match.group(1))]})
                index += 1
            blocks.append({"type": "bulletList", "content": items})
            continue
        ordered = _ORDERED_RE.match(line)
        if ordered:
            start = int(ordered.group(1))
            items: list[dict] = []
            while index < len(lines):
                match = _ORDERED_RE.match(lines[index])
                if not match:
                    break
                items.append({"type": "listItem", "content": [_paragraph(match.group(2))]})
                index += 1
            attrs = {"order": start} if start != 1 else {}
            blocks.append({"type": "orderedList", "attrs": attrs, "content": items})
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
