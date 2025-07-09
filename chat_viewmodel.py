# --- Файл: chat_viewmodel.py ---

import os
import re
import datetime
import logging
from typing import Optional, List, Dict, Any, Tuple, Set

from PySide6.QtCore import QObject, Signal, Slot, Property, QUrl, QThread
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication

from chat_model import ChatModel, CONTEXT_WINDOW_LIMIT
import db_manager
from network_checker import NetworkStatusChecker

logger = logging.getLogger(__name__)

# Эта константа должна быть синхронизирована с MainWindow
COMMON_EXTENSIONS = [".py", ".txt", ".md", ".json", ".html", ".css", ".js", ".yaml", ".yml", ".pdf", ".docx"]

class ChatViewModel(QObject):
    """
    Посредник между View (MainWindow) и Model (ChatModel).
    Предоставляет данные для отображения и обрабатывает команды пользователя.
    """
    # --- Сигналы для управления поиском в ChatView ---
    performSearch = Signal(str, object)
    clearSearchHighlight = Signal()
    searchStatusUpdate = Signal(bool)
    
    # --- Сигналы для обновления свойств View ---
    # Статусы
    geminiApiKeyStatusTextChanged = Signal()
    githubTokenStatusTextChanged = Signal()

    # Состояние кнопок и полей
    canSendChanged = Signal()
    canCancelRequestChanged = Signal()
    canAnalyzeChanged = Signal()
    canCancelAnalysisChanged = Signal()
    canUpdateFromGitChanged = Signal()

    # --- НОВЫЕ СИГНАЛЫ ДЛЯ ПРОЕКТА ---
    projectTypeChanged = Signal(str) # 'github' или 'local'
    repoUrlChanged = Signal(str)
    localPathChanged = Signal(str)
    selectedBranchChanged = Signal(str)
    availableBranchesChanged = Signal(list)
    # ---

    # --- НОВЫЕ СИГНАЛЫ ДЛЯ UI ---
    analysisStateChanged = Signal(bool) # True - запущен, False - остановлен
    analysisProgress_for_bar_changed = Signal(int, int) # processed, total
    networkStatusChanged = Signal(bool) # True - онлайн, False - оффлайн

    # Поля настроек
    modelNameChanged = Signal()
    availableModelsChanged = Signal(list)
    maxTokensChanged = Signal()
    ragEnabledChanged = Signal(bool)
    semanticSearchEnabledChanged = Signal(bool)
    instructionsTextChanged = Signal()
    checkedExtensionsChanged = Signal(set, str)

    toggleAllButtonPropsChanged = Signal()

    # Состояние окна и чата
    windowTitleChanged = Signal()
    isDirtyChanged = Signal()
    isChatViewReadyChanged = Signal()
    chatUpdateRequired = Signal()

    # Статус-бар
    statusMessageChanged = Signal(str, int)
    tokenInfoChanged = Signal(str)

    # Видимость виджетов
    settingsVisibilityChanged = Signal(bool)
    instructionsVisibilityChanged = Signal(bool)
    fileSummariesUpdated = Signal(dict)

    # --- Сигналы для выполнения действий в View ---
    showFileDialog = Signal(str, str, str)
    showSaveFileDialogForGeneratedCode = Signal(str, str) # default_filename, code_content
    showDiffWindow = Signal(str, str, str) # original_code, new_code, file_path
    showMessageDialog = Signal(str, str, str)
    clearApiKeyInput = Signal()
    clearTokenInput = Signal()
    resetUiForNewSession = Signal()
    apiRequestStarted = Signal()
    setInitialSessionPathSignal = Signal(str) # Для обработки запуска через файл
    sessionSavedSuccessfully = Signal(str)

    # --- Сигналы для управления поиском в ChatView ---
    performSearch = Signal(str, object)
    clearSearchHighlight = Signal()
    searchStatusUpdate = Signal(bool)

    def __init__(self, model: ChatModel, parent: None = None):
        super().__init__(parent)
        if not isinstance(model, ChatModel):
            raise TypeError("Model must be an instance of ChatModel")
        self._model = model

        # --- НОВОЕ: Настройка проверки сети ---
        self._network_thread: Optional[QThread] = None
        self._network_checker: Optional[NetworkStatusChecker] = None
        self._setup_network_checker()

        self._search_query: Optional[str] = None

        # --- Внутреннее состояние ViewModel ---
        self._is_chat_view_ready: bool = False
        self._is_request_running: bool = False
        self._is_analysis_running: bool = False
        self._last_api_error: Optional[str] = None
        self._last_api_intermediate_step: Optional[str] = None

        self._settings_visible: bool = False
        self._instructions_visible: bool = True
        self._search_query: Optional[str] = None
        
        # Для отложенной загрузки сессии при запуске
        self._pending_session_load_path: Optional[str] = None

        self._connect_model_signals()
        self._on_session_loaded() # Первичная инициализация

    def _setup_network_checker(self):
        """Инициализирует и запускает воркер для проверки сети."""
        self._network_thread = QThread()
        self._network_checker = NetworkStatusChecker()
        self._network_checker.moveToThread(self._network_thread)

        self._network_checker.status_changed.connect(self.networkStatusChanged)
        self._network_thread.started.connect(self._network_checker.start_checking)
        # Убедимся, что поток завершается при выходе из приложения
        self._network_thread.finished.connect(self._network_checker.deleteLater)
        qapp = QApplication.instance()
        if qapp:
            qapp.aboutToQuit.connect(self._network_thread.quit)

        self._network_thread.start()

    def _connect_model_signals(self):
        # Статусы
        self._model.geminiApiKeyStatusChanged.connect(self._on_gemini_api_key_status_changed)
        self._model.githubTokenStatusChanged.connect(self._on_github_token_status_changed)
        self._model.availableModelsChanged.connect(self.availableModelsChanged)
        self._model.projectDataChanged.connect(self._on_project_data_changed)

        # Анализ
        self._model.analysisStarted.connect(self._on_analysis_started)
        self._model.analysisProgressUpdated.connect(self._on_analysis_progress_updated) # Подключаем к новому слоту
        self._model.analysisFinished.connect(self._on_analysis_finished)
        self._model.analysisError.connect(self._on_analysis_error)

        # API запросы
        self._model.apiRequestStarted.connect(self._on_api_request_started)
        self._model.apiResponseReceived.connect(self._on_api_response_received)
        self._model.apiIntermediateStep.connect(self._on_api_intermediate_step)
        self._model.apiErrorOccurred.connect(self._on_api_error_occurred)
        self._model.apiRequestFinished.connect(self._on_api_request_finished)

        # Сессия и история
        self._model.historyChanged.connect(self._on_history_changed)
        self._model.sessionStateChanged.connect(self._on_session_state_changed)
        self._model.sessionLoaded.connect(self._on_session_loaded)
        self._model.sessionError.connect(self._on_session_error)

        # Общие
        self._model.statusMessage.connect(self.statusMessageChanged)
        self._model.tokenCountUpdated.connect(self._on_token_count_updated)
        self._model.fileSummariesChanged.connect(self.fileSummariesUpdated)

    # --- Properties для биндинга в View ---

    @Property(str, notify=geminiApiKeyStatusTextChanged)
    def geminiApiKeyStatusText(self) -> str:
        loaded = self._model._gemini_api_key_loaded
        return (self.tr("<font color='green'>Ключ API: Загружен</font>") if loaded
                else self.tr("<font color='red'>Ключ API: Не найден!</font>"))

    @Property(str, notify=githubTokenStatusTextChanged)
    def githubTokenStatusText(self) -> str:
        loaded = self._model._github_token_loaded
        return (self.tr("<font color='green'>Токен GitHub: Загружен</font>") if loaded
                else self.tr("<font color='red'>Токен GitHub: Не найден!</font>"))

    # --- Свойства проекта ---
    @Property(str, notify=projectTypeChanged)
    def projectType(self) -> str: return self._model.get_project_type() or ""
    @Property(str, notify=repoUrlChanged)
    def repoUrl(self) -> str: return self._model.get_repo_url() or ""
    @Property(str, notify=localPathChanged)
    def localPath(self) -> str: return self._model.get_local_path() or ""
    @Property(str, notify=selectedBranchChanged)
    def selectedBranch(self) -> str: return self._model.get_selected_branch() or ""
    @Property(list, notify=availableBranchesChanged)
    def availableBranches(self) -> List[str]: return self._model.get_available_branches()

    # --- Свойства настроек ---
    @Property(str, notify=modelNameChanged)
    def modelName(self) -> str: return self._model.get_model_name()
    @Property(int, notify=maxTokensChanged)
    def maxTokens(self) -> int: return self._model.get_max_tokens()
    @Property(str, notify=instructionsTextChanged)
    def instructionsText(self) -> str: return self._model.get_instructions()
    @Property(bool, notify=ragEnabledChanged)
    def ragEnabled(self) -> bool: return self._model.get_rag_enabled()
    @Property(bool, notify=semanticSearchEnabledChanged)
    def semanticSearchEnabled(self) -> bool: return self._model.get_semantic_search_enabled()

    # --- Общие свойства ---
    @Property(bool, notify=isDirtyChanged)
    def isDirty(self) -> bool: return self._model.is_dirty()
    @Property(str, notify=windowTitleChanged)
    def windowTitle(self) -> str:
        base_title = self.tr("CodePilotAI")
        session_path = self._model._current_session_filepath
        session_name = (os.path.basename(session_path).replace(db_manager.SESSION_EXTENSION, "")
                        if session_path
                        else self.tr("Новая сессия"))
        dirty_indicator = "*" if self._model.is_dirty() else ""
        return f"{base_title} - {session_name}{dirty_indicator}"

    # --- Свойства состояния кнопок ---
    @Property(bool, notify=canSendChanged)
    def canSend(self) -> bool:
        # Чат активен, если ChatView готов, API ключ загружен, и нет активного запроса.
        # Проектный контекст не обязателен для базового чата.
        return bool(self._is_chat_view_ready and
                    self._model._gemini_api_key_loaded and
                    not self._is_request_running and
                    not self._is_analysis_running)

    @Property(bool, notify=canCancelRequestChanged)
    def canCancelRequest(self) -> bool: return self._is_request_running

    @Property(bool, notify=canAnalyzeChanged)
    def canAnalyze(self) -> bool:
        ptype = self._model.get_project_type()
        is_project_data_valid = False
        if ptype == 'github':
            is_project_data_valid = bool(self._model.get_repo_url() and self._model.get_selected_branch())
        elif ptype == 'local':
            path = self._model.get_local_path()
            is_project_data_valid = bool(path and os.path.isdir(path))

        return (self._is_chat_view_ready and
                is_project_data_valid and
                (self._model._gemini_api_key_loaded or not self._model.get_rag_enabled()) and
                (self._model._github_token_loaded if ptype == 'github' else True) and
                not self._is_analysis_running and
                not self._is_request_running)

    @Property(bool, notify=canCancelAnalysisChanged)
    def canCancelAnalysis(self) -> bool: return self._is_analysis_running

    @Property(bool, notify=canUpdateFromGitChanged)
    def canUpdateFromGit(self) -> bool:
        """
        Возвращает True, если можно запустить обновление из Git.
        """
        return (
            self._model.is_git_repo() and
            bool(self._model.get_project_context()) and # Обновлять можно только существующий контекст
            not self._is_analysis_running and
            not self._is_request_running
        )

    @Property(str, notify=toggleAllButtonPropsChanged)
    def toggleAllButtonText(self) -> str:
        """Возвращает текст для кнопки 'Скрыть/Показать все'."""
        if not self._model.get_chat_history():
            return self.tr("Скрыть все") # Текст по умолчанию

        # Если все сообщения уже исключены, кнопка должна предлагать их показать
        all_excluded = all(msg.get("excluded", False) for msg in self._model.get_chat_history())
        return self.tr("Показать все") if all_excluded else self.tr("Скрыть все")
    
    # --- Свойства видимости ---
    @Property(bool, notify=isChatViewReadyChanged)
    def isChatViewReady(self): return self._is_chat_view_ready
    @Property(bool, notify=settingsVisibilityChanged)
    def settingsVisible(self) -> bool: return self._settings_visible
    @Property(bool, notify=instructionsVisibilityChanged)
    def instructionsVisible(self) -> bool: return self._instructions_visible

    # --- Метод для получения данных чата ---
    def getChatHistoryForView(self) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
        return (self._model.get_chat_history(),
                self._last_api_error,
                self._last_api_intermediate_step)

    # --- Слоты для команд от View ---

    @Slot(str, str)
    def showDiffRequested(self, filename: str, new_code: str):
        """
        Слот для обработки запроса на показ окна сравнения.
        Запрашивает оригинальный код у модели и инициирует показ окна.
        """
        logger.debug(f"ViewModel: Запрос на diff для '{filename}'. Запрашиваем оригинальный код у модели.")
        original_code = self._model.get_original_file_content(filename)

        if original_code is None:
            # Если оригинальный файл не найден в контексте
            error_msg = self.tr("Не удалось найти оригинальное содержимое файла '{0}' в текущем контексте проекта. Невозможно построить сравнение.").format(filename)
            logger.warning(error_msg)
            self.showMessageDialog.emit("warn", self.tr("Ошибка сравнения"), error_msg)
            return

        # Если все хорошо, испускаем сигнал для главного окна
        self.showDiffWindow.emit(original_code, new_code, filename)
    
    def set_initial_session_path(self, filepath: str):
        """Запоминает путь к сессии, переданный при запуске."""
        self._pending_session_load_path = filepath
        # Также передаем сигнал в UI, если он хочет что-то сделать с этой информацией
        self.setInitialSessionPathSignal.emit(filepath)

    @Slot()
    def setChatViewReady(self):
        if not self._is_chat_view_ready:
            logger.info("ChatView готов!")
            self._is_chat_view_ready = True
            self.isChatViewReadyChanged.emit()
            self._update_all_button_states()
            if self._pending_session_load_path:
                self.sessionFileSelectedToOpen(self._pending_session_load_path)
                self._pending_session_load_path = None
    
    @Slot(str)
    def saveGeminiApiKey(self, api_key: str):
        if self._model.save_gemini_api_key(api_key): self.clearApiKeyInput.emit()

    @Slot(str)
    def saveGithubToken(self, token: str):
        if self._model.save_github_token(token): self.clearTokenInput.emit()

    @Slot()
    def startAnalysis(self):
        logger.info("Команда: начать анализ.")
        self._model.start_project_analysis()

    @Slot()
    def cancelAnalysis(self):
        logger.info("Команда: отменить анализ.")
        self._model.cancel_analysis()

    @Slot()
    def startContextUpdate(self):
        """Слот для запуска обновления контекста из Git."""
        logger.info("Команда: обновить контекст из Git.")
        self._model.start_context_update_from_git()

    @Slot(str)
    def sendMessage(self, user_input: str):
        logger.info(f"Команда: отправить сообщение '{user_input[:50]}...'")
        self._last_api_error = None
        self._last_api_intermediate_step = None
        self._model.send_request_to_api(user_input)

    @Slot()
    def cancelRequest(self): logger.info("Команда: отменить запрос к API.") # Логика в модели

    # --- Слоты для управления проектом ---
    @Slot(str)
    def updateProjectType(self, ptype: str): self._model.set_project_type(ptype)
    @Slot(str)
    def updateRepoUrl(self, url: str): self._model.set_repo_url(url.strip())
    @Slot(str)
    def updateSelectedBranch(self, branch: str):
        if branch: self._model.set_repo_branch(branch)
    @Slot()
    def selectLocalPath(self):
        current_path = self._model.get_local_path() or os.path.expanduser("~")
        self.showFileDialog.emit("folder", self.tr("Выберите папку проекта"), current_path)
    @Slot(str)
    def localPathSelected(self, path: str):
        if path: self._model.set_local_path(path)

    # --- Слоты для настроек ---
    @Slot(str)
    def updateModelName(self, name: str): self._model.set_model_name(name)
    @Slot(int)
    def updateMaxTokens(self, value: int): self._model.set_max_tokens(value)
    @Slot(str)
    def updateInstructions(self, text: str): self._model.set_instructions(text)
    @Slot(bool)
    def updateRagEnabled(self, enabled: bool): self._model.set_rag_enabled(enabled)
    @Slot(bool)
    def updateSemanticSearchEnabled(self, enabled: bool): self._model.set_semantic_search_enabled(enabled)
    @Slot(set, str)
    def updateExtensionsFromUi(self, checked_common_set: Set[str], custom_text: str):
        custom_set = {f".{part.lstrip('.')}" for part in re.split(r"[\s,]+", custom_text.strip()) if part.strip() and part != '.'}
        final_extensions_tuple = tuple(sorted(list(checked_common_set.union(custom_set))))
        self._model.set_extensions(final_extensions_tuple)

    # --- Управление сессиями ---
    @Slot()
    def newSession(self): self._model.new_session()
    @Slot()
    def openSession(self): self.showFileDialog.emit("open", self.tr("Открыть сессию"), self.tr("Файлы сессий (*{0})").format(db_manager.SESSION_EXTENSION))
    @Slot(str)
    def sessionFileSelectedToOpen(self, filepath: str):
        if filepath: self._model.load_session(filepath)
    
    @Slot()
    def saveSession(self) -> bool:
        filepath = self._model.get_current_session_filepath()
        if filepath:
            success, saved_path = self._model.save_session()
            if success and saved_path:
                self.sessionSavedSuccessfully.emit(saved_path)
            return success
        else:
            self.saveSessionAs()
            return False

    @Slot()
    def saveSessionAs(self):
        default_name = self.tr("Сессия_{0}{1}").format(datetime.datetime.now().strftime('%Y%m%d_%H%M%S'), db_manager.SESSION_EXTENSION)
        file_filter = self.tr("Файлы сессий (*{0})").format(db_manager.SESSION_EXTENSION)
        self.showFileDialog.emit("save", self.tr("Сохранить сессию как..."), f"{default_name};;{file_filter}")

    @Slot(str)
    def sessionFileSelectedToSave(self, filepath: str):
        if filepath:
            if not filepath.endswith(db_manager.SESSION_EXTENSION):
                filepath += db_manager.SESSION_EXTENSION
            success, saved_path = self._model.save_session(filepath)
            if success and saved_path:
                self.sessionSavedSuccessfully.emit(saved_path)

    @Slot(str, str)
    def saveGeneratedFileRequested(self, filename: str, content: str):
        """
        Слот, который вызывается, когда пользователь нажимает "Сохранить как..."
        в окне чата.
        """
        logger.debug(f"ViewModel: получен запрос на сохранение файла '{filename}'.")
        # Передаем сигнал главному окну, чтобы оно показало диалог
        self.showSaveFileDialogForGeneratedCode.emit(filename, content)

    @Slot(str, str)
    def generatedFileSelectedToSave(self, filepath: str, content: str):
        """
        Слот, который вызывается из MainWindow после того, как пользователь
        выбрал путь для сохранения сгенерированного файла.
        """
        if not filepath:
            logger.debug("ViewModel: Сохранение сгенерированного файла отменено в диалоге.")
            return

        success, message = self._model.save_generated_file(filepath, content)
        
        if success:
            self.statusMessageChanged.emit(message, 5000)
        else:
            self.showMessageDialog.emit("crit", self.tr("Ошибка сохранения файла"), message)

    @Slot()
    def toggleSettings(self):
        self._settings_visible = not self._settings_visible
        self.settingsVisibilityChanged.emit(self._settings_visible)
    @Slot()
    def toggleInstructions(self):
        self._instructions_visible = not self._instructions_visible
        self.instructionsVisibilityChanged.emit(self._instructions_visible)
    @Slot(int)
    def toggleApiExclusion(self, index: int): self._model.toggle_api_exclusion(index)

    @Slot()
    def toggleAllMessagesExclusion(self):
        """Слот для вызова пакетного переключения исключения сообщений."""
        self._model.toggle_all_messages_exclusion()

    # --- Слоты, реагирующие на сигналы Модели ---

    @Slot()
    def _on_project_data_changed(self):
        """Обновляет состояние ViewModel при изменении данных проекта в модели."""
        self.projectTypeChanged.emit(self.projectType)
        self.repoUrlChanged.emit(self.repoUrl)
        self.localPathChanged.emit(self.localPath)
        self.selectedBranchChanged.emit(self.selectedBranch)
        self.availableBranchesChanged.emit(self.availableBranches)
        self._update_all_button_states()

    @Slot(bool, str)
    def _on_gemini_api_key_status_changed(self, loaded: bool, status_message: str):
        self.geminiApiKeyStatusTextChanged.emit()
        self._update_all_button_states()

    @Slot(bool, str)
    def _on_github_token_status_changed(self, loaded: bool, status_message: str):
        self.githubTokenStatusTextChanged.emit()
        self._update_all_button_states()

    @Slot()
    def _on_analysis_started(self):
        self._is_analysis_running = True
        self.analysisStateChanged.emit(True)
        self._update_all_button_states()

    @Slot(int, int, str) # <--- Убедитесь, что этот слот находится здесь
    def _on_analysis_progress_updated(self, processed: int, total: int, file_path: str):
        """Обрабатывает обновление прогресса анализа и форматирует его для статус-бара."""
        progress_text = self.tr("Анализ: {0}/{1} ({2})").format(processed, total, os.path.basename(file_path))
        self.statusMessageChanged.emit(progress_text, 0) # 0 означает, что сообщение не исчезнет автоматически
        self.analysisProgress_for_bar_changed.emit(processed, total)

    @Slot()
    def _on_analysis_finished(self):
        self._is_analysis_running = False
        self.analysisStateChanged.emit(False)
        self._update_all_button_states()
        self.chatUpdateRequired.emit()
        self.canUpdateFromGitChanged.emit()   

    @Slot(str)
    def _on_analysis_error(self, error_message: str):
        self._is_analysis_running = False
        self.analysisStateChanged.emit(False)
        self.showMessageDialog.emit("crit", self.tr("Ошибка анализа"), error_message)
        self._update_all_button_states()

    @Slot()
    def _on_api_request_started(self):
        self._is_request_running = True
        self._last_api_error = None
        self._last_api_intermediate_step = None
        self._update_all_button_states()
        self.chatUpdateRequired.emit()
        self.apiRequestStarted.emit()

    @Slot(str)
    def _on_api_intermediate_step(self, message: str):
        self._last_api_intermediate_step = message
        self.chatUpdateRequired.emit()

    @Slot(str)
    def _on_api_response_received(self, text: str):
        self._last_api_intermediate_step = None

    @Slot(str)
    def _on_api_error_occurred(self, error_message: str):
        self._last_api_error = error_message
        self.chatUpdateRequired.emit()

    @Slot()
    def _on_api_request_finished(self):
        self._is_request_running = False
        self._update_all_button_states()
        self.chatUpdateRequired.emit()

    @Slot(list)
    def _on_history_changed(self, history: List[Dict]):
        self.chatUpdateRequired.emit()
        self.toggleAllButtonPropsChanged.emit()

    @Slot(str, bool)
    def _on_session_state_changed(self, filepath: Optional[str], is_dirty: bool):
        self.isDirtyChanged.emit()
        self.windowTitleChanged.emit()
        self._update_all_button_states()

    @Slot()
    def _on_session_loaded(self):
        logger.info("--- ViewModel: Загрузка/обновление состояния из Модели ---")
        self.projectTypeChanged.emit(self.projectType)
        self.repoUrlChanged.emit(self.repoUrl)
        self.localPathChanged.emit(self.localPath)
        self.selectedBranchChanged.emit(self.selectedBranch)
        self.availableBranchesChanged.emit(self.availableBranches)
        self.modelNameChanged.emit()
        self.maxTokensChanged.emit()
        self.instructionsTextChanged.emit()
        self.ragEnabledChanged.emit(self.ragEnabled)
        self.semanticSearchEnabledChanged.emit(self.semanticSearchEnabled)
        self.availableModelsChanged.emit(self._model.get_available_models())
        self._parse_and_emit_extensions()
        self._update_all_button_states()
        self.chatUpdateRequired.emit()
        self.resetUiForNewSession.emit()
        self.windowTitleChanged.emit()
        self.isDirtyChanged.emit()
        self.toggleAllButtonPropsChanged.emit()

    @Slot(str)
    def _on_session_error(self, error_message: str):
        self.showMessageDialog.emit("crit", self.tr("Ошибка сессии"), error_message)

    @Slot(int, int)
    def _on_token_count_updated(self, current_tokens: int, context_limit: int):
        info = self.tr("Токены промпта: {0} / {1}").format(current_tokens, context_limit)
        self.tokenInfoChanged.emit(info)

    # --- Вспомогательные методы ---
    def _update_all_button_states(self):
        self.canSendChanged.emit()
        self.canCancelRequestChanged.emit()
        self.canAnalyzeChanged.emit()
        self.canCancelAnalysisChanged.emit()
        self.canUpdateFromGitChanged.emit()

    def _parse_and_emit_extensions(self):
        model_extensions = self._model.get_extensions()
        common_set = set()
        custom_list = []
        common_lookup = set(COMMON_EXTENSIONS)
        for ext in model_extensions:
            if ext in common_lookup: common_set.add(ext)
            else: custom_list.append(ext)
        self.checkedExtensionsChanged.emit(common_set, " ".join(sorted(custom_list)))

    # --- Методы для поиска (без изменений) ---
    @Slot(str)
    def startOrUpdateSearch(self, query: str):
        query = query.strip()
        if not query:
            self.clear_search()
        else:
            self._search_query = query
            self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag(0))
            self.searchStatusUpdate.emit(True)

    @Slot()
    def clear_search(self):
        if self._search_query:
            self._search_query = None
            self.clearSearchHighlight.emit()
            self.searchStatusUpdate.emit(False)

    @Slot()
    def find_next(self):
        if self._search_query: self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag(0))

    @Slot()
    def find_previous(self):
        if self._search_query: self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag.FindBackward)

    @Slot(bool)
    def setSearchResultStatus(self, found: bool):
        logger.debug(f"Получен статус поиска от ChatView: Найдено={found}")

        # --- Методы для поиска ---
    @Slot(str)
    def startOrUpdateSearch(self, query: str):
        """Запускает новый поиск или обновляет существующий."""
        query = query.strip()
        if not query:
            self.clear_search()
        else:
            self._search_query = query
            # Отправляем начальный поисковый запрос без флагов (поиск вперед от начала)
            self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag(0))
            self.searchStatusUpdate.emit(True)

    @Slot()
    def clear_search(self):
        """Очищает поиск и подсветку."""
        if self._search_query:
            self._search_query = None
            self.clearSearchHighlight.emit()
            self.searchStatusUpdate.emit(False)

    @Slot()
    def find_next(self):
        """Ищет следующее совпадение."""
        if self._search_query:
            self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag(0))

    @Slot()
    def find_previous(self):
        """Ищет предыдущее совпадение."""
        if self._search_query:
            self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag.FindBackward)

    @Slot(bool)
    def setSearchResultStatus(self, found: bool):
        """
        Слот для обратной связи от ChatView о результатах поиска.
        В данный момент просто логирует, но может быть расширен.
        """
        logger.debug(f"Получен статус поиска от ChatView: Найдено={found}")