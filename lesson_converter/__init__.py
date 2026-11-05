"""Преобразование сохранённых учебных страниц в автономный HTML."""

from .converter import ConversionError, ConversionResult, convert_lesson

__all__ = ["ConversionError", "ConversionResult", "convert_lesson"]
