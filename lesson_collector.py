#!/usr/bin/env python3
"""Командная строка и оконный запуск Lesson Collector."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lesson_converter import ConversionError, convert_lesson


def _choose_paths() -> tuple[Path, Path] | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover - зависит от сборки Python
        raise ConversionError(
            "В этой сборке Python нет tkinter. Укажите исходный HTML в командной строке."
        ) from exc

    root = tk.Tk()
    root.withdraw()
    root.update()
    source = filedialog.askopenfilename(
        parent=root,
        title="Выберите сохранённую страницу курса",
        filetypes=[("HTML-файлы", "*.html *.htm"), ("Все файлы", "*.*")],
    )
    if not source:
        root.destroy()
        return None

    default_dir = Path(source).parent / "converted"
    output = filedialog.askdirectory(
        parent=root,
        title="Выберите папку для результата",
        initialdir=str(default_dir if default_dir.exists() else default_dir.parent),
        mustexist=False,
    )
    root.destroy()
    return Path(source), Path(output) if output else default_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Преобразует сохранённый раздел курса и папку/архив _files "
            "в автономный HTML и JSON-отчёт."
        )
    )
    parser.add_argument(
        "input_html",
        nargs="?",
        type=Path,
        help="исходный сохранённый .html/.htm; без аргумента откроется окно выбора",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="папка результата (по умолчанию: converted рядом с исходным HTML)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="явно разрешить замену существующих файлов результата",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.input_html is None:
            chosen = _choose_paths()
            if chosen is None:
                print("Выбор отменён.")
                return 0
            source, output_dir = chosen
        else:
            source = args.input_html
            output_dir = args.output_dir or source.parent / "converted"

        result = convert_lesson(source, output_dir, overwrite=args.overwrite)
    except ConversionError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Ошибка файловой системы: {exc}", file=sys.stderr)
        return 3

    print("Готово.")
    print(f"HTML: {result.html_path}")
    print(f"Отчёт: {result.report_path}")
    status = result.report.get("status", "unknown")
    print(f"Статус: {status}")
    warnings = result.report.get("warnings", [])
    if warnings:
        print("Предупреждения:")
        for warning in warnings:
            print(f"  - {warning}")
    return 0 if status in {"success", "completed_with_warnings"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
