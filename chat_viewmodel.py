# --- Файл: chat_viewmodel.py ---

import os
import re
import datetime
import logging
from typing import Optional, List, Dict, Any, Tuple, Set

from PySide6.QtCore import QObject, Signal, Slot, Property, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage

from chat_model import ChatModel, CONTEXT_WINDOW_LIMIT
import db_manager

logger = logging.getLogger(__name__)

COMMON_EXTENSIONS = [".py", ".txt", ".md", ".json", ".html", ".css", ".js", ".yaml", ".yml", ".pdf", ".docx"]

class ChatViewModel(QObject):
    """
    Посредник между View (MainWindow) и Model (ChatModel).
    Предоставляет данные для отображения и обрабатывает команды пользователя.
    """

    # --- Сигналы для обновления свойств View ---
    # Статусы
    geminiApiKeyStatusTextChanged = Signal()
    githubTokenStatusTextChanged = Signal()
    
    # Состояние кнопок и полей
    canSendChanged = Signal()
    canCancelRequestChanged = Signal()
    canAnalyzeChanged = Signal()
    canCancelAnalysisChanged = Signal()

    # Поля настроек
    repoUrlChanged = Signal()
    modelNameChanged = Signal()
    selectedBranchChanged = Signal()
    availableBranchesChanged = Signal(list)
    availableModelsChanged = Signal(list)
    maxTokensChanged = Signal()
    instructionsTextChanged = Signal()
    checkedExtensionsChanged = Signal(set, str)
    
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
    showMessageDialog = Signal(str, str, str)
    clearApiKeyInput = Signal()
    clearTokenInput = Signal()
    resetUiForNewSession = Signal()
    apiRequestStarted = Signal()

    # --- Сигналы для управления поиском в ChatView (без изменений) ---
    performSearch = Signal(str, object)
    clearSearchHighlight = Signal()
    searchStatusUpdate = Signal(bool)

    def __init__(self, model: ChatModel, parent: None = None):
        super().__init__(parent)
        if not isinstance(model, ChatModel):
            raise TypeError("Model must be an instance of ChatModel")
        self._model = model
        # Здесь мы НЕ создаем ChatModel, а получаем уже инициализированный.
        # Язык, загруженный из .env, уже находится внутри self._model.

        # --- Внутреннее состояние ViewModel ---
        self._is_chat_view_ready: bool = False
        self._is_request_running: bool = False
        self._is_analysis_running: bool = False
        self._last_api_error: Optional[str] = None
        self._last_api_intermediate_step: Optional[str] = None
        
        # Настройки UI
        self._settings_visible: bool = False
        self._instructions_visible: bool = True

        # Состояние поиска
        self._search_query: Optional[str] = None

        # Состояние репозитория
        self._repo_url: str = ""
        self._selected_branch: str = ""
        self._available_branches: List[str] = []
        
        # --- Подключение к сигналам Модели ---
        self._connect_model_signals()
        
        # --- Инициализация состояния ViewModel из Модели ---
        self._on_session_loaded()

    def _connect_model_signals(self):
        # Статусы
        self._model.geminiApiKeyStatusChanged.connect(self._on_gemini_api_key_status_changed)
        self._model.githubTokenStatusChanged.connect(self._on_github_token_status_changed)
        self._model.availableModelsChanged.connect(self.availableModelsChanged)
        self._model.repoDataChanged.connect(self._on_repo_data_changed)
        
        # Анализ репозитория
        self._model.analysisStarted.connect(self._on_analysis_started)
        self._model.analysisProgressUpdated.connect(self._on_analysis_progress_updated)
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
    
    @Property(str, notify=repoUrlChanged)
    def repoUrl(self) -> str: return self._repo_url

    @Property(str, notify=selectedBranchChanged)
    def selectedBranch(self) -> str: return self._selected_branch

    @Property(list, notify=availableBranchesChanged)
    def availableBranches(self) -> List[str]: return self._available_branches

    @Property(str, notify=modelNameChanged)
    def modelName(self) -> str: return self._model.get_model_name()
    
    @Property(int, notify=maxTokensChanged)
    def maxTokens(self) -> int: return self._model.get_max_tokens()

    @Property(str, notify=instructionsTextChanged)
    def instructionsText(self) -> str: return self._model.get_instructions()

    @Property(bool, notify=isDirtyChanged)
    def isDirty(self) -> bool: return self._model.is_dirty()

    @Property(str, notify=windowTitleChanged)
    def windowTitle(self) -> str:
        base_title = self.tr("GitGemini Pro")
        session_path = self._model.get_current_session_filepath()
        session_name = (os.path.basename(session_path).replace(db_manager.SESSION_EXTENSION, "") 
                        if session_path 
                        else self.tr("Новая сессия"))
        dirty_indicator = "*" if self._model.is_dirty() else ""
        return f"{base_title} - {session_name}{dirty_indicator}"
        
    # Свойства, управляющие состоянием кнопок
    @Property(bool, notify=canSendChanged)
    def canSend(self) -> bool:
        collection_is_ready = False
        if self._model._current_collection:
            if self._model._vector_db_manager.get_collection_doc_count(self._model._current_collection) > 0:
                collection_is_ready = True

        return (self._is_chat_view_ready and
                self._model._gemini_api_key_loaded and
                self._model._github_token_loaded and
                bool(self._model.get_repo_url()) and
                collection_is_ready and
                not self._is_request_running and
                not self._is_analysis_running)

    @Property(bool, notify=canCancelRequestChanged)
    def canCancelRequest(self) -> bool: return self._is_request_running

    @Property(bool, notify=canAnalyzeChanged)
    def canAnalyze(self) -> bool:
        return (self._is_chat_view_ready and
                self._model._gemini_api_key_loaded and
                self._model._github_token_loaded and
                bool(self._model.get_repo_url()) and
                not self._is_analysis_running and
                not self._is_request_running)

    @Property(bool, notify=canCancelAnalysisChanged)
    def canCancelAnalysis(self) -> bool: return self._is_analysis_running

    # Прочие свойства
    @Property(bool, notify=isChatViewReadyChanged)
    def isChatViewReady(self): return self._is_chat_view_ready
    @Property(bool, notify=settingsVisibilityChanged)
    def settingsVisible(self) -> bool: return self._settings_visible
    @Property(bool, notify=instructionsVisibilityChanged)
    def instructionsVisible(self) -> bool: return self._instructions_visible

    # --- Метод для получения данных чата (RAG) ---
    def getChatHistoryForView(self) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
        return (self._model.get_chat_history(), 
                self._last_api_error, 
                self._last_api_intermediate_step)
        
    # --- Слоты для команд от View ---
    @Slot()
    def setChatViewReady(self):
        if not self._is_chat_view_ready:
            logger.info("ChatView готов!")
            self._is_chat_view_ready = True
            self.isChatViewReadyChanged.emit()
            self._update_all_button_states()

    # Аутентификация
    @Slot(str)
    def saveGeminiApiKey(self, api_key: str):
        if self._model.save_gemini_api_key(api_key): self.clearApiKeyInput.emit()

    @Slot(str)
    def saveGithubToken(self, token: str):
        if self._model.save_github_token(token): self.clearTokenInput.emit()

    # Управление анализом и запросами
    @Slot()
    def startAnalysis(self):
        logger.info("Команда: начать анализ репозитория.")
        self._model.start_repository_analysis()
        
    @Slot()
    def cancelAnalysis(self):
        logger.info("Команда: отменить анализ.")
        self._model.cancel_analysis()

    @Slot(str)
    def sendMessage(self, user_input: str):
        logger.info(f"Команда: отправить сообщение '{user_input[:50]}...'")
        self._last_api_error = None
        self._last_api_intermediate_step = None
        self._model.send_request_to_api(user_input)

    @Slot()
    def cancelRequest(self):
        logger.info("Команда: отменить запрос к API.")
        # Логика отмены находится в модели
    
    # Обновление настроек
    @Slot(str)
    def updateRepoUrl(self, url: str):
        self._model.set_repo_url(url.strip())

    @Slot(str)
    def updateSelectedBranch(self, branch: str):
        if branch:
            self._model.set_repo_branch(branch)
    @Slot(str)
    def updateModelName(self, name: str): self._model.set_model_name(name)
    @Slot(int)
    def updateMaxTokens(self, value: int): self._model.set_max_tokens(value)
    @Slot(str)
    def updateInstructions(self, text: str): self._model.set_instructions(text)
    @Slot(set, str)
    def updateExtensionsFromUi(self, checked_common_set: Set[str], custom_text: str):
        custom_set = {f".{part.lstrip('.')}" for part in re.split(r"[\s,]+", custom_text.strip()) if part.strip() and part != '.'}
        final_extensions_tuple = tuple(sorted(list(checked_common_set.union(custom_set))))
        self._model.set_extensions(final_extensions_tuple)

    # Управление сессиями
    @Slot()
    def newSession(self): self._model.new_session()
    @Slot()
    def openSession(self): self.showFileDialog.emit("open", self.tr("Открыть сессию"), self.tr("Файлы сессий (*{0})").format(db_manager.SESSION_EXTENSION))
    @Slot(str)
    def sessionFileSelectedToOpen(self, filepath: str):
        if filepath: self._model.load_session(filepath)
    @Slot()
    def saveSession(self) -> bool:
        if self._model.get_current_session_filepath():
            success, _ = self._model.save_session()
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
            if not filepath.endswith(db_manager.SESSION_EXTENSION): filepath += db_manager.SESSION_EXTENSION
            self._model.save_session(filepath)

    # Переключение видимости панелей
    @Slot()
    def toggleSettings(self):
        self._settings_visible = not self._settings_visible
        self.settingsVisibilityChanged.emit(self._settings_visible)
    @Slot()
    def toggleInstructions(self):
        self._instructions_visible = not self._instructions_visible
        self.instructionsVisibilityChanged.emit(self._instructions_visible)
        
    # Прочее
    @Slot(int)
    def toggleApiExclusion(self, index: int): self._model.toggle_api_exclusion(index)

    # --- Слоты, реагирующие на сигналы Модели ---

    @Slot(str, str, list)
    def _on_repo_data_changed(self, url: str, branch: str, branches: List[str]):
        """Обновляет состояние ViewModel при изменении данных репозитория в модели."""
        self._repo_url = url
        self._selected_branch = branch
        self._available_branches = branches
        
        self.repoUrlChanged.emit()
        self.selectedBranchChanged.emit()
        self.availableBranchesChanged.emit(branches)
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
        self._update_all_button_states()
        
    @Slot(int, int, str)
    def _on_analysis_progress_updated(self, processed, total, file_path):
        progress_text = self.tr("Анализ: {0}/{1} ({2})").format(processed, total, os.path.basename(file_path))
        self.statusMessageChanged.emit(progress_text, 0)

    @Slot()
    def _on_analysis_finished(self):
        self._is_analysis_running = False
        self._update_all_button_states()
        self.chatUpdateRequired.emit()
        
    @Slot(str)
    def _on_analysis_error(self, error_message: str):
        self._is_analysis_running = False
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

    @Slot(str, bool)
    def _on_session_state_changed(self, filepath: Optional[str], is_dirty: bool):
        self.isDirtyChanged.emit()
        self.windowTitleChanged.emit()
        self._update_all_button_states()
        
    @Slot()
    def _on_session_loaded(self):
        self._checked_common_extensions = set(self._model.get_extensions())
        self._custom_extensions_text = ""
        logger.info("--- ViewModel: Загрузка/обновление состояния из Модели ---")
        self.modelNameChanged.emit()
        self.maxTokensChanged.emit()
        self.instructionsTextChanged.emit()
        self.availableModelsChanged.emit(self._model.get_available_models())
        self._parse_and_emit_extensions()
        self._update_all_button_states()
        self.chatUpdateRequired.emit()
        self.resetUiForNewSession.emit()
        self.windowTitleChanged.emit()
        self.isDirtyChanged.emit()

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
        was_active = self._search_query is not None
        self._search_query = None
        self.clearSearchHighlight.emit()
        if was_active: self.searchStatusUpdate.emit(False)
    
    @Slot()
    def find_next(self):
        if self._search_query:
            logger.debug(f"Команда: найти следующее для '{self._search_query}'")
            self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag(0))
        else:
            logger.warning("find_next вызван без активного запроса")

    @Slot()
    def find_previous(self):
        if self._search_query:
            logger.debug(f"Команда: найти предыдущее для '{self._search_query}'")
            self.performSearch.emit(self._search_query, QWebEnginePage.FindFlag.FindBackward)
        else:
            logger.warning("find_previous вызван без активного запроса")

    @Slot(bool)
    def setSearchResultStatus(self, found: bool):
        logger.debug(f"Получен статус поиска от ChatView: Найдено={found}")