# --- Файл: summaries_window.py ---

import logging
from typing import Dict, Optional

from PySide6.QtCore import Qt, Slot, Signal, QModelIndex
from PySide6.QtGui import QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListView,
    QTextEdit, QSplitter, QLineEdit
)

logger = logging.getLogger(__name__)

class SummariesWindow(QWidget):
    """
    Немодальное окно для отображения списка проанализированных файлов
    и их саммари.
    """
    # Сигнал, который испускается, когда пользователь закрывает окно
    windowClosed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Проанализированные файлы"))
        self.setMinimumSize(600, 400)
        # Устанавливаем флаг, чтобы окно имело собственную иконку на панели задач
        self.setWindowFlag(Qt.WindowType.Window)

        self._summaries_data: Dict[str, str] = {}
        
        # --- UI Elements ---
        main_layout = QVBoxLayout(self)
        
        # Поиск
        search_layout = QHBoxLayout()
        search_label = QLabel(self.tr("Поиск файла:"))
        self.search_line_edit = QLineEdit()
        self.search_line_edit.setPlaceholderText(self.tr("Введите часть имени файла..."))
        search_layout.addWidget(search_label)
        search_layout.addWidget(self.search_line_edit)
        main_layout.addLayout(search_layout)

        # Разделитель для списка и текстового поля
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)

        # Левая панель (список файлов)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0,0,0,0)
        self.file_list_view = QListView()
        self.list_model = QStandardItemModel(self.file_list_view)
        self.file_list_view.setModel(self.list_model)
        left_layout.addWidget(self.file_list_view)
        
        # Правая панель (просмотр саммари)
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0,0,0,0)
        self.summary_text_edit = QTextEdit()
        self.summary_text_edit.setReadOnly(True)
        self.summary_text_edit.setPlaceholderText(self.tr("Выберите файл в списке слева, чтобы увидеть его саммари."))
        right_layout.addWidget(self.summary_text_edit)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1) # левая часть
        splitter.setStretchFactor(1, 2) # правая часть

        # --- Connections ---
        self.file_list_view.clicked.connect(self._on_file_selected)
        self.search_line_edit.textChanged.connect(self._filter_list)

    @Slot(dict)
    def update_summaries(self, summaries: Dict[str, str]):
        """
        Публичный слот для обновления данных из ViewModel.
        Принимает полный словарь саммари.
        """
        logger.debug(f"SummariesWindow: получено обновление, {len(summaries)} саммари.")
        self._summaries_data = summaries
        
        # Обновляем список, учитывая текущий фильтр поиска
        self._filter_list(self.search_line_edit.text())

        # Если выбранный элемент исчез после обновления, очищаем поле
        if self.file_list_view.selectionModel().hasSelection():
            selected_index = self.file_list_view.selectionModel().currentIndex()
            file_path = self.list_model.data(selected_index, Qt.ItemDataRole.DisplayRole)
            if file_path not in self._summaries_data:
                self.summary_text_edit.clear()
        else:
            self.summary_text_edit.clear()

    @Slot(str)
    def _filter_list(self, query: str):
        """Фильтрует отображаемый список файлов на основе строки поиска."""
        self.list_model.clear()
        query = query.lower()
        
        # Сортируем ключи словаря для последовательного отображения
        sorted_paths = sorted(self._summaries_data.keys())

        for path in sorted_paths:
            if query in path.lower():
                item = QStandardItem(path)
                item.setEditable(False)
                # Сохраняем оригинальный путь в пользовательских данных элемента
                item.setData(path, Qt.ItemDataRole.UserRole)
                self.list_model.appendRow(item)

    @Slot(QModelIndex)
    def _on_file_selected(self, index: QModelIndex):
        """
        Обработчик клика по элементу в списке.
        Отображает саммари для выбранного файла.
        """
        if not index.isValid():
            return
        
        # Получаем путь к файлу из данных элемента
        file_path = self.list_model.data(index, Qt.ItemDataRole.UserRole)
        
        if file_path and file_path in self._summaries_data:
            summary_text = self._summaries_data[file_path]
            self.summary_text_edit.setPlainText(summary_text)
        else:
            logger.warning(f"Саммари для '{file_path}' не найдено в словаре.")
            self.summary_text_edit.clear()

    def closeEvent(self, event):
        """
        Переопределяем событие закрытия окна.
        Вместо удаления окно просто скрывается, и испускается сигнал.
        """
        logger.debug("SummariesWindow: Окно скрыто, испускается сигнал windowClosed.")
        self.windowClosed.emit()
        self.hide()
        event.ignore() # Игнорируем стандартное событие закрытия