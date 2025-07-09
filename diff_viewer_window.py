# --- Файл: diff_viewer_window.py ---

import logging
import difflib
from typing import Optional

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import Qt

logger = logging.getLogger(__name__)

class DiffViewerWindow(QWidget):
    """
    Немодальное окно для отображения различий между двумя версиями кода.
    Использует difflib для генерации HTML-представления.
    """

    def __init__(self, original_code: str, new_code: str, file_path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.original_code = original_code
        self.new_code = new_code
        self.file_path = file_path

        self.setWindowTitle(self.tr("Сравнение версий файла: {0}").format(file_path))
        self.setMinimumSize(900, 600)
        self.setWindowFlag(Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._init_ui()
        self._generate_and_set_diff_html()

    def _init_ui(self):
        """Инициализирует виджеты окна."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.web_view = QWebEngineView()
        layout.addWidget(self.web_view)

    def _generate_and_set_diff_html(self):
        """Генерирует HTML для сравнения и устанавливает его в QWebEngineView."""
        logger.debug(f"Генерация diff для файла: {self.file_path}")

        from_lines = self.original_code.splitlines()
        to_lines = self.new_code.splitlines()

        from_desc = self.tr("Оригинал: {0}").format(self.file_path)
        to_desc = self.tr("Предложенные изменения")

        # Используем HtmlDiff для создания HTML-таблицы сравнения
        differ = difflib.HtmlDiff(wrapcolumn=85, tabsize=4)
        html_diff = differ.make_file(from_lines, to_lines, from_desc, to_desc)

        # Инъекция стилей для темной темы, чтобы соответствовать приложению
        dark_theme_css = """
        <style type="text/css">
            body { font-family: sans-serif; background-color: #2b2b2b; color: #d0d0d0; }
            table.diff { font-family: Consolas, 'Courier New', monospace; font-size: 13px; border-collapse: collapse; border: 1px solid #555; width: 100%; }
            .diff_header { background-color: #444; color: #ccc; }
            td { padding: 1px 3px; white-space: pre-wrap; word-wrap: break-word; } /* <-- Добавлены стили для переноса */
            .diff_next { background-color: #3a3a3a; color: #888; white-space: nowrap; } /* <-- Отключаем перенос для номеров строк */
            .diff_add { background-color: #1a4d1a; }
            .diff_chg { background-color: #5d4d1b; }
            .diff_sub { background-color: #6d1e1e; }
            a { color: #66d9ef; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
        """
        html_with_style = html_diff.replace('</head>', f'{dark_theme_css}</head>')

        self.web_view.setHtml(html_with_style)
        logger.debug("HTML для diff успешно сгенерирован и установлен.")