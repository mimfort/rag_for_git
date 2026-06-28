from io import BytesIO

from reviewer.tasks.boards.attachments import _DOCX_MIME, extract_text

# Минимальный валидный PDF с извлекаемым текстом "PDF SPEC CONTENT".
# pypdf логирует безвредный warning про startxref и пересобирает xref сам.
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 58 >>
stream
BT /F1 18 Tf 20 100 Td (PDF SPEC CONTENT) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000241 00000 n
0000000349 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
420
%%EOF"""


def _docx_bytes(*paragraphs: str) -> bytes:
    from docx import Document
    buf = BytesIO()
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(buf)
    return buf.getvalue()


def test_extract_md_decodes_utf8():
    data = "# Спец\nстрока".encode("utf-8")
    assert extract_text("spec.md", "text/markdown", data) == "# Спец\nстрока"


def test_extract_txt_decodes():
    assert extract_text("notes.txt", None, b"plain text") == "plain text"


def test_extract_docx():
    data = _docx_bytes("Спека ТЗ строка один", "строка два")
    text = extract_text("spec.docx", None, data)
    assert "Спека ТЗ строка один" in text
    assert "строка два" in text


def test_extract_pdf():
    text = extract_text("spec.pdf", None, _MINIMAL_PDF)
    assert "PDF SPEC CONTENT" in text


def test_extract_unknown_returns_none():
    assert extract_text("image.png", "image/png", b"\x89PNG\r\n") is None


def test_extract_corrupt_docx_failsoft():
    assert extract_text("broken.docx", None, b"not a real docx") is None


def test_extract_empty_text_returns_none():
    assert extract_text("empty.md", None, b"   ") is None


# --- тесты mime-фолбэка (PRI-196) ---

def test_extract_by_mime_when_extension_unknown():
    """Файл без значимого расширения + mime=text/markdown → декодируется как текст."""
    data = "# Заголовок\nтело".encode("utf-8")
    result = extract_text("attachment", "text/markdown", data)
    assert result == "# Заголовок\nтело"


def test_extract_docx_by_mime():
    """Файл без расширения + mime=docx → парсится как docx."""
    data = _docx_bytes("Раздел один", "Раздел два")
    result = extract_text("file", _DOCX_MIME, data)
    assert result is not None
    assert "Раздел один" in result
    assert "Раздел два" in result


def test_extract_extension_wins_over_mime():
    """Расширение первично: .md + mime=application/pdf → декодируется как текст, не pdf."""
    data = "# Спецификация".encode("utf-8")
    result = extract_text("spec.md", "application/pdf", data)
    assert result == "# Спецификация"
