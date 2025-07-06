# --- Файл: manage_templates_dialog.py ---

import logging
from typing import Dict, Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QTextEdit, QPushButton,
    QDialogButtonBox, QMessageBox, QInputDialog, QListWidgetItem, QLabel, QWidget,
    QLineEdit
)
from PySide6.QtCore import Qt, Slot, QTimer

logger = logging.getLogger(__name__)

class ManageTemplatesDialog(QDialog):
    """
    Диалоговое окно для управления шаблонами системных инструкций.
    Позволяет добавлять, удалять, переименовывать и редактировать шаблоны.
    """

    def __init__(self, templates: Dict[str, str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Управление шаблонами инструкций"))
        self.setMinimumSize(500, 400)

        self.current_templates = templates.copy()
        self._selected_template_name: Optional[str] = None

        main_layout = QVBoxLayout(self)

        top_layout = QHBoxLayout()
        list_layout = QVBoxLayout()
        list_label = QLabel(self.tr("Шаблоны:"))
        self.templates_list_widget = QListWidget()
        self.templates_list_widget.setSortingEnabled(True)
        list_layout.addWidget(list_label)
        list_layout.addWidget(self.templates_list_widget)

        button_layout = QVBoxLayout()
        self.add_button = QPushButton(self.tr("Добавить..."))
        self.rename_button = QPushButton(self.tr("Переименовать..."))
        self.remove_button = QPushButton(self.tr("Удалить"))
        button_layout.addWidget(self.add_button)
        button_layout.addWidget(self.rename_button)
        button_layout.addWidget(self.remove_button)
        button_layout.addStretch(1)

        top_layout.addLayout(list_layout, 2)
        top_layout.addLayout(button_layout, 1)
        main_layout.addLayout(top_layout)

        text_label = QLabel(self.tr("Текст выбранного шаблона:"))
        self.template_text_edit = QTextEdit()
        main_layout.addWidget(text_label)
        main_layout.addWidget(self.template_text_edit, 1)

        self.save_changes_button = QPushButton(self.tr("Сохранить изменения в тексте"))
        self.save_changes_button.setToolTip(self.tr("Сохранить текст для выбранного в списке шаблона"))
        main_layout.addWidget(self.save_changes_button)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        main_layout.addWidget(self.button_box)

        self._populate_list()

        self.templates_list_widget.currentItemChanged.connect(self._on_template_selected)
        self.add_button.clicked.connect(self._add_template)
        self.rename_button.clicked.connect(self._rename_template)
        self.remove_button.clicked.connect(self._remove_template)
        self.save_changes_button.clicked.connect(self._save_template_changes)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        self._update_buttons_state()

    def _populate_list(self):
        """Заполняет QListWidget именами шаблонов из словаря."""
        self.templates_list_widget.blockSignals(True)
        self.templates_list_widget.clear()
        for name in sorted(self.current_templates.keys()):
            self.templates_list_widget.addItem(name)
        self.templates_list_widget.blockSignals(False)

    def _update_buttons_state(self):
        """Обновляет доступность кнопок управления."""
        item_selected = self.templates_list_widget.currentItem() is not None
        self.rename_button.setEnabled(item_selected)
        self.remove_button.setEnabled(item_selected)
        self.save_changes_button.setEnabled(item_selected)

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_template_selected(self, current_item: Optional[QListWidgetItem], previous_item: Optional[QListWidgetItem]):
        """Отображает текст выбранного шаблона в QTextEdit."""
        self.template_text_edit.clear()
        self._selected_template_name = None
        if current_item:
            template_name = current_item.text()
            self._selected_template_name = template_name
            template_content = self.current_templates.get(template_name, "")
            self.template_text_edit.setPlainText(template_content)
        self._update_buttons_state()

    @Slot()
    def _add_template(self):
        """Добавляет новый шаблон."""
        template_name, ok = QInputDialog.getText(self, self.tr("Добавить шаблон"), self.tr("Введите имя нового шаблона:"))
        if ok and template_name:
            template_name = template_name.strip()
            if not template_name:
                QMessageBox.warning(self, self.tr("Ошибка"), self.tr("Имя шаблона не может быть пустым."))
                return
            if template_name in self.current_templates:
                QMessageBox.warning(self, self.tr("Ошибка"), self.tr("Шаблон с именем '{0}' уже существует.").format(template_name))
                return

            self.current_templates[template_name] = ""
            self._populate_list()
            
            items = self.templates_list_widget.findItems(template_name, Qt.MatchFlag.MatchExactly)
            if items:
                self.templates_list_widget.setCurrentItem(items[0])
                self.template_text_edit.setFocus()
            logger.info(f"Добавлен новый шаблон: '{template_name}'")
        else:
            logger.debug("Добавление шаблона отменено пользователем.")

    @Slot()
    def _rename_template(self):
        """Переименовывает выбранный шаблон."""
        if not self._selected_template_name:
            return

        old_name = self._selected_template_name
        new_name, ok = QInputDialog.getText(self, self.tr("Переименовать шаблон"), self.tr("Введите новое имя для '{0}':").format(old_name), QLineEdit.EchoMode.Normal, old_name)

        if ok and new_name:
            new_name = new_name.strip()
            if not new_name:
                QMessageBox.warning(self, self.tr("Ошибка"), self.tr("Имя шаблона не может быть пустым."))
                return
            if new_name == old_name:
                return
            if new_name in self.current_templates:
                QMessageBox.warning(self, self.tr("Ошибка"), self.tr("Шаблон с именем '{0}' уже существует.").format(new_name))
                return

            template_content = self.current_templates.pop(old_name)
            self.current_templates[new_name] = template_content

            self._populate_list()
            items = self.templates_list_widget.findItems(new_name, Qt.MatchFlag.MatchExactly)
            if items:
                self.templates_list_widget.setCurrentItem(items[0])
            logger.info(f"Шаблон '{old_name}' переименован в '{new_name}'")
        else:
            logger.debug("Переименование шаблона отменено пользователем.")

    @Slot()
    def _remove_template(self):
        """Удаляет выбранный шаблон."""
        if not self._selected_template_name:
            return

        template_name_to_remove = self._selected_template_name
        reply = QMessageBox.question(self, self.tr("Удалить шаблон?"),
                                     self.tr("Вы уверены, что хотите удалить шаблон '{0}'?").format(template_name_to_remove),
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            if template_name_to_remove in self.current_templates:
                del self.current_templates[template_name_to_remove]
                self._populate_list()
                self.template_text_edit.clear()
                self._selected_template_name = None
                self._update_buttons_state()
                logger.info(f"Удален шаблон: '{template_name_to_remove}'")
            else:
                logger.warning(f"Попытка удалить несуществующий шаблон: '{template_name_to_remove}'")
        else:
            logger.debug("Удаление шаблона отменено пользователем.")

    @Slot()
    def _save_template_changes(self):
        """Сохраняет текст из QTextEdit в выбранный шаблон в словаре."""
        if not self._selected_template_name:
            QMessageBox.warning(self, self.tr("Ошибка"), self.tr("Нет выбранного шаблона для сохранения текста."))
            return

        current_text = self.template_text_edit.toPlainText()
        self.current_templates[self._selected_template_name] = current_text
        logger.info(f"Текст шаблона '{self._selected_template_name}' обновлен (временно, в диалоге).")
        
        self.save_changes_button.setText(self.tr("Изменения сохранены!"))
        QTimer.singleShot(1500, lambda: self.save_changes_button.setText(self.tr("Сохранить изменения в тексте")))

    def get_updated_templates(self) -> Dict[str, str]:
        """Возвращает обновленный словарь шаблонов."""
        return self.current_templates