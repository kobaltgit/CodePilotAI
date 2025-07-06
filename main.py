# --- Файл: main.py ---

import sys
import os
import html
import json
import markdown
import logging
import logging.handlers
import datetime
from typing import Optional, Dict, Set, List

from PySide6.QtWebEngineWidgets import QWebEngineView

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QLineEdit, QFileDialog,
    QSizePolicy, QSpinBox, QMessageBox, QStatusBar, QGroupBox,
    QCheckBox, QDialog, QComboBox, QInputDialog, QStyle, QSplitter,
    QListWidget, QListWidgetItem, QTabWidget
)
from PySide6.QtCore import (
    Qt, Slot, QUrl, QTimer, QCoreApplication, QFileInfo, QTranslator, QLocale, QSettings, QPoint, QSize
)
from PySide6.QtGui import QAction, QKeySequence, QIcon, QFont, QActionGroup

# --- Импортируем наши модули ---
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
APP_NAME = "CodePilotAI"
APP_ICON_FILENAME = "app_icon.png"
TEMPLATES_FILENAME = "instruction_templates.json"
COMMON_EXTENSIONS = [".py", ".txt", ".md", ".json", ".html", ".css", ".js", ".yaml", ".yml", ".pdf", ".docx"]
MAX_RECENT_PROJECTS = 10

# --- Диалог справки (без изменений) ---
class HelpDialog(QDialog):
    # ... код HelpDialog остается таким же, как в предыдущих версиях ...
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Справка - {0}").format(APP_NAME))
        self.setMinimumSize(700, 500)
        layout = QVBoxLayout(self)
        self.help_view = QWebEngineView()
        layout.addWidget(self.help_view, 1)
        script_dir = os.path.dirname(os.path.abspath(__file__))

        current_app_lang = os.getenv('APP_LANGUAGE', QLocale.system().name().split('_')[0])
        help_file = f"help_content_{current_app_lang}.html"
        html_file_path = os.path.join(script_dir, help_file)

        if not os.path.exists(html_file_path):
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
    def __init__(self, view_model: ChatViewModel, log_file_path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        if not isinstance(view_model, ChatViewModel):
            raise TypeError("ViewModel required")
        self.view_model = view_model
        self.logger = logging.getLogger(__name__)
        self.settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Kobalt", APP_NAME)

        self._log_file_path = log_file_path
        self._log_viewer_window: Optional[LogViewerWindow] = None
        self.summaries_window: Optional[SummariesWindow] = None

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
        self._load_settings()

        self.view_model.windowTitleChanged.emit()
        self.view_model.geminiApiKeyStatusTextChanged.emit()
        self.view_model.githubTokenStatusTextChanged.emit()
        self._update_project_fields() # Вызываем явно для начального состояния
        self._update_all_states_from_vm() # Затем вызываем общий метод

    def _get_resource_path(self, filename: str) -> str:
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_path, filename)

    def _init_ui(self):
        self.CUSTOM_INSTRUCTIONS_TEXT = self.tr("(Пользовательские инструкции)")
        self.SAVE_AS_TEMPLATE_TEXT = self.tr("Сохранить текущие как шаблон...")
        self.setWindowTitle(APP_NAME)
        
        icon_path = self._get_resource_path(APP_ICON_FILENAME)
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- Левая панель: Проекты ---
        projects_panel = QWidget()
        projects_layout = QVBoxLayout(projects_panel)
        projects_layout.setContentsMargins(0, 0, 0, 0)
        projects_label = QLabel(self.tr("<b>Недавние проекты</b>"))
        self.projects_list_widget = QListWidget()
        self.projects_list_widget.setToolTip(self.tr("Двойной клик для открытия сессии"))
        projects_layout.addWidget(projects_label)
        projects_layout.addWidget(self.projects_list_widget)

        # --- Правая панель: Основная рабочая область ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # --- Разделитель ---
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(projects_panel)
        splitter.addWidget(right_panel)
        main_layout.addWidget(splitter)
        self.main_splitter = splitter

        # --- Вкладки для выбора типа проекта ---
        self.project_tabs = QTabWidget()
        right_layout.addWidget(self.project_tabs)
        
        # Вкладка GitHub
        github_tab = QWidget()
        github_layout = QVBoxLayout(github_tab)
        repo_layout = QHBoxLayout()
        repo_url_label = QLabel(self.tr("URL Репозитория:"))
        self.repo_url_lineedit = QLineEdit()
        self.repo_url_lineedit.setPlaceholderText("https://github.com/user/repository")
        branch_label = QLabel(self.tr("Ветка:"))
        self.branch_combobox = QComboBox()
        repo_layout.addWidget(repo_url_label); repo_layout.addWidget(self.repo_url_lineedit, 3)
        repo_layout.addWidget(branch_label); repo_layout.addWidget(self.branch_combobox, 1)
        github_layout.addLayout(repo_layout)
        self.project_tabs.addTab(github_tab, self.tr("GitHub Репозиторий"))

        # Вкладка Локальная папка
        local_tab = QWidget()
        local_layout = QVBoxLayout(local_tab)
        local_path_layout = QHBoxLayout()
        self.select_local_path_button = QPushButton(self.tr("Выбрать папку..."))
        self.local_path_lineedit = QLineEdit()
        self.local_path_lineedit.setPlaceholderText(self.tr("Путь к локальной папке проекта..."))
        self.local_path_lineedit.setReadOnly(True)
        local_path_layout.addWidget(self.select_local_path_button)
        local_path_layout.addWidget(self.local_path_lineedit, 1)
        local_layout.addLayout(local_path_layout)
        self.project_tabs.addTab(local_tab, self.tr("Локальная папка"))
        
        # --- Общие кнопки анализа ---
        analysis_layout = QHBoxLayout()
        self.analyze_repo_button = QPushButton(self.tr("Анализировать"))
        self.cancel_analysis_button = QPushButton(self.tr("Отмена анализа"))
        self.view_summaries_button = QPushButton("👁️")
        self.view_summaries_button.setToolTip(self.tr("Просмотреть проанализированные файлы"))
        self.view_summaries_button.setFixedSize(32, 32)
        font = self.view_summaries_button.font(); font.setPointSize(14); self.view_summaries_button.setFont(font)
        analysis_layout.addWidget(self.analyze_repo_button, 1)
        analysis_layout.addWidget(self.cancel_analysis_button, 1)
        analysis_layout.addSpacing(20)
        analysis_layout.addWidget(self.view_summaries_button)
        analysis_layout.addStretch(0)
        right_layout.addLayout(analysis_layout)

        # --- Блок настроек ---
        self.toggle_settings_button = QPushButton(self.tr("Развернуть настройки ▼"))
        right_layout.addWidget(self.toggle_settings_button)

        self.settings_group_box = QGroupBox(self.tr("Настройки"))
        settings_inner_layout = QVBoxLayout(self.settings_group_box)
        
        # Ключи API
        api_key_layout = QHBoxLayout()
        self.api_key_status_label = QLabel()
        self.api_key_lineedit = QLineEdit(); self.api_key_lineedit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_save_button = QPushButton(self.tr("Сохранить"))
        api_key_layout.addWidget(self.api_key_status_label); api_key_layout.addWidget(self.api_key_lineedit, 1); api_key_layout.addWidget(self.api_key_save_button)
        settings_inner_layout.addLayout(api_key_layout)

        github_token_layout = QHBoxLayout()
        self.github_token_status_label = QLabel()
        self.github_token_lineedit = QLineEdit(); self.github_token_lineedit.setEchoMode(QLineEdit.EchoMode.Password)
        self.github_token_save_button = QPushButton(self.tr("Сохранить"))
        github_token_layout.addWidget(self.github_token_status_label); github_token_layout.addWidget(self.github_token_lineedit, 1); github_token_layout.addWidget(self.github_token_save_button)
        settings_inner_layout.addLayout(github_token_layout)

        # Настройки модели и RAG
        model_settings_layout = QHBoxLayout()
        model_name_label = QLabel(self.tr("Модель ИИ:"))
        self.model_name_combobox = QComboBox(); self.model_name_combobox.setEditable(True)
        max_tokens_label = QLabel(self.tr("Макс. токенов ответа:"))
        self.max_tokens_spinbox = QSpinBox(); self.max_tokens_spinbox.setRange(256, 131072); self.max_tokens_spinbox.setSingleStep(1024)
        self.rag_enabled_checkbox = QCheckBox(self.tr("Исп. RAG (чанки)"))
        self.rag_enabled_checkbox.setToolTip(self.tr("Если включено, файлы будут разбиваться на чанки и саммари.\nЕсли выключено, файлы будут использоваться целиком."))
        model_settings_layout.addWidget(model_name_label); model_settings_layout.addWidget(self.model_name_combobox, 1);
        model_settings_layout.addWidget(max_tokens_label); model_settings_layout.addWidget(self.max_tokens_spinbox)
        model_settings_layout.addSpacing(20); model_settings_layout.addWidget(self.rag_enabled_checkbox)
        settings_inner_layout.addLayout(model_settings_layout)

        # Расширения
        extensions_group_label = QLabel(self.tr("Расширения файлов для анализа:"))
        settings_inner_layout.addWidget(extensions_group_label)
        checkbox_layout = QHBoxLayout()
        self.common_ext_checkboxes = {ext: QCheckBox(ext) for ext in COMMON_EXTENSIONS}
        for cb in self.common_ext_checkboxes.values(): checkbox_layout.addWidget(cb)
        checkbox_layout.addStretch(1)
        settings_inner_layout.addLayout(checkbox_layout)
        custom_ext_layout = QHBoxLayout()
        custom_ext_label = QLabel(self.tr("Другие:"))
        self.custom_ext_lineedit = QLineEdit(); self.custom_ext_lineedit.setPlaceholderText(".log .csv .xml ...")
        custom_ext_layout.addWidget(custom_ext_label); custom_ext_layout.addWidget(self.custom_ext_lineedit, 1)
        settings_inner_layout.addLayout(custom_ext_layout)
        
        right_layout.addWidget(self.settings_group_box)
        self.settings_group_box.setVisible(False)

        # --- Остальной UI (инструкции, чат) ---
        self.toggle_instructions_button = QPushButton(self.tr("Свернуть инструкции ▲"))
        right_layout.addWidget(self.toggle_instructions_button)
        self.instructions_container = QWidget()
        instructions_container_layout = QVBoxLayout(self.instructions_container)
        instructions_container_layout.setContentsMargins(0, 0, 0, 0)
        self.instructions_textedit = QTextEdit(); self.instructions_textedit.setPlaceholderText(self.tr("Системные инструкции...")); self.instructions_textedit.setFixedHeight(80)
        instructions_container_layout.addWidget(self.instructions_textedit)
        templates_layout = QHBoxLayout(); templates_label = QLabel(self.tr("Шаблон:")); self.templates_combobox = QComboBox(); self.manage_templates_button = QPushButton(self.tr("Управлять..."))
        templates_layout.addWidget(templates_label); templates_layout.addWidget(self.templates_combobox, 1); templates_layout.addWidget(self.manage_templates_button)
        instructions_container_layout.addLayout(templates_layout)
        right_layout.addWidget(self.instructions_container)

        self.dialog_textedit = ChatView(self.view_model, self)
        self.input_textedit = QTextEdit(); self.input_textedit.setPlaceholderText(self.tr("Введите запрос (Ctrl+Enter для отправки)...")); self.input_textedit.setFixedHeight(100)
        
        bottom_button_layout = QHBoxLayout()
        self.cancel_button = QPushButton(self.tr("Отмена"))
        self.send_button = QPushButton(self.tr("Отправить Ctrl ↵"))
        bottom_button_layout.addWidget(self.cancel_button); bottom_button_layout.addStretch(1); bottom_button_layout.addWidget(self.send_button)

        right_layout.addWidget(self.dialog_textedit, 1)
        right_layout.addWidget(self.input_textedit)
        right_layout.addLayout(bottom_button_layout)
        
        status_bar = QStatusBar(self); self.setStatusBar(status_bar); status_bar.addPermanentWidget(self.token_status_label)
        self.input_textedit.installEventFilter(self)

    def _create_menu(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu(self.tr("&Файл"))
        actions = [
            (self.tr("&Новая сессия"), QKeySequence.StandardKey.New, self.view_model.newSession),
            (self.tr("&Открыть сессию..."), QKeySequence.StandardKey.Open, self.view_model.openSession),
            (self.tr("&Сохранить сессию"), QKeySequence.StandardKey.Save, self.view_model.saveSession),
            (self.tr("Сохранить сессию &как..."), QKeySequence.StandardKey.SaveAs, self.view_model.saveSessionAs),
            None,
            (self.tr("Очистить список недавних проектов"), None, self._clear_recent_projects),
            None,
            (self.tr("&Выход"), QKeySequence.StandardKey.Quit, self.close)
        ]
        for item in actions:
            if item:
                text, shortcut, slot = item
                action = QAction(text, self)
                if shortcut: action.setShortcut(shortcut)
                action.triggered.connect(slot)
                file_menu.addAction(action)
            else:
                file_menu.addSeparator()
        
        self._create_language_menu()
        view_menu = menu_bar.addMenu(self.tr("&Вид"))
        show_logs_action = QAction(self.tr("Показать &Логи"), self); show_logs_action.setShortcut("Ctrl+L"); show_logs_action.triggered.connect(self._show_log_viewer)
        view_menu.addAction(show_logs_action)

        help_menu = menu_bar.addMenu(self.tr("&Справка"))
        help_content_action = QAction(self.tr("Содержание..."), self); help_content_action.setShortcut(QKeySequence.StandardKey.HelpContents); help_content_action.triggered.connect(self._show_help_content)
        about_action = QAction(self.tr("О программе..."), self); about_action.triggered.connect(self._show_about_dialog)
        help_menu.addAction(help_content_action); help_menu.addSeparator(); help_menu.addAction(about_action)
    
    # ... Остальные методы, такие как _create_language_menu, _connect_signals и т.д. ...
    def _create_language_menu(self): # ... (без изменений) ...
        lang_menu = self.menuBar().addMenu(self.tr("&Язык"))
        lang_group = QActionGroup(self); lang_group.setExclusive(True)
        ru_action = QAction(self.tr("Русский"), self); ru_action.setCheckable(True); ru_action.triggered.connect(lambda: self._switch_language('ru'))
        lang_menu.addAction(ru_action); lang_group.addAction(ru_action)
        en_action = QAction(self.tr("English"), self); en_action.setCheckable(True); en_action.triggered.connect(lambda: self._switch_language('en'))
        lang_menu.addAction(en_action); lang_group.addAction(en_action)
        current_lang = os.getenv('APP_LANGUAGE', QLocale.system().name().split('_')[0])
        (ru_action if current_lang == 'ru' else en_action).setChecked(True)

    @Slot(str)
    def _switch_language(self, lang_code: str): # ... (без изменений) ...
        try:
            self.settings.setValue("interface/language", lang_code)
            QMessageBox.information(self, self.tr("Смена языка"), self.tr("Язык будет изменен после перезапуска приложения."))
        except Exception as e:
            QMessageBox.critical(self, self.tr("Ошибка сохранения"), self.tr("Не удалось сохранить настройку языка: {0}").format(e))

    def _connect_signals(self):
        # Команды от пользователя
        self.project_tabs.currentChanged.connect(self._on_project_tab_changed)
        self.repo_url_lineedit.editingFinished.connect(lambda: self.view_model.updateRepoUrl(self.repo_url_lineedit.text()))
        self.branch_combobox.currentTextChanged.connect(self.view_model.updateSelectedBranch)
        self.select_local_path_button.clicked.connect(self.view_model.selectLocalPath)
        
        self.projects_list_widget.itemDoubleClicked.connect(self._on_recent_project_selected)

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
        self.rag_enabled_checkbox.stateChanged.connect(lambda state: self.view_model.updateRagEnabled(bool(state))) # Убедитесь, что 'state' - это int (0,1,2)
        self.instructions_textedit.textChanged.connect(self._on_instructions_changed)
        for checkbox in self.common_ext_checkboxes.values(): checkbox.stateChanged.connect(self._on_extensions_changed)
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
        # self.view_model.projectDataChanged.connect(self._update_project_fields)
        self.view_model.modelNameChanged.connect(self._update_settings_fields)
        self.view_model.maxTokensChanged.connect(self._update_settings_fields)
        self.view_model.instructionsTextChanged.connect(self._update_settings_fields)
        self.view_model.ragEnabledChanged.connect(self._update_settings_fields)
        self.view_model.projectTypeChanged.connect(self._update_project_fields)
        self.view_model.repoUrlChanged.connect(self._update_project_fields)
        self.view_model.localPathChanged.connect(self._update_project_fields)
        self.view_model.selectedBranchChanged.connect(self._update_project_fields)
        self.view_model.availableBranchesChanged.connect(self._update_project_fields)
        self.view_model.checkedExtensionsChanged.connect(self._update_extensions_ui)
        
        self.view_model.instructionsVisibilityChanged.connect(self._update_instructions_visibility)
        self.view_model.settingsVisibilityChanged.connect(self._update_settings_visibility)
        
        self.view_model.chatUpdateRequired.connect(self._render_chat_view)
        self.view_model.statusMessageChanged.connect(self._update_status_bar)
        self.view_model.tokenInfoChanged.connect(self.token_status_label.setText)
        
        self.view_model.showFileDialog.connect(self._show_file_dialog)
        self.view_model.showMessageDialog.connect(self._show_message_dialog)
        self.view_model.resetUiForNewSession.connect(self._update_all_states_from_vm)

        self.view_model.setInitialSessionPathSignal.connect(self._add_to_recent_projects)

        # Окно саммари
        self.view_summaries_button.clicked.connect(self._show_summaries_window)

    def _show_summaries_window(self):
        if self.summaries_window is None:
            self.summaries_window = SummariesWindow(self)
            self.view_model.fileSummariesUpdated.connect(self.summaries_window.update_summaries)
        self.summaries_window.update_summaries(self.view_model._model._file_summaries if hasattr(self.view_model._model, '_file_summaries') else {})
        self.summaries_window.show()
        self.summaries_window.activateWindow()
        
    def eventFilter(self, obj, event): # ... (без изменений) ...
        if obj is self.input_textedit and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                if self.send_button.isEnabled(): self.send_button.click()
                return True
        return super().eventFilter(obj, event)

    # --- Слоты и методы ---
    @Slot()
    def _update_all_states_from_vm(self): # ... (без изменений) ...
        self._update_settings_fields()
        self._update_extensions_ui(set(), "")
        self._populate_models_combobox(self.view_model._model.get_available_models())
        self._update_button_states()
        self._update_window_title()

    @Slot()
    def _on_project_tab_changed(self, index):
        if self.project_tabs.tabText(index) == self.tr("GitHub Репозиторий"):
            self.view_model.updateProjectType('github')
        else:
            self.view_model.updateProjectType('local')

    @Slot()
    def _update_project_fields(self):
        ptype = self.view_model.projectType
        # Обновление вкладки
        if ptype == 'github' and self.project_tabs.currentIndex() != 0: self.project_tabs.setCurrentIndex(0)
        elif ptype == 'local' and self.project_tabs.currentIndex() != 1: self.project_tabs.setCurrentIndex(1)
        # Обновление полей
        self.repo_url_lineedit.setText(self.view_model.repoUrl)
        self.local_path_lineedit.setText(self.view_model.localPath)
        # Обновление веток
        self.branch_combobox.blockSignals(True)
        self.branch_combobox.clear()
        branches = self.view_model.availableBranches
        if branches:
            self.branch_combobox.addItems(branches)
            self.branch_combobox.setCurrentText(self.view_model.selectedBranch)
            self.branch_combobox.setEnabled(True)
        else:
            self.branch_combobox.setEnabled(False)
        self.branch_combobox.blockSignals(False)
        
    @Slot(QListWidgetItem)
    def _on_recent_project_selected(self, item: QListWidgetItem):
        filepath = item.data(Qt.ItemDataRole.UserRole)
        if self._check_dirty_state(self.tr("открытием проекта '{0}'").format(os.path.basename(filepath))):
            self.view_model.sessionFileSelectedToOpen(filepath)

    @Slot()
    def _update_button_states(self):
        self.send_button.setEnabled(self.view_model.canSend)
        self.input_textedit.setReadOnly(not self.view_model.canSend)
        self.cancel_button.setEnabled(self.view_model.canCancelRequest)
        self.analyze_repo_button.setEnabled(self.view_model.canAnalyze)
        self.cancel_analysis_button.setEnabled(self.view_model.canCancelAnalysis)
        has_summaries = bool(self.view_model._model._project_context)
        self.view_summaries_button.setEnabled(has_summaries)
    
    # ... другие слоты обновления UI (без значительных изменений) ...
    @Slot()
    def _update_window_title(self): self.setWindowTitle(self.view_model.windowTitle)
    @Slot()
    def _update_gemini_api_key_status(self): self.api_key_status_label.setText(self.view_model.geminiApiKeyStatusText)
    @Slot()
    def _update_github_token_status(self): self.github_token_status_label.setText(self.view_model.githubTokenStatusText)
    @Slot(list)
    def _populate_models_combobox(self, models: list):
        current_text = self.model_name_combobox.currentText()

        # Отключаем сигналы, чтобы избежать срабатывания currentTextChanged при программном изменении
        self.model_name_combobox.blockSignals(True) 

        self.model_name_combobox.clear()
        if models: 
            self.model_name_combobox.addItems(models)

        # Устанавливаем текущий текст только если он не пустой, или если это первое заполнение.
        # Если current_text уже есть и он один из моделей, он будет установлен.
        # Иначе будет установлена модель из view_model.modelName (дефолтная).
        if current_text and current_text in models:
            self.model_name_combobox.setCurrentText(current_text)
        else:
            self.model_name_combobox.setCurrentText(self.view_model.modelName)

        # Включаем сигналы обратно
        self.model_name_combobox.blockSignals(False)

        # Если модель в combobox изменилась на другую, чем была в ViewModel,
        # явно вызываем обновление в модели (хотя обычно это происходит по сигналу)
        # Это нужно, если setCurrentText выбрал другую модель, чем была ранее.
        if self.model_name_combobox.currentText() != self.view_model.modelName:
            self.view_model.updateModelName(self.model_name_combobox.currentText())

    @Slot()
    def _update_settings_fields(self):
        self.model_name_combobox.setCurrentText(self.view_model.modelName)
        self.max_tokens_spinbox.setValue(self.view_model.maxTokens)
        self.rag_enabled_checkbox.setChecked(self.view_model.ragEnabled)
        if self.instructions_textedit.toPlainText() != self.view_model.instructionsText: self.instructions_textedit.setPlainText(self.view_model.instructionsText)
    @Slot(set, str)
    def _update_extensions_ui(self, checked_set, custom_text):
        for ext, cb in self.common_ext_checkboxes.items(): cb.setChecked(ext in checked_set)
        self.custom_ext_lineedit.setText(custom_text)
    @Slot(bool)
    def _update_settings_visibility(self, visible): self.settings_group_box.setVisible(visible); self.toggle_settings_button.setText(self.tr("Свернуть настройки ▲") if visible else self.tr("Развернуть настройки ▼"))
    @Slot(bool)
    def _update_instructions_visibility(self, visible): self.instructions_container.setVisible(visible); self.toggle_instructions_button.setText(self.tr("Свернуть инструкции ▲") if visible else self.tr("Развернуть инструкции ▼"))
    @Slot()
    def _render_chat_view(self): # ... код рендеринга без изменений ...
        if not self.view_model.isChatViewReady: return
        history, last_error, intermediate_step = self.view_model.getChatHistoryForView()
        self.dialog_textedit.clear_chat()
        md_ext = ["fenced_code", "codehilite", "nl2br", "tables"]
        for index, msg in enumerate(history):
            role = msg.get("role"); content = msg.get("parts", [""])[0]; is_excluded = msg.get("excluded", False)
            html_out = markdown.markdown(content, extensions=md_ext) if role == "model" else f"<pre>{html.escape(content)}</pre>"
            self.dialog_textedit.add_message(role, f"{html_out}<hr>", index, is_excluded, is_last=False)
        if intermediate_step: self.dialog_textedit.add_message("system", f"<i>{html.escape(intermediate_step)}</i>", -1, False, is_last=False)
        if self.view_model.canCancelRequest or self.view_model.canCancelAnalysis: self.dialog_textedit.show_loader()
        if last_error: self.dialog_textedit.add_error_message(last_error)
        self.dialog_textedit.scroll_to_bottom()
    @Slot(str, int)
    def _update_status_bar(self, message, timeout): self._status_clear_timer.stop(); self.statusBar().showMessage(message, 0); \
        (self._status_clear_timer.start(timeout) if timeout > 0 else None)
    @Slot()
    def _clear_temporary_status_message(self): self.statusBar().clearMessage()

    # Методы сохранения/загрузки состояния окна
    def _load_settings(self):
        self.resize(self.settings.value("window/size", QSize(1200, 800)))
        self.move(self.settings.value("window/pos", QPoint(50, 50)))
        self.main_splitter.restoreState(self.settings.value("window/splitterState"))
        self._load_recent_projects()

    def _save_settings(self):
        self.settings.setValue("window/size", self.size())
        self.settings.setValue("window/pos", self.pos())
        self.settings.setValue("window/splitterState", self.main_splitter.saveState())
        
    def _load_recent_projects(self):
        self.projects_list_widget.clear()
        paths = self.settings.value("recentProjects/list", [], type=list)
        for path in paths:
            if os.path.exists(path):
                item = QListWidgetItem(os.path.basename(path).replace(db_manager.SESSION_EXTENSION, ""))
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                self.projects_list_widget.addItem(item)
                
    @Slot(str)
    def _add_to_recent_projects(self, filepath: str):
        if not filepath: return
        paths = self.settings.value("recentProjects/list", [], type=list)
        if filepath in paths: paths.remove(filepath)
        paths.insert(0, filepath)
        self.settings.setValue("recentProjects/list", paths[:MAX_RECENT_PROJECTS])
        self._load_recent_projects()

    @Slot()
    def _clear_recent_projects(self):
        self.settings.remove("recentProjects/list")
        self.projects_list_widget.clear()

    def closeEvent(self, event):
        if not self._check_dirty_state(self.tr("выходом из приложения")):
            event.ignore()
            return
        self._save_settings()
        if self._log_viewer_window: self._log_viewer_window.close()
        event.accept()

    # ... все остальные методы без изменений ...
    def _check_dirty_state(self, action_text: str) -> bool: # ... (без изменений) ...
        if not self.view_model.isDirty: return True
        reply = QMessageBox.question(self, self.tr("Несохраненные изменения"), self.tr("Имеются несохраненные изменения.\nСохранить их перед {0}?").format(action_text),
                                     QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Save: return self.view_model.saveSession()
        return reply != QMessageBox.StandardButton.Cancel

    @Slot(str, str, str)
    def _show_file_dialog(self, dialog_type, title, filter_or_dir): # ... (без изменений) ...
        if dialog_type == "folder":
            path = QFileDialog.getExistingDirectory(self, title, filter_or_dir)
            if path: self.view_model.localPathSelected(path)
        elif dialog_type == "open":
            filepath, _ = QFileDialog.getOpenFileName(self, title, os.path.expanduser("~"), filter_or_dir)
            if filepath: self._add_to_recent_projects(filepath); self.view_model.sessionFileSelectedToOpen(filepath)
        elif dialog_type == "save":
            default_path, file_filter = filter_or_dir.split(";;")
            filepath, _ = QFileDialog.getSaveFileName(self, title, os.path.join(os.path.expanduser("~"), default_path), file_filter)
            if filepath: self._add_to_recent_projects(filepath); self.view_model.sessionFileSelectedToSave(filepath)

    # Остальные слоты и методы остаются практически без изменений,
    # так как они уже вызываются из ViewModel, который мы адаптировали.
    # Это демонстрация хорошей архитектуры MVP/MVVM.
    @Slot(str, str, str)
    def _show_message_dialog(self, msg_type, title, message): QMessageBox.information(self, title, message)
    @Slot()
    def _show_help_content(self): HelpDialog(self).exec()
    @Slot()
    def _show_about_dialog(self): QMessageBox.about(self, self.tr("О программе {0}").format(APP_NAME), self.tr("<b>{0} v2.1</b><br>...").format(APP_NAME)) # Placeholder
    @Slot()
    def _show_log_viewer(self):
        if self._log_viewer_window is None:
            self._log_viewer_window = LogViewerWindow(self._log_file_path, self)
            self._log_viewer_window.destroyed.connect(lambda: setattr(self, '_log_viewer_window', None))
        self._log_viewer_window.show()
        self._log_viewer_window.activateWindow()
    @Slot()
    def _on_extensions_changed(self):
        checked = {ext for ext, cb in self.common_ext_checkboxes.items() if cb.isChecked()}
        custom = self.custom_ext_lineedit.text()
        self.view_model.updateExtensionsFromUi(checked, custom)
    @Slot()
    def _on_instructions_changed(self):
        current_text = self.instructions_textedit.toPlainText()
        self.view_model.updateInstructions(current_text)
        # ... (логика выбора шаблона в комбобоксе) ...
    @Slot()
    def _open_manage_templates_dialog(self): # ... (без изменений) ...
        if not ManageTemplatesDialog: return
        dialog = ManageTemplatesDialog(self.instruction_templates, self)
        if dialog.exec():
            self.instruction_templates = dialog.get_updated_templates()
            if self._save_instruction_templates(): self._populate_templates_combobox(); self._update_settings_fields()
    def _load_instruction_templates(self): # ... (без изменений) ...
        if os.path.exists(self._templates_file_path):
            try:
                with open(self._templates_file_path, 'r', encoding='utf-8') as f: self.instruction_templates = json.load(f)
            except Exception as e: self.logger.error(f"Ошибка загрузки шаблонов: {e}")
        else: self.instruction_templates = {}
    def _save_instruction_templates(self): # ... (без изменений) ...
        try:
            with open(self._templates_file_path, 'w', encoding='utf-8') as f: json.dump(self.instruction_templates, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e: self.logger.error(f"Ошибка сохранения шаблонов: {e}"); return False
    def _populate_templates_combobox(self): # ... (без изменений) ...
        self.templates_combobox.blockSignals(True)
        self.templates_combobox.clear()
        self.templates_combobox.addItems([self.CUSTOM_INSTRUCTIONS_TEXT] + sorted(self.instruction_templates.keys()) + [self.SAVE_AS_TEMPLATE_TEXT])
        self.templates_combobox.setCurrentIndex(0)
        self.templates_combobox.blockSignals(False)
    @Slot(int)
    def _on_template_selected(self, index): # ... (без изменений) ...
        selected_text = self.templates_combobox.itemText(index)
        if selected_text == self.SAVE_AS_TEMPLATE_TEXT:
            # ... (логика сохранения нового шаблона) ...
            pass
        elif selected_text != self.CUSTOM_INSTRUCTIONS_TEXT:
            self.instructions_textedit.setPlainText(self.instruction_templates.get(selected_text, ""))
    
# --- Точка входа в приложение ---
def setup_logging() -> str:
    log_dir = "logs"
    if not os.path.exists(log_dir): os.makedirs(log_dir)
    log_filename = os.path.join(log_dir, f"{APP_NAME.lower()}_{datetime.datetime.now().strftime('%Y-%m-%d')}.log")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.handlers.RotatingFileHandler(log_filename, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG); file_handler.setFormatter(formatter); root_logger.addHandler(file_handler)
    console_handler = logging.StreamHandler(); console_handler.setLevel(logging.DEBUG); console_handler.setFormatter(formatter); root_logger.addHandler(console_handler)
    root_logger.info(f"Система логирования настроена. Логи пишутся в {log_filename}")
    return log_filename

def main():
    QCoreApplication.setOrganizationName("Kobalt")
    QCoreApplication.setApplicationName(APP_NAME)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --disable-software-rasterizer --disable-gpu-compositing --no-sandbox"
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)

    # --- Настройка языка ---
    settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Kobalt", APP_NAME)
    app_lang = settings.value("interface/language", QLocale.system().name().split('_')[0])
    
    app = QApplication(sys.argv)
    
    translator = QTranslator()
    locale = QLocale(app_lang)
    base_path = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else os.path.dirname(sys.executable)
    if translator.load(locale, "app", "_", os.path.join(base_path, "translations")):
        app.installTranslator(translator)
    
    log_file_path = setup_logging()
    logger = logging.getLogger(__name__)
    logger.info(f"--- Запуск {APP_NAME} ---")

    # --- Обработка запуска по файлу ---
    filepath_to_open = None
    if len(sys.argv) > 1:
        path_arg = sys.argv[1]
        if os.path.isfile(path_arg) and path_arg.endswith(db_manager.SESSION_EXTENSION):
            filepath_to_open = path_arg
            logger.info(f"Приложение запущено с файлом: {filepath_to_open}")
    
    chat_model = ChatModel(app_lang=app_lang)
    chat_view_model = ChatViewModel(chat_model)
    if filepath_to_open:
        chat_view_model.set_initial_session_path(filepath_to_open)

    window = MainWindow(chat_view_model, log_file_path=log_file_path)
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()