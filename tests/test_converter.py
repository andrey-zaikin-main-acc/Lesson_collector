from __future__ import annotations

import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import html

from lesson_converter import ConversionError, convert_lesson


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def write_fixture(root: Path, *, image_name: str, write_image: bool = True) -> Path:
    source = root / "Кириллица урок.html"
    resources = root / "Кириллица урок_files"
    resources.mkdir()
    source.write_text(
        '<!doctype html><html><body><iframe src="./Кириллица%20урок_files/content.html"></iframe></body></html>',
        encoding="utf-8",
    )
    (resources / "content.html").write_text(
        "<!doctype html><html><body><main><h1>Тестовый раздел</h1>"
        f'<p>Начало и конец текста.</p><img src="{image_name}" alt="Схема">'
        "</main></body></html>",
        encoding="utf-8",
    )
    if write_image:
        (resources / image_name).write_bytes(PNG_1X1)
    return source


class ConverterTests(unittest.TestCase):
    def test_extensionless_image_and_cyrillic_paths(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = write_fixture(root, image_name="картинка без расширения")
            result = convert_lesson(source, root / "результат")
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["images"]["found"], 1)
            self.assertEqual(report["images"]["embedded"], 1)
            self.assertTrue(report["images"]["byte_integrity"]["passed"])
            self.assertTrue(report["images"]["items"][0]["extension_was_missing"])
            document = html.document_fromstring(result.html_path.read_bytes())
            uri = document.xpath("//img/@src")[0]
            self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), PNG_1X1)

    def test_missing_image_is_reported_and_marked_in_html(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = write_fixture(root, image_name="missing.png", write_image=False)
            result = convert_lesson(source, root / "out")
            self.assertEqual(result.report["status"], "completed_with_warnings")
            self.assertEqual(result.report["images"]["missing"], 1)
            output = result.html_path.read_text(encoding="utf-8")
            self.assertIn("Изображение отсутствует", output)
            self.assertIn("Схема", output)

    def test_unsafe_zip_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "lesson.html"
            source.write_text('<iframe src="lesson_files/content.html"></iframe>', encoding="utf-8")
            archive = root / "lesson_files.zip"
            with zipfile.ZipFile(archive, "w") as file:
                file.writestr("../escape.txt", "bad")
                file.writestr("lesson_files/content.html", "<main><h1>Test</h1></main>")
            with self.assertRaisesRegex(ConversionError, "Небезопасный путь"):
                convert_lesson(source, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_existing_result_requires_overwrite_flag(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = write_fixture(root, image_name="image.png")
            output = root / "out"
            convert_lesson(source, output)
            with self.assertRaisesRegex(ConversionError, "--overwrite"):
                convert_lesson(source, output)
            convert_lesson(source, output, overwrite=True)

    def test_saved_accordion_is_visible_without_decorative_controls(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "accordion.html"
            source.write_text(
                "<!doctype html><meta charset=utf-8><main><h1>Раздел</h1>"
                '<button aria-controls="accordion-panel-one"><h3>Подробнее</h3><svg><path d="M0 0h1v1z"/></svg></button>'
                '<div id="accordion-panel-one" hidden style="display:none"><p>Скрытый учебный текст</p></div>'
                '<button aria-label="Увеличить"><svg><path d="M0 0h1v1z"/></svg></button>'
                "</main>",
                encoding="utf-8",
            )
            result = convert_lesson(source, root / "out")
            output = result.html_path.read_text(encoding="utf-8")
            self.assertIn("Подробнее", output)
            self.assertIn("Скрытый учебный текст", output)
            self.assertNotIn("<button", output)
            self.assertNotIn("<svg", output)
            self.assertNotIn("display:none", output)
            self.assertNotIn("aria-label", output)
            self.assertEqual(result.report["static_conversion"]["accordion_panels_made_visible"], 1)

    def test_svg_with_external_dependency_is_not_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = write_fixture(root, image_name="unsafe.svg", write_image=False)
            resources = root / "Кириллица урок_files"
            (resources / "unsafe.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
                '<image href="https://example.invalid/tracker.png"/></svg>',
                encoding="utf-8",
            )
            result = convert_lesson(source, root / "out")
            output = result.html_path.read_text(encoding="utf-8")
            self.assertEqual(result.report["images"]["unsupported"], 1)
            self.assertIn("SVG содержит небезопасные", output)
            self.assertNotIn("example.invalid", output)
            self.assertTrue(result.report["external_dependencies"]["passed"])


if __name__ == "__main__":
    unittest.main()
