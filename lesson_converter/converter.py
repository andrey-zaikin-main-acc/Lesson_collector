"""Безопасный статический конвертер сохранённых HTML-страниц курса."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from lxml import etree, html


MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_FILE_SIZE = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
MAX_IFRAME_DEPTH = 5

_SPACE_RE = re.compile(r"\s+")
_CSS_URL_RE = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_BAD_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_URL_ATTR_NAMES = {
    "src", "srcset", "href", "poster", "data", "action", "formaction",
    "background", "cite", "longdesc", "manifest",
}
_REMOVED_TAGS = {"script", "style", "link", "meta", "base", "noscript", "template"}
_UNSUPPORTED_TAGS = {"video", "audio", "iframe", "object", "embed", "canvas", "applet"}
_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "img": {"src", "alt", "title", "width", "height"},
    "table": {"summary"},
    "th": {"colspan", "rowspan", "scope", "abbr"},
    "td": {"colspan", "rowspan", "headers"},
    "ol": {"start", "reversed", "type"},
    "li": {"value"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "time": {"datetime"},
    "abbr": {"title"},
    "details": {"open"},
    "a": {"href"},
}

_STATIC_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { background: #f3f5f8; }
body {
  margin: 0; color: #20242a; background: #f3f5f8;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
  font-size: 18px; line-height: 1.62;
}
main {
  width: min(100% - 32px, 980px); margin: 32px auto;
  padding: clamp(24px, 5vw, 64px); background: #fff; border-radius: 16px;
  box-shadow: 0 8px 32px rgba(25, 38, 57, .09); overflow-wrap: anywhere;
}
h1, h2, h3, h4, h5, h6 { line-height: 1.25; color: #152033; page-break-after: avoid; }
h1 { margin: 0 0 1.2em; font-size: clamp(2rem, 5vw, 3.2rem); }
h2 { margin: 2.2em 0 .8em; font-size: clamp(1.55rem, 3.2vw, 2.15rem); }
h3 { margin: 1.8em 0 .65em; font-size: 1.35rem; }
p { margin: .8em 0; }
ul, ol { padding-left: 1.7em; }
li + li { margin-top: .35em; }
figure { margin: 1.5em 0; page-break-inside: avoid; }
figcaption { margin-top: .6em; color: #5b6472; font-size: .92em; text-align: center; }
img, svg { display: block; max-width: 100%; height: auto; margin: 1.2em auto; }
table { width: 100%; margin: 1.4em 0; border-collapse: collapse; font-size: .94em; }
caption { margin-bottom: .6em; font-weight: 600; }
th, td { padding: .65em .75em; border: 1px solid #b8c0cc; text-align: left; vertical-align: top; overflow-wrap: normal; word-break: normal; }
th { background: #eef2f7; }
th:first-child, td:first-child { min-width: 10rem; }
blockquote { margin: 1.2em 0; padding: .6em 1.2em; border-left: 4px solid #5683c4; background: #f5f8fc; }
pre, code { font-family: Consolas, "Courier New", monospace; }
pre { padding: 1em; overflow: auto; background: #f0f2f5; border-radius: 8px; white-space: pre-wrap; }
.lc-external { color: inherit; text-decoration: none; }
.lc-address { color: #596273; font-size: .9em; }
.lc-static-control { margin: .5em 0; }
.lc-added { margin: 1em 0; padding: .8em 1em; border: 2px dashed #bb6b00; background: #fff8e8; color: #633b00; }
details { display: block; margin: 1em 0; }
summary { font-weight: 600; }
@media (max-width: 640px) {
  body { font-size: 16px; }
  main { width: 100%; margin: 0; padding: 22px 18px; border-radius: 0; box-shadow: none; }
  table { display: block; overflow-x: auto; }
}
@media print {
  @page { margin: 16mm; }
  html, body { background: #fff; font-size: 11pt; }
  main { width: auto; margin: 0; padding: 0; box-shadow: none; }
  img, svg { max-height: 240mm; }
  table { display: table; font-size: 9.5pt; }
  tr, img, figure, blockquote { page-break-inside: avoid; }
}
""".strip()


class ConversionError(RuntimeError):
    """Понятная пользователю ошибка конвертации."""


@dataclass(frozen=True)
class ConversionResult:
    html_path: Path
    report_path: Path
    report: dict[str, Any]


@dataclass
class _Document:
    path: Path
    tree: html.HtmlElement
    root: html.HtmlElement
    title: str
    score: int


def _normalize_text(value: str) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFC", value)).strip()


def _local_name(element: etree._Element) -> str:
    if not isinstance(element.tag, str):
        return ""
    return etree.QName(element).localname.lower()


def _iter_elements(root: etree._Element) -> Iterator[etree._Element]:
    for element in root.iter():
        if isinstance(element.tag, str):
            yield element


def _remove_element(element: etree._Element) -> None:
    parent = element.getparent()
    if parent is None:
        return
    if element.tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + element.tail
        else:
            parent.text = (parent.text or "") + element.tail
    parent.remove(element)


def _replace_element(old: etree._Element, new: etree._Element) -> None:
    parent = old.getparent()
    if parent is None:
        return
    new.tail = old.tail
    parent.replace(old, new)


def _read_html(path: Path) -> html.HtmlElement:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConversionError(f"Не удалось прочитать HTML «{path.name}»: {exc}") from exc
    try:
        # Сохранённые браузером страницы обычно UTF-8. Если декларации кодировки
        # нет, lxml по стандарту HTML выберет Latin-1 и повредит кириллицу в URL.
        # Строгая проверка UTF-8 сохраняет корректное поведение для старых кодировок.
        if not re.search(br"charset\s*=|<\?xml[^>]+encoding\s*=", raw[:4096], re.I):
            try:
                text = raw.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError:
                pass
            else:
                return html.document_fromstring(
                    text, parser=html.HTMLParser(recover=True, remove_comments=False)
                )
        return html.document_fromstring(
            raw, parser=html.HTMLParser(recover=True, remove_comments=False)
        )
    except (etree.ParserError, ValueError) as exc:
        raise ConversionError(f"Файл «{path.name}» не удалось разобрать как HTML.") from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_extract(archive: Path, target: Path) -> None:
    try:
        zip_file = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConversionError(f"Архив ресурсов «{archive.name}» повреждён или недоступен.") from exc

    with zip_file:
        infos = zip_file.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise ConversionError("В архиве слишком много файлов.")
        total_size = sum(info.file_size for info in infos)
        if total_size > MAX_ARCHIVE_TOTAL_SIZE:
            raise ConversionError("Суммарный размер файлов архива превышает безопасный предел 2 ГБ.")

        root = target.resolve()
        for info in infos:
            raw_name = info.filename.replace("\\", "/")
            parts = PurePosixPath(raw_name).parts
            if (
                not raw_name or raw_name.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
                or (parts and ":" in parts[0])
            ):
                raise ConversionError(f"Небезопасный путь в ZIP: {raw_name!r}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ConversionError(f"Символические ссылки в ZIP не поддерживаются: {raw_name!r}")
            if info.flag_bits & 0x1:
                raise ConversionError(f"Зашифрованный файл в ZIP не поддерживается: {raw_name!r}")
            if info.file_size > MAX_ARCHIVE_FILE_SIZE:
                raise ConversionError(f"Файл в архиве слишком велик: {raw_name!r}")

            destination = (target.joinpath(*parts)).resolve()
            if destination != root and not _is_relative_to(destination, root):
                raise ConversionError(f"Небезопасный путь в ZIP: {raw_name!r}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _find_archive(source: Path) -> Path | None:
    exact = source.with_name(f"{source.stem}_files.zip")
    if exact.is_file():
        return exact
    prefix = f"{source.stem}_files".casefold()
    matches = [item for item in source.parent.glob("*.zip") if item.stem.casefold() == prefix]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConversionError("Найдено несколько подходящих архивов _files.zip.")
    return None


def _prepare_resources(source: Path, temporary_root: Path) -> tuple[list[Path], str]:
    folder = source.with_name(f"{source.stem}_files")
    if folder.is_dir():
        return [source.parent.resolve()], "folder"
    archive = _find_archive(source)
    if archive is None:
        return [source.parent.resolve()], "not_found"
    extraction_root = temporary_root.resolve()
    _safe_extract(archive, extraction_root)
    return [extraction_root, source.parent.resolve()], "zip"


def _clean_local_reference(reference: str) -> str | None:
    parsed = urllib.parse.urlsplit(reference.strip())
    if parsed.scheme and parsed.scheme.casefold() != "file":
        return None
    if parsed.netloc and parsed.scheme.casefold() != "file":
        return None
    path = urllib.parse.unquote(parsed.path).replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    parts = PurePosixPath(path).parts
    if any(part == ".." for part in parts):
        return None
    return str(PurePosixPath(*[part for part in parts if part not in {"", "."}]))


def _resolve_local_file(reference: str, document_path: Path, roots: Iterable[Path]) -> Path | None:
    cleaned = _clean_local_reference(reference)
    if cleaned is None:
        return None
    relative = Path(*PurePosixPath(cleaned).parts)
    candidates = [document_path.parent / relative]
    candidates.extend(root / relative for root in roots)
    allowed_roots = [document_path.parent.resolve(), *(root.resolve() for root in roots)]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not any(resolved == root or _is_relative_to(resolved, root) for root in allowed_roots):
            continue
        if resolved.is_file():
            return resolved
    return None


def _class_token_xpath(token: str) -> str:
    return f'contains(concat(" ", normalize-space(@class), " "), " {token} ")'


def _candidate_content_roots(tree: html.HtmlElement) -> list[html.HtmlElement]:
    candidates: list[html.HtmlElement] = []
    candidates.extend(tree.xpath(f"//*[{_class_token_xpath('player-rich-text-editor')}]")[:10])
    candidates.extend(tree.xpath("//main | //article | //*[@role='main']")[:30])
    if not candidates:
        bodies = tree.xpath("//body")
        candidates.append(bodies[0] if bodies else tree)
    unique: list[html.HtmlElement] = []
    seen: set[int] = set()
    for candidate in candidates:
        marker = id(candidate)
        if marker not in seen:
            seen.add(marker)
            unique.append(candidate)
    return unique


def _meaningful_text(root: etree._Element) -> str:
    clone = copy.deepcopy(root)
    for element in list(_iter_elements(clone)):
        if _local_name(element) in _REMOVED_TAGS or _local_name(element) == "input":
            _remove_element(element)
    return _normalize_text(clone.text_content())


def _choose_content_root(tree: html.HtmlElement) -> tuple[html.HtmlElement, int]:
    best: tuple[int, html.HtmlElement] | None = None
    for candidate in _candidate_content_roots(tree):
        text_length = len(_meaningful_text(candidate))
        headings = len(candidate.xpath(".//*[self::h1 or self::h2 or self::h3 or self::h4]"))
        images = len(candidate.xpath(".//img | .//*[local-name()='image']"))
        tables = len(candidate.xpath(".//table"))
        score = text_length + headings * 500 + images * 350 + tables * 500
        if best is None or score > best[0]:
            best = (score, candidate)
    assert best is not None
    return best[1], best[0]


def _title_for(tree: html.HtmlElement, root: html.HtmlElement, fallback: str) -> str:
    for matches in (root.xpath(".//h1"), tree.xpath("//h1"), tree.xpath("//title")):
        for match in matches:
            value = _normalize_text(match.text_content())
            if value:
                return value
    return fallback


def _discover_documents(source: Path, resource_roots: list[Path]) -> tuple[_Document, list[dict[str, str]]]:
    queue: list[tuple[Path, int]] = [(source.resolve(), 0)]
    visited: set[Path] = set()
    documents: list[_Document] = []
    missing_iframes: list[dict[str, str]] = []
    while queue:
        path, depth = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        tree = _read_html(path)
        root, score = _choose_content_root(tree)
        title = _title_for(tree, root, source.stem)
        documents.append(_Document(path, tree, root, title, score))
        if depth >= MAX_IFRAME_DEPTH:
            continue
        for frame in tree.xpath("//iframe[@src]"):
            reference = frame.get("src", "")
            resolved = _resolve_local_file(reference, path, resource_roots)
            if resolved is not None:
                queue.append((resolved, depth + 1))
            else:
                missing_iframes.append({"reference": _safe_reference(reference), "importance": "significant"})
    if not documents:
        raise ConversionError("Не найден HTML-документ для преобразования.")
    return max(documents, key=lambda item: item.score), missing_iframes


def _safe_reference(reference: str) -> str:
    value = reference.strip()
    if value.lower().startswith("data:"):
        media = value[5:].split(";", 1)[0].split(",", 1)[0]
        return f"data:{media or 'resource'}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    cleaned = urllib.parse.unquote(parsed.path).replace("\\", "/")
    return str(PurePosixPath(cleaned)).lstrip("./") or "(пустая ссылка)"


def _detect_image_mime(data: bytes, name: str = "") -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if len(data) >= 12 and data[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif"
    prefix = data[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if prefix.startswith(b"<svg") or (prefix.startswith(b"<?xml") and b"<svg" in prefix):
        return "image/svg+xml"
    guessed, _ = mimetypes.guess_type(name)
    if guessed and guessed.startswith("image/"):
        return guessed
    return None


def _decode_data_image(reference: str) -> tuple[str, bytes] | None:
    if not reference.lower().startswith("data:"):
        return None
    try:
        header, payload = reference.split(",", 1)
        mime = header[5:].split(";", 1)[0].casefold()
        if not mime.startswith("image/"):
            return None
        if ";base64" in header.casefold():
            data = base64.b64decode(payload, validate=True)
        else:
            data = urllib.parse.unquote_to_bytes(payload)
    except (ValueError, base64.binascii.Error):
        return None
    detected = _detect_image_mime(data)
    return (detected or mime, data) if data else None


def _svg_is_self_contained(data: bytes) -> bool:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return False
    try:
        root = etree.fromstring(
            data,
            parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=False),
        )
    except (etree.XMLSyntaxError, ValueError):
        return False
    forbidden_tags = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video"}
    for element in _iter_elements(root):
        tag = _local_name(element)
        if tag in forbidden_tags:
            return False
        if tag == "style":
            css = "".join(element.itertext())
            if re.search(r"@import\b", css, re.I):
                return False
            for _, url in _CSS_URL_RE.findall(css):
                lowered_url = url.strip().lower()
                if not (lowered_url.startswith("#") or lowered_url.startswith("data:image/")):
                    return False
        for name, value in element.attrib.items():
            local = etree.QName(name).localname.lower() if name.startswith("{") else name.lower()
            lowered_value = value.strip().lower()
            if local.startswith("on"):
                return False
            if local in {"href", "src"} and not (
                lowered_value.startswith("#")
                or (
                    lowered_value.startswith("data:image/")
                    and not lowered_value.startswith("data:image/svg+xml")
                )
            ):
                return False
            if local == "style" or "url(" in lowered_value:
                for _, url in _CSS_URL_RE.findall(value):
                    lowered_url = url.strip().lower()
                    if not (lowered_url.startswith("#") or lowered_url.startswith("data:image/")):
                        return False
    return True


def _image_data(
    reference: str, document_path: Path, resource_roots: list[Path]
) -> tuple[tuple[str, bytes, Path | None] | None, str | None]:
    inline = _decode_data_image(reference)
    if inline is not None:
        if inline[0] == "image/svg+xml" and not _svg_is_self_contained(inline[1]):
            return None, "unsafe_svg"
        return (inline[0], inline[1], None), None
    path = _resolve_local_file(reference, document_path, resource_roots)
    if path is None:
        return None, "not_found"
    try:
        data = path.read_bytes()
    except OSError:
        return None, "not_found"
    mime = _detect_image_mime(data, path.name)
    if mime is None:
        return None, "unsupported_format"
    if mime == "image/svg+xml" and not _svg_is_self_contained(data):
        return None, "unsafe_svg"
    return (mime, data, path), None


def _srcset_first(value: str) -> str:
    first = value.split(",", 1)[0].strip()
    return first.split()[0] if first else ""


def _append_added_text(parent: etree._Element, text: str, css_class: str) -> None:
    node = etree.Element("span")
    node.set("class", f"lc-added {css_class}".strip())
    node.text = text
    parent.append(node)


def _image_placeholder(element: etree._Element, reference: str, reason: str) -> None:
    placeholder = etree.Element("div")
    placeholder.set("class", "lc-added")
    alt = _normalize_text(element.get("alt", ""))
    label = f": {alt}" if alt else ""
    reason_text = {
        "not_found": "Изображение отсутствует",
        "unsupported_format": "Формат изображения не распознан",
        "unsafe_svg": "SVG содержит небезопасные или внешние зависимости",
    }.get(reason, "Изображение недоступно")
    placeholder.text = f"[{reason_text}{label}; источник: {_safe_reference(reference)}]"
    _replace_element(element, placeholder)


def _process_images(root: etree._Element, document_path: Path, resource_roots: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    images = [element for element in _iter_elements(root) if _local_name(element) in {"img", "image"}]
    for element in images:
        tag = _local_name(element)
        reference = element.get("src") or element.get("href") or element.get("{http://www.w3.org/1999/xlink}href")
        if not reference and element.get("srcset"):
            reference = _srcset_first(element.get("srcset", ""))
        reference = reference or ""
        safe_ref = _safe_reference(reference)
        loaded, error = (
            _image_data(reference, document_path, resource_roots)
            if reference
            else (None, "not_found")
        )
        if loaded is None:
            status = "missing" if error == "not_found" else "unsupported"
            records.append({
                "source": safe_ref,
                "status": status,
                "reason": error,
                "importance": "significant",
            })
            if tag == "img":
                _image_placeholder(element, reference, error or "not_found")
            else:
                _remove_element(element)
            continue

        mime, data, local_path = loaded
        digest = hashlib.sha256(data).hexdigest()
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        if tag == "img":
            element.set("src", uri)
            element.attrib.pop("srcset", None)
        else:
            element.set("href", uri)
            element.attrib.pop("{http://www.w3.org/1999/xlink}href", None)
        records.append({
            "source": safe_ref, "status": "embedded", "mime_type": mime,
            "bytes": len(data), "sha256": digest,
            "extension_was_missing": bool(local_path and not local_path.suffix),
        })
    return records


def _convert_links(root: etree._Element) -> None:
    anchors = [element for element in _iter_elements(root) if _local_name(element) == "a"]
    existing_ids = {element.get("id") for element in _iter_elements(root) if element.get("id")}
    anchor_map: dict[str, str] = {}
    next_anchor = 1
    for anchor in anchors:
        href = anchor.get("href", "").strip()
        if href.startswith("#") and href[1:] in existing_ids:
            original = href[1:]
            if original not in anchor_map:
                anchor_map[original] = f"anchor-{next_anchor}"
                next_anchor += 1
            anchor.set("href", f"#{anchor_map[original]}")
            continue
        anchor.tag = "span"
        anchor.set("class", "lc-external")
        if href and href not in {"#", "javascript:void(0);", "javascript:void(0)"}:
            visible = _normalize_text(anchor.text_content())
            address = _safe_reference(href)
            if address and address not in visible:
                _append_added_text(anchor, f" ({address})", "lc-address")
        anchor.attrib.pop("href", None)
    for element in _iter_elements(root):
        old_id = element.get("id")
        if old_id in anchor_map:
            element.set("id", anchor_map[old_id])


def _sanitize_svg_attributes(element: etree._Element) -> None:
    for name, value in list(element.attrib.items()):
        local = etree.QName(name).localname.lower() if name.startswith("{") else name.lower()
        lowered = value.strip().lower()
        if local.startswith("on") or local in {"style", "class", "tabindex", "role"} or local.startswith(("data-", "aria-")):
            del element.attrib[name]
        elif "url(" in lowered:
            urls = [match[1].strip(" \t\r\n'\"") for match in _CSS_URL_RE.findall(value)]
            if any(url and not url.startswith("#") and not url.lower().startswith("data:") for url in urls):
                del element.attrib[name]
        elif local in {"href", "src"} and not (lowered.startswith("#") or lowered.startswith("data:image/")):
            del element.attrib[name]


def _sanitize_tree(root: etree._Element, unsupported: list[dict[str, Any]]) -> None:
    for element in list(_iter_elements(root)):
        if _local_name(element) in _REMOVED_TAGS:
            _remove_element(element)

    for element in list(_iter_elements(root)):
        tag = _local_name(element)
        if tag in _UNSUPPORTED_TAGS:
            references = [_safe_reference(element.get(name, "")) for name in ("src", "poster", "data") if element.get(name)]
            original_text = _normalize_text(element.text_content())
            replacement = etree.Element("div")
            if original_text:
                preserved = etree.SubElement(replacement, "div")
                preserved.text = original_text
            note = etree.SubElement(replacement, "div")
            note.set("class", "lc-added")
            note.text = f"[Неподдерживаемый интерактивный элемент: {tag}]"
            unsupported.append({"type": tag, "references": references, "importance": "significant"})
            _replace_element(element, replacement)

    static_controls = 0
    removed_empty_controls = 0
    for element in list(_iter_elements(root)):
        tag = _local_name(element)
        if tag == "button":
            if not _normalize_text(element.text_content()) and not element.xpath(".//img"):
                removed_empty_controls += 1
                _remove_element(element)
                continue
            for icon in list(element.xpath(".//*[local-name()='svg']")):
                _remove_element(icon)
            static_controls += 1
            element.tag = "div"
            element.set("class", "lc-static-control")
        elif tag == "form":
            unsupported.append({"type": "form", "references": [], "importance": "significant"})
            element.tag = "section"
        elif tag == "input":
            _remove_element(element)
        elif tag in {"select", "option", "textarea"}:
            element.tag = "div"
        elif tag == "details":
            element.set("open", "")
        elif tag in {"source", "track"}:
            _remove_element(element)
        elif tag in {"foreignobject", "animate", "animatemotion", "animatetransform", "set"}:
            _remove_element(element)
    if static_controls:
        unsupported.append({
            "type": "static_controls", "count": static_controls, "importance": "informational",
            "note": "Кнопки сохранены как статическое содержимое; раскрывающиеся блоки видимы.",
            "empty_decorative_controls_removed": removed_empty_controls,
        })

    _convert_links(root)

    for element in _iter_elements(root):
        tag = _local_name(element)
        in_svg = tag == "svg" or any(_local_name(ancestor) == "svg" for ancestor in element.iterancestors())
        if in_svg:
            _sanitize_svg_attributes(element)
            continue
        allowed = _ALLOWED_ATTRIBUTES.get(tag, set())
        preserve_class = (element.get("class") or "").startswith("lc-")
        preserve_id = (element.get("id") or "").startswith("anchor-")
        for name in list(element.attrib):
            local = etree.QName(name).localname.lower() if name.startswith("{") else name.lower()
            if preserve_class and local == "class":
                continue
            if preserve_id and local == "id":
                continue
            if local not in allowed or local.startswith("on") or local in {"style", "hidden", "inert"}:
                element.attrib.pop(name, None)


def _baseline_text(root: etree._Element, title: str, add_title: bool) -> str:
    clone = copy.deepcopy(root)
    for element in list(_iter_elements(clone)):
        if _local_name(element) in _REMOVED_TAGS or _local_name(element) == "input":
            _remove_element(element)
    text = clone.text_content()
    return _normalize_text(f"{title} {text}" if add_title else text)


def _output_comparable_text(document: html.HtmlElement) -> str:
    main = document.xpath("//main")
    if not main:
        return ""
    clone = copy.deepcopy(main[0])
    for element in list(_iter_elements(clone)):
        if "lc-added" in set((element.get("class") or "").split()):
            _remove_element(element)
    return _normalize_text(clone.text_content())


def _other_section_references(document: _Document) -> list[str]:
    headings = {
        _normalize_text(element.text_content()).casefold()
        for element in document.root.xpath(".//*[self::h1 or self::h2 or self::h3 or self::h4]")
    }
    headings.add(document.title.casefold())
    generic = {"продолжить", "назад", "далее", "закрыть", "меню", "оглавление"}
    result: list[str] = []
    for button in document.tree.xpath("//button"):
        if document.root is button or document.root in button.iterancestors():
            continue
        value = _normalize_text(button.text_content())
        folded = value.casefold()
        if not value or len(value) > 160 or folded in headings or folded in generic:
            continue
        if value not in result:
            result.append(value)
    return result


def _safe_output_stem(stem: str) -> str:
    cleaned = _BAD_FILENAME_RE.sub("_", stem).strip(" .")
    cleaned = _SPACE_RE.sub(" ", cleaned)
    return (cleaned or "lesson")[:160].rstrip(" .")


def _build_document(title: str, content: etree._Element, add_title: bool) -> html.HtmlElement:
    output = html.Element("html", lang="ru")
    head = etree.SubElement(output, "head")
    etree.SubElement(head, "meta", charset="utf-8")
    etree.SubElement(head, "meta", name="viewport", content="width=device-width, initial-scale=1")
    etree.SubElement(head, "meta", **{
        "http-equiv": "Content-Security-Policy",
        "content": (
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
            "media-src data:; object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"
        ),
    })
    title_element = etree.SubElement(head, "title")
    title_element.text = title
    style = etree.SubElement(head, "style")
    style.text = _STATIC_CSS
    body = etree.SubElement(output, "body")
    main = etree.SubElement(body, "main")
    if add_title:
        heading = etree.SubElement(main, "h1")
        heading.text = title
    if _local_name(content) in {"body", "main", "article"}:
        if content.text and content.text.strip():
            lead = etree.SubElement(main, "div")
            lead.text = content.text
        for child in list(content):
            content.remove(child)
            main.append(child)
    else:
        main.append(content)
    return output


def _serialize(document: html.HtmlElement) -> bytes:
    return html.tostring(document, encoding="utf-8", method="html", doctype="<!doctype html>")


def _audit_embedded_images(document: html.HtmlElement, records: list[dict[str, Any]]) -> dict[str, Any]:
    embedded_records = [record for record in records if record["status"] == "embedded"]
    image_nodes = [element for element in _iter_elements(document) if _local_name(element) in {"img", "image"}]
    digests: list[str] = []
    errors: list[str] = []
    for index, element in enumerate(image_nodes, start=1):
        reference = element.get("src") or element.get("href") or ""
        decoded = _decode_data_image(reference)
        if decoded is None:
            errors.append(f"Изображение {index} не содержит корректный data URI.")
            continue
        digests.append(hashlib.sha256(decoded[1]).hexdigest())
    expected = [record["sha256"] for record in embedded_records]
    if digests != expected:
        errors.append("Хеши встроенных изображений не совпали с исходными байтами.")
    return {"passed": not errors, "checked": len(digests), "errors": errors}


def _audit_dependencies(document: html.HtmlElement) -> dict[str, Any]:
    findings: list[str] = []
    for element in _iter_elements(document):
        tag = _local_name(element)
        if tag == "script":
            findings.append("script")
        if tag in {"iframe", "object", "embed", "form", "base", "link"}:
            findings.append(tag)
        if tag == "meta" and element.get("http-equiv", "").casefold() == "refresh":
            findings.append("meta refresh")
        for name, value in element.attrib.items():
            local = etree.QName(name).localname.lower() if name.startswith("{") else name.lower()
            if local.startswith("on"):
                findings.append(f"{tag}[{local}]")
            if local in _URL_ATTR_NAMES:
                values = [value]
                if local == "srcset":
                    values = [part.strip().split()[0] for part in value.split(",") if part.strip()]
                for candidate in values:
                    lowered = candidate.strip().lower()
                    if lowered and not (lowered.startswith("data:") or lowered.startswith("#") or lowered == "about:blank"):
                        findings.append(f"{tag}[{local}]={_safe_reference(candidate)}")
        for _, url in _CSS_URL_RE.findall(element.get("style", "")):
            if not url.lower().startswith(("data:", "#")):
                findings.append(f"style url={_safe_reference(url)}")
    for style in document.xpath("//style"):
        css = style.text or ""
        if re.search(r"@import\b", css, re.I):
            findings.append("CSS @import")
        for _, url in _CSS_URL_RE.findall(css):
            if not url.strip().lower().startswith(("data:", "#")):
                findings.append(f"CSS url={_safe_reference(url)}")
    unique = list(dict.fromkeys(findings))
    return {"passed": not unique, "findings": unique}


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def convert_lesson(
    input_html: str | os.PathLike[str], output_dir: str | os.PathLike[str], *, overwrite: bool = False
) -> ConversionResult:
    """Преобразовать один сохранённый раздел и вернуть пути результата."""

    source = Path(input_html).expanduser().resolve()
    if not source.is_file():
        raise ConversionError(f"Исходный HTML не найден: {source}")
    if source.suffix.casefold() not in {".html", ".htm"}:
        raise ConversionError("Исходный файл должен иметь расширение .html или .htm.")

    destination = Path(output_dir).expanduser().resolve()
    stem = _safe_output_stem(source.stem)
    html_path = destination / f"{stem}_autonomous.html"
    report_path = destination / f"{stem}_report.json"
    if not overwrite:
        existing = [path.name for path in (html_path, report_path) if path.exists()]
        if existing:
            raise ConversionError("Результат уже существует: " + ", ".join(existing) + ". Используйте --overwrite.")

    with tempfile.TemporaryDirectory(prefix="lesson_collector_") as temporary_name:
        resource_roots, resource_mode = _prepare_resources(source, Path(temporary_name))
        selected, missing_iframes = _discover_documents(source, resource_roots)

        root_copy = copy.deepcopy(selected.root)
        root_has_title = any(
            _normalize_text(element.text_content()) == selected.title for element in root_copy.xpath(".//h1")
        )
        add_title = not root_has_title
        source_text = _baseline_text(selected.root, selected.title, add_title)

        image_records = _process_images(root_copy, selected.path, resource_roots)
        unsupported: list[dict[str, Any]] = []
        _sanitize_tree(root_copy, unsupported)
        output_document = _build_document(selected.title, root_copy, add_title)
        serialized = _serialize(output_document)
        parsed_output = html.document_fromstring(serialized)

        output_text = _output_comparable_text(parsed_output)
        text_match = source_text == output_text
        image_audit = _audit_embedded_images(parsed_output, image_records)
        dependency_audit = _audit_dependencies(parsed_output)
        other_sections = _other_section_references(selected)

        missing_images = [record for record in image_records if record["status"] == "missing"]
        unsupported_images = [record for record in image_records if record["status"] == "unsupported"]
        missing_resources = [*missing_iframes, *missing_images, *unsupported_images]
        warnings: list[str] = []
        if missing_images:
            warnings.append(f"Отсутствуют учебные изображения: {len(missing_images)}.")
        if unsupported_images:
            warnings.append(f"Не удалось безопасно встроить изображения: {len(unsupported_images)}.")
        if missing_iframes:
            warnings.append(f"Не найдены локальные iframe: {len(missing_iframes)}.")
        significant_unsupported = [item for item in unsupported if item.get("importance") == "significant"]
        if significant_unsupported:
            warnings.append(f"Обнаружены неподдерживаемые интерактивные элементы: {len(significant_unsupported)}.")
        if other_sections:
            warnings.append("В сохранении есть только переходы к другим разделам: " + ", ".join(other_sections))
        if not text_match:
            warnings.append("Проверка сохранности нормализованного текста не пройдена.")
        if not image_audit["passed"]:
            warnings.append("Проверка байтов встроенных изображений не пройдена.")
        if not dependency_audit["passed"]:
            warnings.append("В результате найдены внешние или локальные зависимости.")

        status = "success" if not warnings else "completed_with_warnings"
        report: dict[str, Any] = {
            "format_version": 1,
            "status": status,
            "section_title": selected.title,
            "source_file": source.name,
            "content_source": "local_iframe" if selected.path != source else "source_document",
            "resource_source": resource_mode,
            "scope": "Один сохранённый раздел; не является полной копией урока или курса.",
            "text_integrity": {
                "passed": text_match,
                "normalization": "Unicode NFC; последовательности пробельных символов заменены одним пробелом.",
                "source_characters": len(source_text),
                "output_characters": len(output_text),
                "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "output_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
                "explicit_exclusions": [
                    "скрипты, стили, служебные meta/link/base/noscript/template",
                    "поля input и их значения",
                    "добавленные отметки об отсутствующих/неподдерживаемых элементах",
                    "добавленные текстовые адреса деактивированных ссылок",
                ],
            },
            "images": {
                "found": len(image_records),
                "embedded": sum(item["status"] == "embedded" for item in image_records),
                "missing": len(missing_images),
                "unsupported": len(unsupported_images),
                "byte_integrity": image_audit,
                "items": image_records,
            },
            "missing_resources": missing_resources,
            "unsupported_elements": unsupported,
            "static_conversion": {
                "accordion_panels_made_visible": len(selected.root.xpath(".//*[starts-with(@id, 'accordion-panel-')]")),
                "original_styles_replaced_with_reading_and_print_styles": True,
                "original_scripts_removed": True,
                "system_fonts_used": True,
            },
            "external_dependencies": dependency_audit,
            "other_section_references_without_saved_content": other_sections,
            "warnings": warnings,
            "privacy": {"outer_platform_session_data_copied": False, "scripts_and_forms_removed": True},
        }

        report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _write_atomic(html_path, serialized)
        _write_atomic(report_path, report_bytes)

    return ConversionResult(html_path=html_path, report_path=report_path, report=report)
