# --- Файл: main.py ---

import sys
import os
import html
import json
import markdown
import logging
import logging.handlers
import datetime
from typing import Optional, Dict, Set

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QLineEdit, QFileDialog,
    QSizePolicy, QSpinBox, QMessageBox, QStatusBar, QGroupBox,
    QCheckBox, QDialog, QComboBox, QInputDialog, QStyle
)
from PySide6.QtCore import (
    Qt, Slot, QUrl, QTimer, QCoreApplication, QFileInfo, QTranslator, QLocale
)
from PySide6.QtGui import QAction, QKeySequence, QIcon, QFont, QActionGroup

try:
    from dotenv import load_dotenv, set_key
except ImportError:
    print("КРИТИЧЕСКАЯ ОШИБКА: Модуль python-dotenv не найден! Установите его: pip install python-dotenv")
    sys.exit(1)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:
    # Логирование здесь еще не настроено, поэтому используем print
    print("КРИТИЧЕСКАЯ ОШИБКА: Модуль QtWebEngineWidgets не найден! Установите его: pip install PySide6-WebEngine")
    sys.exit(1)

# --- Импортируем модели и менеджеры ---
from chat_model import ChatModel
from chat_view import ChatView
from chat_viewmodel import ChatViewModel
from summaries_window import SummariesWindow
from log_viewer_window import LogViewerWindow
import db_manager

try:
    from manage_templates_dialog import ManageTemplatesDialog
except ImportError:
    ManageTemplatesDialog = None
    print("--- MainWindow: Предупреждение: manage_templates_dialog.py не найден. Управление шаблонами будет недоступно. ---")

# --- Глобальные константы ---
APP_ICON_FILENAME = "app_icon.png"
TEMPLATES_FILENAME = "instruction_templates.json"
# Удаляем строковые константы отсюда, т.к. их нужно будет переводить через self.tr()
COMMON_EXTENSIONS = [".py", ".txt", ".md", ".json", ".html", ".css", ".js", ".yaml", ".yml", ".pdf", ".docx"]

# --- Диалог справки ---
class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Справка - GitGemini Pro"))
        self.setMinimumSize(700, 500)
        layout = QVBoxLayout(self)
        self.help_view = QWebEngineView()
        layout.addWidget(self.help_view, 1)
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Пытаемся загрузить локализованный файл справки
        current_app_lang = os.getenv('APP_LANGUAGE', QLocale.system().name().split('_')[0])
        help_file = f"help_content_{current_app_lang}.html"
        html_file_path = os.path.join(script_dir, help_file)

        if not os.path.exists(html_file_path):
             # Если локализованного файла нет, используем дефолтный
             html_file_path = os.path.join(script_dir, "help_content.html")

        if os.path.exists(html_file_path):
            local_url = QUrl.fromLocalFile(QFileInfo(html_file_path).absoluteFilePath())
            self.help_view.load(local_url)
        else:
            self.help_view.setHtml(self.tr("<html><body><h1>Ошибка</h1><p>Файл справки 'help_content.html' не найден.</p></body></html>"))
        
        close_button = QPushButton(self.tr("Закрыть"))
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        self.setLayout(layout)

# --- Основное окно приложения ---
class MainWindow(QMainWindow):
    def __init__(self, view_model: ChatViewModel, log_file_path: str, parent: Optional[QWidget] = None): # Добавляем log_file_path
        super().__init__(parent)
        if not isinstance(view_model, ChatViewModel):
            raise TypeError("ViewModel required")
        self.view_model = view_model
        self.logger = logging.getLogger(__name__)

        self._log_file_path = log_file_path # Сохраняем путь к лог-файлу
        self._log_viewer_window: Optional[LogViewerWindow] = None # Инициализируем окно просмотра логов
        self.summaries_window: Optional[SummariesWindow] = None

        self._dotenv_path = self._get_resource_path('.env')
        # Убедимся, что файл .env существует
        if not os.path.exists(self._dotenv_path):
            try:
                with open(self._dotenv_path, 'w') as f:
                    pass # Просто создаем пустой файл
            except OSError as e:
                self.logger.error(f"Не удалось создать файл .env: {e}")
                # Это некритично для запуска, но сохранение языка не будет работать
        
        self._templates_file_path = self._get_resource_path(TEMPLATES_FILENAME)
        self.instruction_templates: Dict[str, str] = {}
        self._load_instruction_templates()

        self.token_status_label = QLabel(self.tr("Токены: ..."))
        self.token_status_label.setStyleSheet("padding-right: 10px;")
        self._status_clear_timer = QTimer(self)
        self._status_clear_timer.setSingleShot(True)
        self._status_clear_timer.timeout.connect(self._clear_temporary_status_message)

        self._init_ui()
        self._populate_templates_combobox()
        self._create_menu()
        self._connect_signals()

        self.view_model.windowTitleChanged.emit()
        self.view_model.geminiApiKeyStatusTextChanged.emit()
        self.view_model.githubTokenStatusTextChanged.emit()
        self._update_all_states_from_vm()

    def _get_resource_path(self, filename: str) -> str:
        """Получает правильный путь к ресурсу, будь то скрипт или скомпилированное приложение."""
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_path, filename)

    def _init_ui(self):
        # Определяем переводимые константы здесь, чтобы иметь доступ к self.tr()
        self.CUSTOM_INSTRUCTIONS_TEXT = self.tr("(Пользовательские инструкции)")
        self.SAVE_AS_TEMPLATE_TEXT = self.tr("Сохранить текущие как шаблон...")

        self.setWindowTitle(self.tr("GitGemini Pro"))
        self.setGeometry(100, 100, 950, 800)
        
        icon_path = self._get_resource_path(APP_ICON_FILENAME)
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- Блок URL Репозитория и Ветки ---
        repo_layout = QHBoxLayout()
        repo_url_label = QLabel(self.tr("URL Репозитория:"))
        self.repo_url_lineedit = QLineEdit()
        self.repo_url_lineedit.setPlaceholderText(self.tr("https://github.com/user/repository"))
        
        branch_label = QLabel(self.tr("Ветка:"))
        self.branch_combobox = QComboBox()
        self.branch_combobox.setToolTip(self.tr("Выберите ветку для анализа"))
        self.branch_combobox.setEnabled(False)
        self.branch_combobox.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

        repo_layout.addWidget(repo_url_label)
        repo_layout.addWidget(self.repo_url_lineedit, 3)
        repo_layout.addWidget(branch_label)
        repo_layout.addWidget(self.branch_combobox, 1)
        main_layout.addLayout(repo_layout)

        analysis_layout = QHBoxLayout()
        self.analyze_repo_button = QPushButton(self.tr("Анализировать репозиторий"))
        self.cancel_analysis_button = QPushButton(self.tr("Отмена анализа"))

        self.view_summaries_button = QPushButton("👁️")
        self.view_summaries_button.setToolTip(self.tr("Просмотреть проанализированные файлы и их саммари"))
        self.view_summaries_button.setFixedSize(32, 32)
        font = self.view_summaries_button.font()
        font.setPointSize(14)
        self.view_summaries_button.setFont(font)
        self.view_summaries_button.setStyleSheet("QPushButton { border: 1px solid #555; border-radius: 4px; } QPushButton:hover { background-color: #444; }")

        analysis_layout.addWidget(self.analyze_repo_button, 1)
        analysis_layout.addWidget(self.cancel_analysis_button, 1)
        analysis_layout.addSpacing(20)
        analysis_layout.addWidget(self.view_summaries_button)
        analysis_layout.addStretch(0)
        main_layout.addLayout(analysis_layout)

        # --- Блок настроек ---
        self.toggle_settings_button = QPushButton(self.tr("Развернуть настройки ▼"))
        main_layout.addWidget(self.toggle_settings_button)

        self.settings_group_box = QGroupBox(self.tr("Настройки"))
        settings_inner_layout = QVBoxLayout(self.settings_group_box)
        
        # Gemini API Key
        api_key_layout = QHBoxLayout()
        self.api_key_status_label = QLabel(self.tr("Ключ API:"))
        self.api_key_lineedit = QLineEdit()
        self.api_key_lineedit.setPlaceholderText(self.tr("Введите Gemini API ключ..."))
        self.api_key_lineedit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_save_button = QPushButton(self.tr("Сохранить ключ"))
        api_key_layout.addWidget(self.api_key_status_label)
        api_key_layout.addWidget(self.api_key_lineedit, 1)
        api_key_layout.addWidget(self.api_key_save_button)
        settings_inner_layout.addLayout(api_key_layout)

        # GitHub Token
        github_token_layout = QHBoxLayout()
        self.github_token_status_label = QLabel(self.tr("Токен GitHub:"))
        self.github_token_lineedit = QLineEdit()
        self.github_token_lineedit.setPlaceholderText(self.tr("Введите GitHub Personal Access Token..."))
        self.github_token_lineedit.setEchoMode(QLineEdit.EchoMode.Password)
        self.github_token_save_button = QPushButton(self.tr("Сохранить токен"))
        github_token_layout.addWidget(self.github_token_status_label)
        github_token_layout.addWidget(self.github_token_lineedit, 1)
        github_token_layout.addWidget(self.github_token_save_button)
        settings_inner_layout.addLayout(github_token_layout)

        # Model Selection
        model_name_layout = QHBoxLayout()
        model_name_label = QLabel(self.tr("Модель ИИ:"))
        self.model_name_combobox = QComboBox()
        self.model_name_combobox.setEditable(True)
        self.model_name_combobox.setToolTip(self.tr("Выберите модель Gemini или введите имя вручную"))
        model_name_layout.addWidget(model_name_label)
        model_name_layout.addWidget(self.model_name_combobox, 1)
        settings_inner_layout.addLayout(model_name_layout)

        # Max Tokens
        max_tokens_layout = QHBoxLayout()
        max_tokens_label = QLabel(self.tr("Макс. токенов ответа:"))
        self.max_tokens_spinbox = QSpinBox()
        self.max_tokens_spinbox.setRange(256, 131072)
        self.max_tokens_spinbox.setSingleStep(1024)
        self.max_tokens_spinbox.setValue(65536)
        max_tokens_layout.addWidget(max_tokens_label)
        max_tokens_layout.addWidget(self.max_tokens_spinbox)
        max_tokens_layout.addStretch(1)
        settings_inner_layout.addLayout(max_tokens_layout)

        # Extensions
        extensions_group_label = QLabel(self.tr("Расширения файлов для анализа:"))
        settings_inner_layout.addWidget(extensions_group_label)
        checkbox_layout = QHBoxLayout()
        self.common_ext_checkboxes = {ext: QCheckBox(ext) for ext in COMMON_EXTENSIONS}
        for cb in self.common_ext_checkboxes.values():
            checkbox_layout.addWidget(cb)
        checkbox_layout.addStretch(1)
        settings_inner_layout.addLayout(checkbox_layout)
        custom_ext_layout = QHBoxLayout()
        custom_ext_label = QLabel(self.tr("Другие:"))
        self.custom_ext_lineedit = QLineEdit()
        self.custom_ext_lineedit.setPlaceholderText(self.tr(".log .csv .xml ..."))
        custom_ext_layout.addWidget(custom_ext_label)
        custom_ext_layout.addWidget(self.custom_ext_lineedit, 1)
        settings_inner_layout.addLayout(custom_ext_layout)
        
        main_layout.addWidget(self.settings_group_box)
        self.settings_group_box.setVisible(False)

        # --- Блок инструкций (с контейнером) ---
        self.toggle_instructions_button = QPushButton(self.tr("Свернуть инструкции ▲"))
        main_layout.addWidget(self.toggle_instructions_button)

        self.instructions_container = QWidget()
        instructions_container_layout = QVBoxLayout(self.instructions_container)
        instructions_container_layout.setContentsMargins(0, 0, 0, 0)

        self.instructions_textedit = QTextEdit()
        self.instructions_textedit.setPlaceholderText(self.tr("Системные инструкции (опционально)..."))
        self.instructions_textedit.setFixedHeight(80)
        instructions_container_layout.addWidget(self.instructions_textedit)

        templates_layout = QHBoxLayout()
        templates_label = QLabel(self.tr("Шаблон:"))
        self.templates_combobox = QComboBox()
        self.manage_templates_button = QPushButton(self.tr("Управлять..."))
        templates_layout.addWidget(templates_label)
        templates_layout.addWidget(self.templates_combobox, 1)
        templates_layout.addWidget(self.manage_templates_button)
        instructions_container_layout.addLayout(templates_layout)

        main_layout.addWidget(self.instructions_container)

        # --- Блок чата ---
        self.dialog_textedit = ChatView(self.view_model, self)
        self.input_textedit = QTextEdit()
        self.input_textedit.setPlaceholderText(self.tr("Введите запрос (Enter для новой строки, Ctrl+Enter для отправки)..."))
        self.input_textedit.setFixedHeight(100)
        
        bottom_button_layout = QHBoxLayout()
        self.cancel_button = QPushButton(self.tr("Отмена"))
        self.send_button = QPushButton(self.tr("Отправить Ctrl ↵"))
        bottom_button_layout.addWidget(self.cancel_button)
        bottom_button_layout.addStretch(1)
        bottom_button_layout.addWidget(self.send_button)

        main_layout.addWidget(self.dialog_textedit, 1)
        main_layout.addWidget(self.input_textedit)
        main_layout.addLayout(bottom_button_layout)
        
        # --- Статус-бар ---
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)
        status_bar.addPermanentWidget(self.token_status_label)

        self.input_textedit.installEventFilter(self)

    def _create_menu(self):
        menu_bar = self.menuBar()
        
        # --- File Menu ---
        file_menu = menu_bar.addMenu(self.tr("&Файл"))
        actions = [
            (self.tr("&Новая сессия"), QKeySequence.StandardKey.New, self._new_session),
            (self.tr("&Открыть сессию..."), QKeySequence.StandardKey.Open, self._open_session),
            (self.tr("&Сохранить сессию"), QKeySequence.StandardKey.Save, self.view_model.saveSession),
            (self.tr("Сохранить сессию &как..."), QKeySequence.StandardKey.SaveAs, self.view_model.saveSessionAs),
            None,
            (self.tr("&Выход"), QKeySequence.StandardKey.Quit, self.close)
        ]
        for item in actions:
            if item:
                text, shortcut, slot = item
                action = QAction(text, self)
                action.setShortcut(shortcut)
                action.triggered.connect(slot)
                file_menu.addAction(action)
            else:
                file_menu.addSeparator()
        
        self._create_language_menu()
        self._create_view_menu()

        # --- Help Menu ---
        help_menu = menu_bar.addMenu(self.tr("&Справка"))
        help_content_action = QAction(self.tr("Содержание справки..."), self)
        help_content_action.setShortcut(QKeySequence.StandardKey.HelpContents)
        help_content_action.triggered.connect(self._show_help_content)
        help_menu.addAction(help_content_action)
        help_menu.addSeparator()
        about_action = QAction(self.tr("О программе..."), self)
        about_action.triggered.connect(self._show_about_dialog)
        help_menu.addAction(about_action)

    def _create_view_menu(self):
        """Создает меню для настроек отображения."""
        view_menu = self.menuBar().addMenu(self.tr("&Вид"))
        
        show_logs_action = QAction(self.tr("Показать &Логи"), self)
        show_logs_action.setShortcut(QKeySequence("Ctrl+L")) # Стандартное сочетание для логов
        show_logs_action.triggered.connect(self._show_log_viewer)
        view_menu.addAction(show_logs_action)

    @Slot()
    def _show_log_viewer(self):
        """Создает (если нужно) и показывает окно просмотра логов."""
        if self._log_viewer_window is None:
            self.logger.info("Создание нового экземпляра LogViewerWindow.")
            self._log_viewer_window = LogViewerWindow(self._log_file_path, self)
            # Подключаемся к сигналу уничтожения, чтобы очистить ссылку
            self._log_viewer_window.destroyed.connect(self._on_log_viewer_destroyed)
            self._log_viewer_window.show()
        else:
            # Если окно уже существует, просто активируем его
            self._log_viewer_window.activateWindow()

    @Slot()
    def _on_log_viewer_destroyed(self):
        """Слот, который вызывается при уничтожении окна логов."""
        self.logger.info("Экземпляр LogViewerWindow был уничтожен, очищаем ссылку.")
        self._log_viewer_window = None

    def _create_language_menu(self):
        """Создает меню для выбора языка."""
        lang_menu = self.menuBar().addMenu(self.tr("&Язык"))

        # Группа для взаимоисключающих действий
        lang_group = QActionGroup(self)
        lang_group.setExclusive(True)

        ru_action = QAction(self.tr("Русский"), self)
        ru_action.setCheckable(True)
        ru_action.triggered.connect(lambda: self._switch_language('ru'))
        lang_menu.addAction(ru_action)
        lang_group.addAction(ru_action)

        en_action = QAction(self.tr("English"), self)
        en_action.setCheckable(True)
        en_action.triggered.connect(lambda: self._switch_language('en'))
        lang_menu.addAction(en_action)
        lang_group.addAction(en_action)

        # Устанавливаем текущий язык на основе загруженной настройки
        # (os.getenv вернет None если переменная не найдена)
        current_lang = os.getenv('APP_LANGUAGE', QLocale.system().name().split('_')[0])
        if current_lang == 'ru':
            ru_action.setChecked(True)
        else:
            en_action.setChecked(True)

    @Slot(str)
    def _switch_language(self, lang_code: str):
        """Слот для переключения языка и сохранения выбора в .env."""
        try:
            # Используем set_key для сохранения настройки в .env
            set_key(self._dotenv_path, "APP_LANGUAGE", lang_code)
            self.logger.info(f"Язык приложения установлен на '{lang_code}' и сохранен в {self._dotenv_path}")
            
            QMessageBox.information(
                self,
                self.tr("Смена языка"),
                self.tr("Язык будет изменен после перезапуска приложения.")
            )
        except Exception as e:
            self.logger.error(f"Не удалось сохранить настройку языка в файл .env: {e}", exc_info=True)
            QMessageBox.critical(
                self,
                self.tr("Ошибка сохранения"),
                self.tr("Не удалось сохранить настройку языка. Убедитесь, что у приложения есть права на запись в файл .env.")
            )

    def _connect_signals(self):
        # Команды от пользователя
        self.repo_url_lineedit.editingFinished.connect(
            lambda: self.view_model.updateRepoUrl(self.repo_url_lineedit.text())
        )
        self.branch_combobox.currentTextChanged.connect(self.view_model.updateSelectedBranch)

        self.api_key_save_button.clicked.connect(lambda: self.view_model.saveGeminiApiKey(self.api_key_lineedit.text()))
        self.github_token_save_button.clicked.connect(lambda: self.view_model.saveGithubToken(self.github_token_lineedit.text()))
        self.analyze_repo_button.clicked.connect(self.view_model.startAnalysis)
        self.cancel_analysis_button.clicked.connect(self.view_model.cancelAnalysis)
        self.send_button.clicked.connect(lambda: self.view_model.sendMessage(self.input_textedit.toPlainText().strip()))
        self.cancel_button.clicked.connect(self.view_model.cancelRequest)
        self.view_model.apiRequestStarted.connect(self.input_textedit.clear)

        # Настройки
        self.model_name_combobox.currentTextChanged.connect(self.view_model.updateModelName)
        self.max_tokens_spinbox.valueChanged.connect(self.view_model.updateMaxTokens)
        self.instructions_textedit.textChanged.connect(self._on_instructions_changed)
        for checkbox in self.common_ext_checkboxes.values():
            checkbox.stateChanged.connect(self._on_extensions_changed)
        self.custom_ext_lineedit.editingFinished.connect(self._on_extensions_changed)

        # Шаблоны
        self.templates_combobox.currentIndexChanged.connect(self._on_template_selected)
        self.manage_templates_button.clicked.connect(self._open_manage_templates_dialog)

        # Переключатели видимости
        self.toggle_settings_button.clicked.connect(self.view_model.toggleSettings)
        self.toggle_instructions_button.clicked.connect(self.view_model.toggleInstructions)

        # Сигналы от ViewModel к UI
        self.view_model.windowTitleChanged.connect(self._update_window_title)
        self.view_model.geminiApiKeyStatusTextChanged.connect(self._update_gemini_api_key_status)
        self.view_model.githubTokenStatusTextChanged.connect(self._update_github_token_status)
        self.view_model.clearApiKeyInput.connect(self.api_key_lineedit.clear)
        self.view_model.clearTokenInput.connect(self.github_token_lineedit.clear)
        
        self.view_model.canSendChanged.connect(self._update_button_states)
        self.view_model.canCancelRequestChanged.connect(self._update_button_states)
        self.view_model.canAnalyzeChanged.connect(self._update_button_states)
        self.view_model.canCancelAnalysisChanged.connect(self._update_button_states)

        self.view_model.availableModelsChanged.connect(self._populate_models_combobox)
        self.view_model.repoUrlChanged.connect(lambda: self._update_settings_fields())
        self.view_model.modelNameChanged.connect(lambda: self._update_settings_fields())
        self.view_model.maxTokensChanged.connect(lambda: self._update_settings_fields())
        self.view_model.instructionsTextChanged.connect(lambda: self._update_settings_fields())
        self.view_model.checkedExtensionsChanged.connect(self._update_extensions_ui)
        
        self.view_model.instructionsVisibilityChanged.connect(self._update_instructions_visibility)
        self.view_model.settingsVisibilityChanged.connect(self._update_settings_visibility)
        
        self.view_model.chatUpdateRequired.connect(self._render_chat_view)
        self.view_model.statusMessageChanged.connect(self._update_status_bar)
        self.view_model.tokenInfoChanged.connect(self.token_status_label.setText)
        
        self.view_model.showFileDialog.connect(self._show_file_dialog)
        self.view_model.showMessageDialog.connect(self._show_message_dialog)
        self.view_model.resetUiForNewSession.connect(self._update_all_states_from_vm)

        # Сигналы для обновления данных репозитория
        self.view_model.repoUrlChanged.connect(self._update_repo_url_field)
        self.view_model.selectedBranchChanged.connect(self._update_selected_branch)
        self.view_model.availableBranchesChanged.connect(self._populate_branch_combobox)

        # Сигналы для окна саммари
        self.view_summaries_button.clicked.connect(self._show_summaries_window)
        
    @Slot()
    def _show_summaries_window(self):
        """Создает (если нужно) и показывает окно с саммари."""
        if self.summaries_window is None:
            self.logger.info("Первый запуск: создание окна SummariesWindow.")
            self.summaries_window = SummariesWindow(self)
            self.view_model.fileSummariesUpdated.connect(self.summaries_window.update_summaries)
            self.summaries_window.windowClosed.connect(self._on_summaries_window_closed)
            self.summaries_window.update_summaries(self.view_model._model._file_summaries)
        
        self.logger.debug("Отображение окна SummariesWindow.")
        self.summaries_window.show()
        self.summaries_window.activateWindow()

    @Slot()
    def _on_summaries_window_closed(self):
        self.logger.debug("Окно саммари было закрыто (скрыто).")
        pass        

    def eventFilter(self, obj, event):
        if obj is self.input_textedit and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                if self.send_button.isEnabled(): self.send_button.click()
                return True
        return super().eventFilter(obj, event)

    # --- Слоты обновления UI ---
    @Slot()
    def _update_all_states_from_vm(self):
        self.logger.info("Полное обновление UI из ViewModel...")
        self._update_settings_fields()
        self._update_extensions_ui(self.view_model._checked_common_extensions, self.view_model._custom_extensions_text)
        self._populate_models_combobox(self.view_model._model.get_available_models())
        self._update_button_states()
        self._update_window_title()
    
    @Slot()
    def _update_window_title(self):
        self.setWindowTitle(self.view_model.windowTitle)

    @Slot()
    def _update_button_states(self):
        self.send_button.setEnabled(self.view_model.canSend)
        self.input_textedit.setReadOnly(not self.view_model.canSend)
        self.cancel_button.setEnabled(self.view_model.canCancelRequest)
        self.analyze_repo_button.setEnabled(self.view_model.canAnalyze)
        self.cancel_analysis_button.setEnabled(self.view_model.canCancelAnalysis)
        self.view_summaries_button.setEnabled(bool(self.view_model._model._file_summaries))

    @Slot()
    def _update_gemini_api_key_status(self):
        self.api_key_status_label.setText(self.view_model.geminiApiKeyStatusText)

    @Slot()
    def _update_github_token_status(self):
        self.github_token_status_label.setText(self.view_model.githubTokenStatusText)
        
    @Slot(list)
    def _populate_models_combobox(self, models: list):
        self.logger.info(f"Обновление списка моделей в UI: {models}")
        self.model_name_combobox.blockSignals(True)
        current_text = self.model_name_combobox.currentText()
        self.model_name_combobox.clear()
        self.model_name_combobox.addItems(models)
        self.model_name_combobox.setCurrentText(current_text)
        self.model_name_combobox.blockSignals(False)
    
    @Slot()
    def _update_settings_fields(self):
        self.model_name_combobox.setCurrentText(self.view_model.modelName)
        self.max_tokens_spinbox.setValue(self.view_model.maxTokens)
        if self.instructions_textedit.toPlainText() != self.view_model.instructionsText:
            self.instructions_textedit.setPlainText(self.view_model.instructionsText)
        self._on_instructions_changed()
        
    @Slot(set, str)
    def _update_extensions_ui(self, checked_common_set, custom_text):
        for ext, cb in self.common_ext_checkboxes.items():
            cb.setChecked(ext in checked_common_set)
        self.custom_ext_lineedit.setText(custom_text)

    @Slot(bool)
    def _update_settings_visibility(self, visible: bool):
        self.settings_group_box.setVisible(visible)
        self.toggle_settings_button.setText(self.tr("Свернуть настройки ▲") if visible else self.tr("Развернуть настройки ▼"))

    @Slot(bool)
    def _update_instructions_visibility(self, visible: bool):
        self.instructions_container.setVisible(visible)
        self.toggle_instructions_button.setText(self.tr("Свернуть инструкции ▲") if visible else self.tr("Развернуть инструкции ▼"))

    @Slot()
    def _render_chat_view(self):
        if not self.view_model.isChatViewReady: return
        self.logger.debug("Перерисовка ChatView...")
        history, last_error, intermediate_step = self.view_model.getChatHistoryForView()
        
        self.dialog_textedit.clear_chat()

        md_ext = ["fenced_code", "codehilite", "nl2br", "tables"]
        
        for index, msg in enumerate(history):
            role = msg.get("role")
            content = msg.get("parts", [""])[0]
            is_excluded = msg.get("excluded", False)
            html_out = markdown.markdown(content, extensions=md_ext) if role == "model" else f"<pre>{html.escape(content)}</pre>"
            self.dialog_textedit.add_message(role, f"{html_out}<hr>", index, is_excluded, is_last=False)
        
        if intermediate_step:
            self.dialog_textedit.add_message("system", f"<i>{html.escape(intermediate_step)}</i>", -1, False, is_last=False)

        if self.view_model.canCancelRequest or self.view_model.canCancelAnalysis:
            self.dialog_textedit.show_loader()
            
        if last_error:
            self.dialog_textedit.add_error_message(last_error)

        self.dialog_textedit.scroll_to_bottom()

    @Slot(str, int)
    def _update_status_bar(self, message: str, timeout: int):
        self._status_clear_timer.stop()
        self.statusBar().showMessage(message, 0)
        if timeout > 0:
            self._status_clear_timer.start(timeout)

    @Slot()
    def _clear_temporary_status_message(self):
        self.statusBar().clearMessage()

    @Slot()
    def _update_repo_url_field(self):
        if self.repo_url_lineedit.text() != self.view_model.repoUrl:
            self.repo_url_lineedit.setText(self.view_model.repoUrl)

    @Slot(list)
    def _populate_branch_combobox(self, branches: list):
        self.branch_combobox.blockSignals(True)
        self.branch_combobox.clear()
        if branches:
            self.branch_combobox.addItems(branches)
            self.branch_combobox.setEnabled(True)
        else:
            self.branch_combobox.setEnabled(False)
        self.branch_combobox.blockSignals(False)
        self._update_selected_branch()
        
    @Slot()
    def _update_selected_branch(self):
        self.branch_combobox.blockSignals(True)
        self.branch_combobox.setCurrentText(self.view_model.selectedBranch)
        self.branch_combobox.blockSignals(False)

    # --- Методы для работы с шаблонами ---
    def _load_instruction_templates(self):
        base_filename = "instruction_templates.json"
        # Используем язык, загруженный из .env при старте приложения
        current_app_lang = os.getenv('APP_LANGUAGE', QLocale.system().name().split('_')[0])

        # Для русского языка используется файл по умолчанию.
        # Для других языков ищем файл с суффиксом, например, _en.
        templates_to_load = base_filename
        if current_app_lang != 'ru': # Теперь используем current_app_lang
            base, ext = os.path.splitext(base_filename)
            localized_filename = f"{base}_{current_app_lang}{ext}"
            localized_path = self._get_resource_path(localized_filename)
            if os.path.exists(localized_path):
                templates_to_load = localized_filename

        self._templates_file_path = self._get_resource_path(templates_to_load)

        if os.path.exists(self._templates_file_path):
            try:
                with open(self._templates_file_path, 'r', encoding='utf-8') as f:
                    self.instruction_templates = json.load(f)
                self.logger.info(self.tr("Шаблоны инструкций загружены из '{0}': {1} шт.").format(os.path.basename(self._templates_file_path), len(self.instruction_templates)))
            except (json.JSONDecodeError, OSError) as e:
                self.logger.error(self.tr("Ошибка загрузки шаблонов из '{0}': {1}").format(self._templates_file_path, e))
                self.instruction_templates = {}
        else:
            self.instruction_templates = {}
            self.logger.warning(self.tr("Файл шаблонов не найден: {0}").format(self._templates_file_path))
            
    def _save_instruction_templates(self) -> bool:
        try:
            with open(self._templates_file_path, 'w', encoding='utf-8') as f:
                json.dump(self.instruction_templates, f, ensure_ascii=False, indent=4)
            self.logger.info(self.tr("Шаблоны инструкций сохранены в {0}").format(self._templates_file_path))
            return True
        except Exception as e:
            self.logger.error(self.tr("Ошибка сохранения шаблонов: {0}").format(e), exc_info=True)
            self._show_message_dialog("crit", self.tr("Ошибка сохранения"), self.tr("Не удалось сохранить шаблоны: {0}").format(e))
            return False

    def _populate_templates_combobox(self):
        self.templates_combobox.blockSignals(True)
        self.templates_combobox.clear()
        self.templates_combobox.addItems([self.CUSTOM_INSTRUCTIONS_TEXT] + sorted(self.instruction_templates.keys()) + [self.SAVE_AS_TEMPLATE_TEXT])
        self.templates_combobox.setCurrentIndex(0)
        self.templates_combobox.blockSignals(False)

    @Slot(int)
    def _on_template_selected(self, index):
        selected_text = self.templates_combobox.itemText(index)
        if selected_text == self.SAVE_AS_TEMPLATE_TEXT:
            current_instructions = self.instructions_textedit.toPlainText().strip()
            if not current_instructions:
                self._show_message_dialog("warn", self.tr("Нечего сохранять"), self.tr("Поле инструкций пустое."))
                self.templates_combobox.setCurrentIndex(0)
                return
            template_name, ok = QInputDialog.getText(self, self.tr("Сохранить шаблон"), self.tr("Введите имя нового шаблона:"))
            if ok and template_name.strip():
                if template_name in self.instruction_templates:
                    if QMessageBox.question(self, self.tr("Перезаписать?"), self.tr("Шаблон '{0}' уже существует. Перезаписать?").format(template_name)) == QMessageBox.StandardButton.No:
                        self.templates_combobox.setCurrentIndex(0)
                        return
                self.instruction_templates[template_name] = current_instructions
                if self._save_instruction_templates():
                    self._populate_templates_combobox()
                    self.templates_combobox.setCurrentText(template_name)
            else:
                self.templates_combobox.setCurrentIndex(0)
        elif selected_text != self.CUSTOM_INSTRUCTIONS_TEXT:
            self.instructions_textedit.setPlainText(self.instruction_templates.get(selected_text, ""))

    @Slot()
    def _on_instructions_changed(self):
        current_text = self.instructions_textedit.toPlainText()
        self.view_model.updateInstructions(current_text)
        self.templates_combobox.blockSignals(True)
        matched_name = next((name for name, content in self.instruction_templates.items() if content == current_text), None)
        self.templates_combobox.setCurrentText(matched_name or self.CUSTOM_INSTRUCTIONS_TEXT)
        self.templates_combobox.blockSignals(False)

    @Slot()
    def _open_manage_templates_dialog(self):
        if not ManageTemplatesDialog:
            self._show_message_dialog("crit", self.tr("Ошибка"), self.tr("Компонент управления шаблонами не загружен."))
            return
        dialog = ManageTemplatesDialog(self.instruction_templates, self)
        if dialog.exec():
            self.instruction_templates = dialog.get_updated_templates()
            if self._save_instruction_templates():
                self._populate_templates_combobox()
                self._update_settings_fields()

    # --- Вспомогательные методы ---
    @Slot()
    def _on_extensions_changed(self):
        checked = {ext for ext, cb in self.common_ext_checkboxes.items() if cb.isChecked()}
        custom = self.custom_ext_lineedit.text()
        self.view_model.updateExtensionsFromUi(checked, custom)
        
    def closeEvent(self, event):
        if not self._check_dirty_state(self.tr("выходом из приложения")):
            event.ignore()
            return
        # Корректно останавливаем поток логгера перед выходом
        if self._log_viewer_window is not None:
            self.logger.info("Main window is closing, ensuring log reader thread is stopped...")
            # Вызов close() окна логгера запустит его собственный closeEvent,
            # который вызовет _stop_log_reading и дождется завершения потока.
            self._log_viewer_window.close()
        event.accept()

    def _check_dirty_state(self, action_text: str) -> bool:
        if not self.view_model.isDirty: return True
        reply = QMessageBox.question(self, self.tr("Несохраненные изменения"), self.tr("Имеются несохраненные изменения.\nСохранить их перед {0}?").format(action_text),
                                     QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                     QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Save: return self.view_model.saveSession()
        return reply != QMessageBox.StandardButton.Cancel

    def _new_session(self):
        if self._check_dirty_state(self.tr("созданием новой сессии")): self.view_model.newSession()
    def _open_session(self):
        if self._check_dirty_state(self.tr("открытием другой сессии")): self.view_model.openSession()

    @Slot(str, str, str)
    def _show_file_dialog(self, dialog_type: str, title: str, filter_or_dir: str):
        if dialog_type == "open":
            filepath, _ = QFileDialog.getOpenFileName(self, title, os.path.expanduser("~"), filter_or_dir)
            if filepath: self.view_model.sessionFileSelectedToOpen(filepath)
        elif dialog_type == "save":
            default_path, file_filter = filter_or_dir.split(";;")
            filepath, _ = QFileDialog.getSaveFileName(self, title, os.path.join(os.path.expanduser("~"), default_path), file_filter)
            if filepath: self.view_model.sessionFileSelectedToSave(filepath)

    @Slot(str, str, str)
    def _show_message_dialog(self, msg_type: str, title: str, message: str):
        QMessageBox.information(self, title, message)

    @Slot()
    def _show_help_content(self):
        HelpDialog(self).exec()

    @Slot()
    def _show_about_dialog(self):
        QMessageBox.about(self, self.tr("О программе GitGemini Pro"),
                          self.tr("<b>GitGemini Pro v2.0</b><br><br>"
                                  "Интеллектуальный ассистент, разработанный для глубокого анализа кодовой базы "
                                  "GitHub-репозиториев. Используя передовые модели Google Gemini "
                                  "и локальные векторные базы данных, GitGemini Pro "
                                  "позволяет вам «задавать вопросы» своему коду, получать "
                                  "интеллектуальные ответы, проводить рефакторинг, "
                                  "генерировать документацию и многое другое, сохраняя при этом "
                                  "<b>полную конфиденциальность</b> ваших данных.<br><br>"
                                  "Автор: <a href='mailto:kobaltmail@gmail.com'>kobaltGIT</a><br>"
                                  "Лицензия: <a href='https://mit-license.org/'>MIT License<br><br>"))

def setup_logging() -> str: # Указываем, что функция возвращает str
    """Настраивает глобальную систему логирования и возвращает путь к текущему файлу лога."""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    log_filename = os.path.join(log_dir, f"gitgemini_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    file_handler = logging.handlers.RotatingFileHandler(log_filename, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    root_logger.info("Система логирования настроена.")
    return log_filename # Возвращаем имя файла

def get_base_path():
    """Определяет базовый путь для ресурсов и .env файла."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

def main():
    QCoreApplication.setOrganizationName("Kobalt")
    QCoreApplication.setApplicationName("GitGeminiPro")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu"
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)

    # --- Загрузка .env и настройка языка ---
    base_path = get_base_path()
    dotenv_path = os.path.join(base_path, '.env')

    # Создаем .env, если его нет
    if not os.path.exists(dotenv_path):
        with open(dotenv_path, 'w') as f:
            pass # Просто создаем пустой файл
    
    load_dotenv(dotenv_path=dotenv_path)

    # Получаем язык из .env или из системы
    app_lang = os.getenv('APP_LANGUAGE', QLocale.system().name().split('_')[0])
    
    app = QApplication(sys.argv)
    
    # --- Логика перевода ---
    translator = QTranslator()
    # Используем язык, полученный ранее
    locale = QLocale(app_lang) 
    translations_path = os.path.join(base_path, 'translations')
    if translator.load(locale, "app", "_", translations_path):
        app.installTranslator(translator)
        print(f"Файл перевода для локали '{app_lang}' успешно загружен.") # Добавил app_lang для лога
    else:
        print(f"Не удалось загрузить файл перевода для локали '{locale.name()}' из {translations_path}") # Используем print до настройки логгера
    
    log_file_path = setup_logging() # Захватываем путь к файлу логов
    
    logger = logging.getLogger(__name__)
    logger.info("="*20 + " Запуск приложения GitGemini Pro " + "="*20)
    logger.info(f"Используемый язык: {app_lang}")

    chat_model = ChatModel(app_lang=app_lang)
    chat_view_model = ChatViewModel(chat_model)
    window = MainWindow(chat_view_model, log_file_path=log_file_path) # Передаем путь к лог-файлу
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()