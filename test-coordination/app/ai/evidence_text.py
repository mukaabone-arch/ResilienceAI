"""Read the text out of an evidence file, safely, for the pre-checks.

Files are untrusted: every reader has a size cap, Office files are read as zip
archives with a cap on what is decompressed, and XML is stripped with a regex
rather than parsed (no entity expansion). Anything that can't be read comes
back as kind "image" / "binary" / "unreadable" with no text, and the checks
that need text are skipped with a note rather than guessed.
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass, field

MAX_CHARS = 2_000_000          # about 25k lines of a typical log
MAX_LINES = 40_000
MAX_UNZIPPED = 20 * 1024 * 1024
MAX_PDF_PAGES = 200

TEXT_EXT = {"txt", "log", "csv", "json", "xml", "eml"}
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
TAG = re.compile(r"<[^>]+>")


@dataclass
class Extracted:
    kind: str                          # text | pdf | docx | xlsx | pptx | image | binary | unreadable
    lines: list[str] = field(default_factory=list)
    truncated: bool = False
    note: str | None = None

    @property
    def readable(self) -> bool:
        return bool(self.lines)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def _finish(kind: str, text: str, note: str | None = None) -> Extracted:
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS].replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        lines, truncated = lines[:MAX_LINES], True
    lines = [x.rstrip() for x in lines]
    if not any(x.strip() for x in lines):
        return Extracted("unreadable" if kind != "image" else kind, note=note or "No text could be read from the file.")
    return Extracted(kind, lines, truncated, note)


def _zip_parts(data: bytes, pattern: str) -> list[tuple[str, str]]:
    out, budget = [], MAX_UNZIPPED
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(pattern, n)), key=_natural)
        for n in names:
            if budget <= 0:
                break
            with z.open(n) as f:
                chunk = f.read(budget + 1)[:budget]
            budget -= len(chunk)
            out.append((n, chunk.decode("utf-8", "replace")))
    return out


def _natural(s: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]


def _xml_text(xml: str, para: str) -> str:
    xml = re.sub(para, "\n", xml)
    return html.unescape(TAG.sub("", xml))


def _docx(data: bytes) -> str:
    parts = _zip_parts(data, r"word/document\.xml")
    return "\n".join(_xml_text(x, r"</w:p>|<w:br\s*/>|<w:tab\s*/>") for _, x in parts)


def _pptx(data: bytes) -> str:
    parts = _zip_parts(data, r"ppt/slides/slide\d+\.xml")
    return "\n".join(_xml_text(x, r"</a:p>") for _, x in parts)


def _xlsx(data: bytes) -> str:
    shared = [html.unescape(TAG.sub("", m)) for _, x in _zip_parts(data, r"xl/sharedStrings\.xml")
              for m in re.findall(r"<si>(.*?)</si>", x, re.S)]
    rows = []
    for _, sheet in _zip_parts(data, r"xl/worksheets/sheet\d+\.xml"):
        for r in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
            cells = []
            for attrs, body in re.findall(r"<c([^>]*)>(.*?)</c>", r, re.S):
                v = re.search(r"<v>(.*?)</v>", body, re.S)
                inline = re.search(r"<is>(.*?)</is>", body, re.S)
                if 't="s"' in attrs and v and v.group(1).isdigit() and int(v.group(1)) < len(shared):
                    cells.append(shared[int(v.group(1))])
                elif inline:
                    cells.append(html.unescape(TAG.sub("", inline.group(1))))
                elif v:
                    cells.append(html.unescape(v.group(1)))
            if cells:
                rows.append("\t".join(cells))
    return "\n".join(rows)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    out, size = [], 0
    for page in reader.pages[:MAX_PDF_PAGES]:
        t = page.extract_text() or ""
        out.append(t)
        size += len(t)
        if size > MAX_CHARS:
            break
    return "\n".join(out)


def extract(filename: str, data: bytes) -> Extracted:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in IMAGE_EXT:
        return Extracted("image", note="This is an image, so its text can't be read here. The reviewer should check it by eye.")
    try:
        if ext in TEXT_EXT:
            return _finish("text", _decode(data[: MAX_CHARS * 2]))
        if ext == "pdf":
            return _finish("pdf", _pdf(data), None)
        if ext == "docx":
            return _finish("docx", _docx(data))
        if ext == "pptx":
            return _finish("pptx", _pptx(data))
        if ext == "xlsx":
            return _finish("xlsx", _xlsx(data))
    except Exception as e:  # noqa: BLE001 - a malformed file is reported, never fatal to the upload
        return Extracted("unreadable", note=f"The file couldn't be read ({type(e).__name__}).")
    return Extracted("binary", note=f"Text can't be read from .{ext or '(none)'} files here.")
