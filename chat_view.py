# --- Файл: chat_view.py ---

import os
import json
import logging
from PySide6.QtCore import QObject, Slot, Signal, QUrl, QFileInfo
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineFindTextResult

logger = logging.getLogger(__name__)

class PyBridge(QObject):
    """
    Класс-мостик между Python и JavaScript в QWebEngineView.
    """
    messageApiExclusionToggleRequested = Signal(int)
    saveFileRequested = Signal(str, str) # filename, content

    @Slot(str, str)
    def request_save_file(self, filename, content):
        """Слот, вызываемый из JS для сохранения сгенерированного файла."""
        logger.debug(f"PyBridge: Получен запрос на сохранение файла '{filename}'")
        self.saveFileRequested.emit(filename, content)

    @Slot(str)
    def copy_code_to_clipboard(self, code_text):
        """Слот, вызываемый из JS для копирования кода."""
        logger.debug(f"PyBridge: Получен запрос на копирование кода (длина: {len(code_text)})")
        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(code_text)
            logger.info("PyBridge: Код успешно скопирован в буфер обмена.")
        except Exception as e:
            logger.error(f"PyBridge: Ошибка копирования кода: {e}", exc_info=True)

    @Slot(int)
    def request_toggle_api_exclusion(self, index: int):
        """
        Слот, вызываемый из JS при клике на кнопку переключения статуса исключения сообщения.
        """
        logger.debug(f"PyBridge: Получен запрос на переключение API exclusion для индекса: {index}")
        self.messageApiExclusionToggleRequested.emit(index)

class ChatView(QWebEngineView):
    """
    Основной класс виджета чата, основанный на QWebEngineView.
    """
    searchResultReady = Signal(bool)
    pageLoaded = Signal()

    def __init__(self, view_model, parent=None):
        super().__init__(parent)
        self._view_model = view_model
        self.py_bridge = PyBridge()
        self.channel = QWebChannel(self.page())
        
        self.page().setWebChannel(self.channel)
        self.channel.registerObject("py_bridge", self.py_bridge)
        
        self.py_bridge.messageApiExclusionToggleRequested.connect(self._view_model.toggleApiExclusion)
        self.py_bridge.saveFileRequested.connect(self._view_model.saveGeneratedFileRequested)
        
        self._connect_viewmodel_signals()
        self.pageLoaded.connect(self._view_model.setChatViewReady)
        self.loadFinished.connect(self._on_load_finished)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        html_file_path = os.path.join(script_dir, "chat_template.html")
        local_url = QUrl.fromLocalFile(QFileInfo(html_file_path).absoluteFilePath())
        logger.debug(f"ChatView: Загрузка HTML из {local_url.toString()}")
        self.load(local_url)

    def _connect_viewmodel_signals(self):
        """Подключает сигналы от ViewModel к слотам ChatView."""
        logger.debug("ChatView: Подключение сигналов от ViewModel.")
        self._view_model.performSearch.connect(self._on_perform_search)
        self._view_model.clearSearchHighlight.connect(self._on_clear_search)
        self.searchResultReady.connect(self._view_model.setSearchResultStatus)
        self.page().findTextFinished.connect(self._on_find_text_finished)

    @Slot(bool)
    def _on_load_finished(self, ok):
        """Слот, вызываемый по завершении загрузки страницы."""
        if ok:
            logger.info("ChatView: Страница HTML успешно загружена.")
            self.pageLoaded.emit()
        else:
            error_html = self.tr("<html><body><h1>Ошибка загрузки интерфейса чата</h1><p>Не удалось загрузить chat_template.html</p></body></html>")
            logger.error("ChatView: ОШИБКА загрузки страницы HTML!")
            self.setHtml(error_html)

    @Slot(str, object)
    def _on_perform_search(self, query: str, flags_object):
        """Слот для выполнения поиска по команде от ViewModel."""
        logger.debug(f"ChatView: Выполнение findText для '{query}'")
        flags = flags_object if isinstance(flags_object, QWebEnginePage.FindFlags) else QWebEnginePage.FindFlag(0)
        if self.page():
            self.page().findText(query, flags)
        else:
            logger.error("ChatView: self.page() недоступен для поиска!")

    @Slot()
    def _on_clear_search(self):
        """Слот для очистки подсветки поиска."""
        logger.debug("ChatView: Очистка подсветки findText.")
        if self.page():
            self.page().findText("")
        else:
            logger.error("ChatView: self.page() недоступен для очистки поиска!")

    @Slot(QWebEngineFindTextResult)
    def _on_find_text_finished(self, result: QWebEngineFindTextResult):
        """Слот, вызываемый по завершении операции findText."""
        is_search_active = bool(self._view_model._search_query)
        found = is_search_active and result.numberOfMatches() > 0
        logger.debug(f"ChatView: Сигнал findTextFinished получен. Найдено: {found}")
        self.searchResultReady.emit(found)

    def _run_js(self, js_code: str):
        """Выполняет JavaScript код на странице."""
        if self.page():
            self.page().runJavaScript(js_code)

    def clear_chat(self):
        """Очищает содержимое чата (вызывает JS функцию)."""
        logger.debug("ChatView: Очистка чата (вызов JS).")
        self._run_js("clearChatContent();")

    def add_message(self, role: str, html_content: str, message_index: int, is_excluded: bool, is_last: bool = True):
        """
        Добавляет отрендеренное HTML сообщение в чат.
        Передает в JS объект с переведенными строками.
        """
        logger.debug(f"ChatView: Добавление сообщения (роль: {role}, индекс: {message_index}, исключено: {is_excluded})")
        
        # Готовим словарь с переведенными текстами для JS
        texts = {
            "user_prefix": self.tr("Вы:"),
            "model_prefix": self.tr("ИИ:"),
            "exclude_tooltip": self.tr("Исключить из контекста API"),
            "include_tooltip": self.tr("Включить в контекст API"),
            "spoiler_summary": self.tr("Сообщение исключено из контекста API. Нажмите, чтобы раскрыть."),
            "scroll_top_tooltip": self.tr("К началу этого сообщения"),
            "copy_button_text": self.tr("Копировать код"),
            "copied_button_text": self.tr("Скопировано!"),
            "save_button_text": self.tr("Сохранить как..."),
        }
        
        js_safe_html = json.dumps(html_content)
        js_is_excluded = 'true' if is_excluded else 'false'
        js_texts = json.dumps(texts) # Сериализуем словарь в JSON
        
        # Вызываем JS функцию с дополнительным параметром
        js_code = f'appendMessage("{role}", {js_safe_html}, {message_index}, {js_is_excluded}, {js_texts});'
        self._run_js(js_code)
        
        if is_last:
            self.scroll_to_bottom()

    def add_error_message(self, error_text: str):
        """Добавляет сообщение об ошибке."""
        logger.debug("ChatView: Добавление сообщения об ошибке.")
        js_safe_error = json.dumps(error_text)
        js_code = f'appendErrorMessage({js_safe_error});'
        self._run_js(js_code)
        self.scroll_to_bottom()

    def show_loader(self):
        """Показывает прелоадер."""
        logger.debug("ChatView: Показ лоадера (вызов JS).")
        self._run_js("showLoader();")
        self.scroll_to_bottom()

    def hide_loader(self):
        """Скрывает прелоадер."""
        logger.debug("ChatView: Скрытие лоадера (вызов JS).")
        self._run_js("hideLoader();")

    def scroll_to_bottom(self):
        """Прокручивает чат вниз (вызывает JS)."""
        self._run_js("scrollToBottom();")