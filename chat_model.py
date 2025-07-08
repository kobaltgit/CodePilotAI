# --- Файл: chat_model.py ---

import os
import re
import json
import logging
import hashlib
from typing import Optional, List, Dict, Any, Tuple, Union

from PySide6.QtCore import QObject, Signal, Slot, QThread, QDir

import db_manager
from dotenv import load_dotenv, set_key, find_dotenv
import google.generativeai as genai
import google.generativeai.types as genai_types
from google.api_core import exceptions as google_exceptions

# Наши модули
from github_manager import GitHubManager
from github.Repository import Repository
from summarizer import SummarizerWorker

logger = logging.getLogger(__name__)
try:
    import docx
except ImportError:
    docx = None
    logging.warning("Библиотека 'python-docx' не найдена. DOCX файлы будут проигнорированы.")
try:
    from PyPDF2 import PdfReader
    from io import BytesIO
except ImportError:
    PdfReader = None
    BytesIO = None
    logging.warning("Библиотека 'PyPDF2' не найдена. PDF файлы будут проигнорированы.")

# Глобальный лимит контекстного окна
CONTEXT_WINDOW_LIMIT = 1048576

# --- Воркер для Gemini API ---
# --- Воркер для Gemini API ---
class GeminiWorker(QThread): # <-- Изменено: наследование от QThread
    response_received = Signal(str) # <-- Изменено: больше не передаем original_prompt
    error_occurred = Signal(str)
    finished_work = Signal() # Изменено с finished на finished_work для ясности

    def __init__(self, api_key: str, model_name: str, prompt_parts: List[Dict[str, Any]], max_output_tokens: int): # <-- Изменено: model_name вместо model: genai.GenerativeModel
        super().__init__()
        self.api_key = api_key
        self.model_name = model_name # <-- Добавлено
        self.prompt_parts = prompt_parts
        self.max_output_tokens_config = max_output_tokens
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        logger.info("GeminiWorker: Получен запрос на отмену.")

    # run() метод теперь не @Slot(), так как это метод QThread.
    # Внутренности run() будут скорректированы в следующем фрагменте
    def run(self):
        full_response_text = ""
        try:
            if self._is_cancelled:
                logger.info("GeminiWorker: Выполнение отменено до начала.") # Изменено на INFO
                self.finished_work.emit()
                return

            genai.configure(api_key=self.api_key)
            # Инициализация модели внутри потока, как в рабочем проекте
            model = genai.GenerativeModel(self.model_name) # <-- Добавлено

            logger.info(f"GeminiWorker: Отправка запроса к Gemini API для модели '{self.model_name}'...") # Изменено: self.model_name
            generation_config = genai_types.GenerationConfig(max_output_tokens=self.max_output_tokens_config)

            response = model.generate_content( # <-- Изменено: используем локальную 'model'
                self.prompt_parts,
                generation_config=generation_config,
                request_options={"timeout": 180},
            )
            logger.info(f"GeminiWorker: Получен ответ от Gemini API.") # Изменено на INFO

            if self._is_cancelled:
                logger.info("GeminiWorker: Выполнение отменено во время работы.") # Изменено на INFO
                self.finished_work.emit()
                return

            if hasattr(response, "text") and response.text:
                logger.info(f"GeminiWorker: Ответ получен. Длина текста: {len(response.text)}.") # Изменено на INFO
                self.response_received.emit(response.text) # <-- Изменено: больше не передаем original_prompt
            else:
                reason = "Неизвестно"
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    reason = response.prompt_feedback.block_reason.name
                err_msg = self.tr("Генерация прервана. Причина: {0}").format(reason)
                logger.warning(f"GeminiWorker: {err_msg}. Debug response: {response}")
                self.error_occurred.emit(err_msg)

        except Exception as e:
            if not self._is_cancelled:
                err_msg = self.tr("Ошибка API Gemini: {0} - {1}").format(type(e).__name__, e)
                logger.error(err_msg, exc_info=True)
                self.error_occurred.emit(err_msg)
        finally:
            logger.info("GeminiWorker: Завершение работы.") # Изменено на INFO
            self.finished_work.emit()
            self.deleteLater() # <-- Добавлено: воркер сам удаляет себя после завершения


# --- НОВЫЙ Воркер для асинхронной загрузки/инициализации объекта Gemini GenerativeModel ---
# class GeminiModelLoaderWorker(QObject):
#     """
#     Воркер для асинхронной загрузки/инициализации объекта Gemini GenerativeModel.
#     """
#     model_loaded = Signal(object) # Передает объект genai.GenerativeModel
#     error_occurred = Signal(str)
#     finished = Signal()

#     def __init__(self, api_key: str, model_name: str):
#         super().__init__()
#         self._api_key = api_key
#         self._model_name = model_name

#     @Slot()
#     def run(self):
#         try:
#             logger.info(f"GeminiModelLoaderWorker: Инициализация модели '{self._model_name}'...")
#             genai.configure(api_key=self._api_key)
#             model = genai.GenerativeModel(self._model_name)
#             self.model_loaded.emit(model)
#             logger.info(f"GeminiModelLoaderWorker: Модель '{self._model_name}' успешно инициализирована.")
#         except Exception as e:
#             err_msg = self.tr("Ошибка при инициализации модели Gemini '{0}': {1}").format(self._model_name, e)
#             logger.error(f"GeminiModelLoaderWorker: {err_msg}", exc_info=True)
#             self.error_occurred.emit(err_msg)
#         finally:
#             self.finished.emit()

# --- НОВЫЙ Воркер для асинхронного получения списка доступных моделей Gemini ---
# class GeminiModelsListWorker(QObject):
#     """
#     Воркер для асинхронного получения списка доступных моделей Gemini.
#     """
#     models_list_ready = Signal(list)
#     error_occurred = Signal(str)
#     finished = Signal()

#     def __init__(self, api_key: str):
#         super().__init__()
#         self._api_key = api_key

#     @Slot()
#     def run(self):
#         try:
#             logger.info("GeminiModelsListWorker: Получение списка моделей...")
#             # Убедимся, что API ключ сконфигурирован в этом потоке
#             genai.configure(api_key=self._api_key)
#             models = genai.list_models()
#             available_models = sorted([m.name.replace("models/", "") for m in models if 'generateContent' in m.supported_generation_methods])
#             self.models_list_ready.emit(available_models)
#             logger.info(f"GeminiModelsListWorker: Список моделей ({len(available_models)}) успешно получен.")
#         except Exception as e:
#             err_msg = self.tr("Ошибка при получении списка моделей Gemini: {0}").format(e)
#             logger.error(f"GeminiModelsListWorker: {err_msg}", exc_info=True)
#             self.error_occurred.emit(err_msg)
#         finally:
#             self.finished.emit()

class ChatModel(QObject):
    # --- Сигналы ---
    geminiApiKeyStatusChanged = Signal(bool, str)
    githubTokenStatusChanged = Signal(bool, str)
    availableModelsChanged = Signal(list)
    projectDataChanged = Signal()
    analysisStarted = Signal()
    analysisProgressUpdated = Signal(int, int, str)
    analysisFinished = Signal()
    analysisError = Signal(str)
    historyChanged = Signal(list)
    sessionStateChanged = Signal(str, bool)
    sessionLoaded = Signal()
    sessionError = Signal(str)
    fileSummariesChanged = Signal(dict)
    apiRequestStarted = Signal()
    apiResponseReceived = Signal(str)
    apiIntermediateStep = Signal(str)
    apiErrorOccurred = Signal(str)
    apiRequestFinished = Signal()
    statusMessage = Signal(str, int)
    tokenCountUpdated = Signal(int, int)

    def __init__(self, app_lang: str = 'en', parent=None):
        super().__init__(parent)
        self._app_language = app_lang

        # --- Состояние аутентификации ---
        self._dotenv_path: Optional[str] = find_dotenv()
        self._gemini_api_key: Optional[str] = None
        self._github_token: Optional[str] = None
        self._gemini_api_key_loaded: bool = False
        self._github_token_loaded: bool = False

        # --- Состояние сессии ---
        # Общие для всех типов проектов
        self._chat_history: List[Dict[str, Any]] = []
        self._project_context: List[Dict[str, Any]] = [] # Теперь хранит как саммари, так и чанки/полные файлы
        self._file_summaries_for_display: Dict[str, str] = {} # <-- ДОБАВЛЕНО: Для отображения саммари
        self._current_session_filepath: Optional[str] = None
        self._is_dirty: bool = False
        # Специфичные для проекта
        self._project_type: Optional[str] = None  # 'github' или 'local'
        self._repo_url: Optional[str] = None
        self._repo_branch: Optional[str] = None
        self._local_path: Optional[str] = None
        # Вспомогательные
        self._repo_object: Optional[Repository] = None
        self._available_branches: List[str] = []

        # --- Настройки ---
        self._model_name: str = "gemini-1.5-flash-latest" # Обновлено на актуальную
        self._available_models: List[str] = [self._model_name] # Изначально, пока не загружен список
        self._max_output_tokens: int = 65536
        self._extensions: Tuple[str, ...] = tuple()
        self._instructions: str = ""
        self._rag_enabled: bool = True # RAG включен по умолчанию

        # --- Состояние токенов ---
        self._current_prompt_tokens: int = 0
        self._token_limit_for_display: int = CONTEXT_WINDOW_LIMIT

        # --- Воркеры и менеджеры ---
        # self._gemini_model: Optional[genai.GenerativeModel] = None # Объект модели Gemini - Удален
        self._analysis_worker: Optional[SummarizerWorker] = None
        self._analysis_thread: Optional[QThread] = None
        self._github_manager: Optional[GitHubManager] = None
        # self._gemini_worker_thread: Optional[QThread] = None - Удален, теперь GeminiWorker сам поток
        self._gemini_worker: Optional[GeminiWorker] = None # <-- Добавлено: ссылка на активный GeminiWorker

        # Удалены _gemini_loader_thread, _gemini_loader_worker, _gemini_models_list_thread, _gemini_models_list_worker

        self._load_credentials()
        self.new_session()

    # --- Управление Ключами и Токенами ---
    def _load_credentials(self):
        load_dotenv(dotenv_path=self._dotenv_path, override=True)
        self._gemini_api_key = os.getenv("GEMINI_API_KEY")
        self._gemini_api_key_loaded = bool(self._gemini_api_key)
        status = self.tr("Загружен") if self._gemini_api_key_loaded else self.tr("Не найден!")
        self.geminiApiKeyStatusChanged.emit(self._gemini_api_key_loaded, self.tr("Ключ API: {0}").format(status))

        # Теперь _initialize_gemini() выполняет синхронную инициализацию и получение списка моделей
        # Вся логика обработки отсутствия ключа или ошибок также находится внутри _initialize_gemini.
        self._initialize_gemini() 

        self._github_token = os.getenv("GITHUB_TOKEN")
        self._github_token_loaded = bool(self._github_token)
        status = self.tr("Загружен") if self._github_token_loaded else self.tr("Не найден!")
        self.githubTokenStatusChanged.emit(self._github_token_loaded, self.tr("Токен GitHub: {0}").format(status))
        if self._github_token_loaded: self._initialize_github_manager()


    def _save_credential(self, key_name: str, value: str) -> bool:
        if not value: return False
        try:
            path = self._dotenv_path or os.path.join(os.getcwd(), ".env")
            set_key(path, key_name, value, quote_mode="always")
            self._dotenv_path = path; self._load_credentials()
            self.statusMessage.emit(self.tr("{0} успешно сохранен.").format(key_name), 5000); return True
        except Exception as e:
            self.statusMessage.emit(self.tr("Ошибка сохранения: {0}").format(e), 0); return False

    def save_gemini_api_key(self, key: str) -> bool: return self._save_credential("GEMINI_API_KEY", key)
    def save_github_token(self, token: str) -> bool: return self._save_credential("GITHUB_TOKEN", token)

    # --- Инициализация сервисов ---
    def _initialize_gemini(self):
        """
        Инициализирует глобальный API клиент Gemini и получает список доступных моделей.
        Вызывается при загрузке ключа API или смене модели.
        """
        if not self._gemini_api_key:
            self.geminiApiKeyStatusChanged.emit(False, self.tr("Ключ API: Отсутствует"))
            self.statusMessage.emit(self.tr("Ключ API Gemini отсутствует."), 5000)
            # Сбрасываем доступность моделей, если нет ключа
            self._available_models = [] 
            self.availableModelsChanged.emit([])
            return

        try:
            genai.configure(api_key=self._gemini_api_key)

            # Синхронное получение списка моделей
            logger.info("ChatModel: Получение списка моделей Gemini...")
            models = genai.list_models()
            self._available_models = sorted([m.name.replace("models/", "") for m in models if 'generateContent' in m.supported_generation_methods])
            self.availableModelsChanged.emit(self._available_models)
            logger.info(f"ChatModel: Список моделей ({len(self._available_models)}) успешно получен.")

            # Проверка, что выбранная модель доступна
            if self._model_name not in self._available_models:
                if self._available_models:
                    old_model_name = self._model_name
                    self._model_name = self._available_models[0] # Выбираем первую доступную
                    logger.warning(self.tr("Выбранная модель '{0}' недоступна. Установлена первая доступная: '{1}'.").format(old_model_name, self._model_name))
                    self.statusMessage.emit(self.tr("Модель '{0}' недоступна. Установлена: '{1}'.").format(old_model_name, self._model_name), 5000)
                else:
                    self.statusMessage.emit(self.tr("Нет доступных моделей Gemini."), 5000)
                    self._model_name = "" # Очищаем имя модели, если нет доступных

            self.geminiApiKeyStatusChanged.emit(True, self.tr("Ключ API: Загружен"))
            self.statusMessage.emit(self.tr("API Gemini сконфигурирован."), 3000)

        except Exception as e:
            err_msg = self.tr("Ошибка конфигурации API Gemini или получения списка моделей: {0}").format(e)
            logger.error(err_msg, exc_info=True)
            self.geminiApiKeyStatusChanged.emit(False, self.tr("Ключ API: Ошибка!"))
            self.statusMessage.emit(err_msg, 0)
            self._available_models = []
            self._model_name = ""
            self.availableModelsChanged.emit([])

    # Этот метод остается без изменений
    def _initialize_github_manager(self):
        if not self._github_token: return
        self._github_manager = GitHubManager(self._github_token)
        rate_info = self._github_manager.rate_limit_info if self._github_manager.is_authenticated() else self.tr("Ошибка!")
        self.githubTokenStatusChanged.emit(self._github_manager.is_authenticated(), self.tr("Токен GitHub: {0}").format(rate_info))

    # Удалены все слоты, которые были ниже (on_gemini_model_loaded, _on_gemini_model_loading_error, _fetch_available_models, _cleanup_gemini_loader, _clear_gemini_loader_references, _cleanup_gemini_models_list, _clear_models_list_references, _on_models_list_received, _on_models_list_error)

    # --- Анализ проекта (полностью переработано) ---
    def start_project_analysis(self):
        """Запускает анализ проекта, независимо от его типа (GitHub или локальный)."""
        is_ready, error_msg = self._is_ready_for_analysis()
        if not is_ready:
            self.analysisError.emit(error_msg)
            return

        if self._analysis_thread and self._analysis_thread.isRunning():
            self.statusMessage.emit(self.tr("Анализ уже запущен."), 3000)
            return

        # 1. Очищаем старый контекст
        self._clear_project_context()

        # 2. Получаем ПУТИ к файлам
        self.statusMessage.emit(self.tr("Поиск файлов для анализа..."), 0)
        file_paths = self._get_file_paths_for_analysis()
        if not file_paths:
            self.analysisError.emit(self.tr("Не найдено подходящих файлов для анализа. Проверьте путь и выбранные расширения."))
            return

        # 3. Запускаем SummarizerWorker
        self.analysisStarted.emit()
        self.statusMessage.emit(self.tr("Начат анализ {0} файлов...").format(len(file_paths)), 0)

        # Создаем SummarizerWorker, передавая ему пути, а не контент
        self._analysis_worker = SummarizerWorker(
            file_paths=file_paths, # <-- ИЗМЕНЕНО
            project_type=self._project_type, # <-- ДОБАВЛЕНО
            project_source_path=self._local_path or self._repo_url, # <-- ДОБАВЛЕНО
            repo_object=self._repo_object, # <-- ДОБАВЛЕНО
            repo_branch=self._repo_branch, # <-- ДОБАВЛЕНО
            github_manager=self._github_manager, # <-- ДОБАВЛЕНО
            rag_enabled=self._rag_enabled,
            gemini_api_key=self._gemini_api_key,
            model_name=self._model_name,
            app_lang=self._app_language
        )

        # Подключение сигналов (остается без изменений)
        self._analysis_worker.context_data_ready.connect(self._on_context_data_ready)
        self._analysis_worker.file_summarized.connect(self._on_file_summarized)
        self._analysis_worker.progress_updated.connect(self.analysisProgressUpdated)
        self._analysis_worker.error_occurred.connect(self.analysisError)
        self._analysis_worker.finished.connect(self._on_analysis_finished)
        self._analysis_worker.finished.connect(lambda: setattr(self, '_analysis_worker', None))
        self._analysis_worker.finished.connect(lambda: setattr(self, '_analysis_thread', None))

        self._analysis_thread = self._analysis_worker
        self._analysis_thread.start()


    def _is_ready_for_analysis(self) -> Tuple[bool, str]:
        """Проверяет, все ли готово к запуску анализа."""
        if not self._gemini_api_key_loaded and self._rag_enabled:
            return False, self.tr("Ключ API Gemini необходим для анализа в режиме RAG.")
        if self._project_type == 'github':
            if not self._github_token_loaded: return False, self.tr("Токен GitHub не загружен.")
            if not self._repo_object or not self._repo_branch: return False, self.tr("Репозиторий или ветка не выбраны.")
        elif self._project_type == 'local':
            if not self._local_path or not os.path.isdir(self._local_path): return False, self.tr("Выбрана некорректная локальная папка.")
        else:
            return False, self.tr("Тип проекта не определен.")
        return True, ""

    def _get_file_paths_for_analysis(self) -> List[str]:
        """Собирает список путей к файлам из GitHub или локальной папки."""
        file_paths = []
        if not self._extensions:
            logger.warning("Список расширений для анализа пуст. Файлы не будут собраны.")
            return []

        if self._project_type == 'github':
            if not self._github_manager:
                logger.error("GitHubManager не инициализирован. Невозможно получить файлы.")
                return []
            # get_repo_file_tree возвращает dict {path: size}, нам нужны только ключи
            files_to_process, _ = self._github_manager.get_repo_file_tree(self._repo_object, self._repo_branch, self._extensions)
            file_paths = list(files_to_process.keys())

        elif self._project_type == 'local':
            ignored_dirs = {"venv", ".venv", "__pycache__", ".git", ".vscode", ".idea", "node_modules"}
            for root, dirs, files in os.walk(self._local_path, topdown=True):
                dirs[:] = [d for d in dirs if d not in ignored_dirs]
                for file in files:
                    if file.lower().endswith(self._extensions):
                        full_path = os.path.join(root, file)
                        # Для локальных файлов мы будем использовать полный путь
                        file_paths.append(full_path)
        
        logger.info(f"Найдено {len(file_paths)} файлов для анализа.")
        return file_paths

    def cancel_analysis(self):
        if self._analysis_thread and self._analysis_thread.isRunning():
            if self._analysis_worker: self._analysis_worker.cancel()
            self.statusMessage.emit(self.tr("Отмена анализа..."), 3000)

    @Slot(list)
    def _on_context_data_ready(self, context_data_batch: List[Dict[str, Any]]):
        """Принимает пакет данных от воркера и добавляет в общий контекст."""
        self._project_context.extend(context_data_batch)
        self._mark_dirty()

    @Slot(str, str)
    def _on_file_summarized(self, file_path: str, summary: str):
        """Обновляет словарь саммари для отображения в UI."""
        self._file_summaries_for_display[file_path] = summary # <-- Напрямую добавляем в новый словарь
        self.fileSummariesChanged.emit(self._file_summaries_for_display) # <-- Эмитируем весь словарь


    @Slot()
    def _on_analysis_finished(self):
        self.analysisFinished.emit()
        self.statusMessage.emit(self.tr("Анализ проекта завершен."), 5000)
        # После завершения анализа обновляем словарь саммари целиком
        # Используем уже собранный _file_summaries_for_display
        self.fileSummariesChanged.emit(self._file_summaries_for_display)

        # Обнуляем ссылки после завершения работы потока
        self._analysis_worker = None
        self._analysis_thread = None

    # --- RAG и основной запрос к API ---
    def send_request_to_api(self, user_input: str):
        # Проверки на готовность
        if not self._is_ready_for_request(): return

        # Проверка на пустой ввод пользователя
        user_input_stripped = user_input.strip()
        if not user_input_stripped:
            self.statusMessage.emit(self.tr("Введите ваш запрос."), 3000)
            return

        self.add_user_message(user_input_stripped) # Добавляем сообщение пользователя в историю
        self.apiRequestStarted.emit() # Сигнал для UI (показ лоадера)

        self.statusMessage.emit(self.tr("Формирование промпта..."), 0) # Обновление статуса

        final_prompt_parts = self._build_final_prompt() # Строим финальный промпт

        if not final_prompt_parts:
            # _build_final_prompt уже должен был отправить apiErrorOccurred и apiRequestFinished
            return

        # Запуск GeminiWorker (он теперь сам QThread)
        self.statusMessage.emit(self.tr("Отправка запроса ({0} т.)...").format(self._current_prompt_tokens), 0)

        # Безопасная остановка предыдущего воркера, если он вдруг активен (не должно быть благодаря _is_ready_for_request)
        if self._gemini_worker and self._gemini_worker.isRunning():
            self._gemini_worker.cancel() # Запрашиваем отмену
            self._gemini_worker.wait(1000) # Ждем немного (1 секунда) для завершения
            if self._gemini_worker.isRunning():
                logger.warning("GeminiWorker не завершился после отмены. Принудительное удаление.")
                self._gemini_worker.terminate()
                self._gemini_worker.wait(1000)
            # Обнуление ссылок произойдет в _worker_thread_finished

        # Создаем НОВЫЙ GeminiWorker (который сам является потоком)
        self._gemini_worker = GeminiWorker(
            api_key=self._gemini_api_key,
            model_name=self._model_name,
            prompt_parts=final_prompt_parts,
            max_output_tokens=self._max_output_tokens,
        )

        # Подключение сигналов от GeminiWorker
        self._gemini_worker.response_received.connect(self._on_api_response_received)
        self._gemini_worker.error_occurred.connect(self._handle_final_api_error)
        # self._gemini_worker.finished_work.connect(self._handle_worker_finished) # <-- Удалена эта строка
        self._gemini_worker.finished_work.connect(self.apiRequestFinished) # <-- Добавлена эта строка: finish_work напрямую эмитирует apiRequestFinished
        # self._gemini_worker.finished.connect(self._worker_thread_finished) # Сигнал QThread, что поток завершен
        self._gemini_worker.finished_work.connect(lambda: setattr(self, '_gemini_worker', None)) # <-- Добавлено обнуление ссылки

        self._gemini_worker.start() # Запускаем поток


    def _is_ready_for_request(self) -> bool:
        if self._gemini_worker and self._gemini_worker.isRunning(): # <-- Изменено: проверяем self._gemini_worker
            self.statusMessage.emit(self.tr("Дождитесь завершения предыдущего запроса."), 3000); return False
        if not self._gemini_api_key_loaded: # <-- Изменено: удалена проверка self._gemini_model
            self.apiErrorOccurred.emit(self.tr("Ключ API не загружен.")); return False
        return True


    def _build_final_prompt(self) -> List[Dict[str, Any]]:
        # Функция для очистки сообщений: гарантируем, что 'parts' - это список строк
        def clean_message(msg: Dict[str, Any]) -> Dict[str, Any]:
            cleaned_parts = [str(p) for p in msg.get("parts", []) if p is not None]
            return {"role": msg["role"], "parts": cleaned_parts}

        # Временная модель для подсчета токенов. Инициализация внутри, т.к. _gemini_model не хранится.
        try:
            genai.configure(api_key=self._gemini_api_key)
            temp_model = genai.GenerativeModel(self._model_name)
        except Exception as e:
            logger.error(self.tr("Ошибка инициализации временной модели для подсчета токенов: {0}").format(e), exc_info=True)
            self.apiErrorOccurred.emit(self.tr("Ошибка подготовки промпта: не удалось инициализировать модель для подсчета токенов."))
            return []

        # Вспомогательная функция для подсчета токенов с использованием временной модели
        def _count_tokens_helper(parts_list: List[Dict[str, Any]]) -> Optional[int]:
            if not parts_list: return 0
            try:
                # API Gemini требует чередования ролей для count_tokens
                # Простая очистка для подсчета: берем только первые два, если их много, или все, если их мало.
                temp_prompt = []
                last_role = None
                for msg in parts_list:
                    if not msg.get("role") or not msg.get("parts"): continue
                    if last_role == msg["role"] and len(temp_prompt) > 0:
                        # Если роли совпадают, и это не начало промпта,
                        # заменяем последнее сообщение на текущее, чтобы сохранить чередование.
                        # (Это упрощение для подсчета, не для фактического промпта)
                        temp_prompt[-1] = clean_message(msg)
                    else:
                        temp_prompt.append(clean_message(msg))
                        last_role = msg["role"]

                # Дополнительная проверка, чтобы убедиться, что roles чередуются
                if len(temp_prompt) > 1:
                    for i in range(len(temp_prompt) - 1):
                        if temp_prompt[i]["role"] == temp_prompt[i+1]["role"]:
                            # Если есть последовательные одинаковые роли, это все равно может сломать count_tokens
                            # В этом случае, делаем грубую оценку.
                            logger.warning(self.tr("Внутренняя ошибка: роли не чередуются для подсчета токенов. Грубая оценка."))
                            return sum(len(p) for msg in parts_list for p in msg.get("parts", [])) // 4 # Грубая оценка

                return temp_model.count_tokens(temp_prompt).total_tokens
            except google_exceptions.InvalidArgument as e:
                logger.warning(self.tr("Ошибка InvalidArgument при подсчете токенов: {0}. Использование грубой оценки.").format(e))
                return sum(len(p) for msg in parts_list for p in msg.get("parts", [])) // 4 # Грубая оценка
            except Exception as e:
                logger.error(self.tr("Ошибка подсчета токенов: {0}. Использование грубой оценки.").format(e), exc_info=True)
                return sum(len(p) for msg in parts_list for p in msg.get("parts", [])) // 4 # Грубая оценка


        prompt_token_budget = CONTEXT_WINDOW_LIMIT - self._max_output_tokens
        current_tokens = 0

        final_prompt_parts = [] # Это будет итоговый промпт для отправки в API

        # 1. Системные инструкции
        instructions_text = self._build_system_instructions()
        instructions_part = [
            {"role": "user", "parts": [instructions_text]},
            {"role": "model", "parts": [self.tr("OK. Я готов к работе.")]}
        ]

        instr_tokens = _count_tokens_helper(instructions_part)
        if instr_tokens is None: # Если подсчет токенов вернул None, значит ошибка.
            self.apiErrorOccurred.emit(self.tr("Ошибка: Не удалось подсчитать токены для системных инструкций."))
            return []

        if current_tokens + instr_tokens < prompt_token_budget:
            final_prompt_parts.extend(instructions_part)
            current_tokens += instr_tokens
        else:
            self.apiErrorOccurred.emit(self.tr("Системные инструкции слишком длинные и не помещаются в контекст."))
            return []

        # 2. Контекст проекта (если есть и RAG включен)
        if self._project_context:
            context_str = self._build_context_string(prompt_token_budget - current_tokens)
            if context_str:
                context_part = [
                    {"role": "user", "parts": [self.tr("**Контекст из файлов проекта:**\n{0}").format(context_str)]},
                    {"role": "model", "parts": [self.tr("OK. Контекст получен.")]}
                ]
                context_tokens = _count_tokens_helper(context_part)
                if context_tokens is None:
                    logger.warning(self.tr("Ошибка: Не удалось подсчитать токены для контекста проекта. Контекст пропущен."))
                    self.apiIntermediateStep.emit(self.tr("Ошибка подсчета токенов для контекста. Контекст проекта пропущен."))
                elif current_tokens + context_tokens < prompt_token_budget:
                    final_prompt_parts.extend(context_part)
                    current_tokens += context_tokens
                else:
                    self.apiIntermediateStep.emit(self.tr("Контекст проекта частично исключен из-за превышения лимита токенов."))
                    # Продолжаем без этого контекста, если он не влез.


        # 3. История чата
        # Все, кроме последнего запроса пользователя.
        # Исключенные сообщения пропускаются.
        history_to_consider = self._chat_history[:-1] 
        last_user_message = self._chat_history[-1]

        history_part = []
        # Добавляем сообщения из истории, начиная с последних, пока есть место
        for message in reversed(history_to_consider):
            if message.get("excluded", False):
                continue
            cleaned_message = clean_message(message)
            message_tokens = _count_tokens_helper([cleaned_message])
            if message_tokens is None:
                logger.warning(self.tr("Ошибка: Не удалось подсчитать токены для сообщения истории. Сообщение пропущено."))
                self.apiIntermediateStep.emit(self.tr("Ошибка подсчета токенов для истории. Некоторые сообщения могут быть пропущены."))
                continue

            if current_tokens + message_tokens <= prompt_token_budget:
                history_part.insert(0, cleaned_message); current_tokens += message_tokens
            else:
                self.apiIntermediateStep.emit(self.tr("Часть истории чата исключена из-за превышения лимита токенов."))
                break # Добавлять больше не можем, бюджет исчерпан

        final_prompt_parts.extend(history_part)

        # 4. Последний запрос пользователя
        cleaned_last_message = clean_message(last_user_message)
        last_message_tokens = _count_tokens_helper([cleaned_last_message])
        if last_message_tokens is None:
            self.apiErrorOccurred.emit(self.tr("Ошибка подсчета токенов для вашего запроса. Возможно, запрос слишком большой или внутренняя ошибка."))
            return []

        # Если последний запрос пользователя не помещается, это критическая ошибка,
        # так как пользователь не получит ответа на свой текущий вопрос.
        if (current_tokens + last_message_tokens > prompt_token_budget):
            logger.error(self.tr("Промпт превышает лимит токенов: текущий={0}, последний_запрос={1}, бюджет={2}").format(current_tokens, last_message_tokens, prompt_token_budget))
            self.apiErrorOccurred.emit(self.tr("Недостаточно места для вашего запроса в контекстном окне. Попробуйте исключить сообщения из истории или уменьшить инструкции."))
            return []

        final_prompt_parts.append(cleaned_last_message)
        current_tokens += last_message_tokens

        # Применяем строгую очистку чередования ролей (как в рабочем проекте)
        final_cleaned_messages_for_api = []
        last_role = None
        for msg in final_prompt_parts:
            current_role = msg.get("role")
            if not current_role: continue # Пропускаем сообщения без роли

            if not final_cleaned_messages_for_api: # Если это первое сообщение
                final_cleaned_messages_for_api.append(msg)
                last_role = current_role
                continue

            if current_role == last_role:
                if current_role == "user":
                    # Два user подряд: ЗАМЕНЯЕМ предыдущий текущим (оставляем ПОЗДНИЙ)
                    logger.debug(self.tr("Очистка промпта: Замена предыдущего USER на текущий: {0}...").format(str(msg.get('parts', [''])[0])[:50]))
                    final_cleaned_messages_for_api[-1] = msg
                elif current_role == "model":
                    # Два model подряд: ПРОПУСКАЕМ текущий (оставляем ПЕРВЫЙ)
                    logger.debug(self.tr("Очистка промпта: Пропуск дублирующегося MODEL: {0}...").format(str(msg.get('parts', [''])[0])[:50]))
                    pass # Игнорируем текущее сообщение msg
            else:
                # Роли чередуются, добавляем текущее сообщение
                final_cleaned_messages_for_api.append(msg)
                last_role = current_role

        # Если финальный промпт оказался пустым после всех фильтров
        if not final_cleaned_messages_for_api:
             self.apiErrorOccurred.emit(self.tr("Не удалось сформировать промпт. Возможно, слишком большой объем данных или внутренняя ошибка."))
             return []

        # Обновляем токен каунт на основе финально очищенного промпта
        # Проверяем, что temp_model доступна для финального подсчета.
        if temp_model and self._gemini_api_key:
            # Используем temp_model для финального подсчета токенов очищенного промпта
            try:
                self._current_prompt_tokens = temp_model.count_tokens(final_cleaned_messages_for_api).total_tokens
            except Exception as e:
                logger.error(self.tr("Финальная ошибка подсчета токенов для очищенного промпта: {0}. Использование грубой оценки.").format(e), exc_info=True)
                self._current_prompt_tokens = sum(len(p) for msg in final_cleaned_messages_for_api for p in msg.get("parts", [])) // 4
        else:
             self._current_prompt_tokens = sum(len(p) for msg in final_cleaned_messages_for_api for p in msg.get("parts", [])) // 4

        self.tokenCountUpdated.emit(self._current_prompt_tokens, CONTEXT_WINDOW_LIMIT)
        return final_cleaned_messages_for_api # Возвращаем очищенный промпт

    def _build_system_instructions(self) -> str:
        """
        Собирает текст системных инструкций, адаптируя его под текущую задачу
        (анализ проекта или общий чат).
        """
        lang_instruction_phrase = self.tr("на русском языке") if self._app_language == 'ru' else "in English"
        base_instructions = ""

        # Выбираем базовый промпт в зависимости от наличия контекста проекта
        if self._project_context:
            # Промпт для режима анализа кода
            base_instructions = self.tr(
                "Ты — мой ассистент по программированию и другим направлениям, это зависит от контекста. Анализируй предоставленный контекст и отвечай на вопросы. "
                "Отвечай {0}, если не указано иное. При ответе всегда используй Pygments для подсветки кода."
            ).format(lang_instruction_phrase)
        else:
            # Промпт для режима общего чата
            base_instructions = self.tr(
                "Ты — полезный и разносторонний ассистент. Отвечай на вопросы четко и по делу. "
                "Отвечай {0}, если не указано иное."
            ).format(lang_instruction_phrase)

        # Добавляем пользовательские инструкции, если они есть
        if self._instructions.strip():
            user_instructions_header = self.tr("Дополнительные инструкции:")
            return f"{base_instructions}\n\n{user_instructions_header}\n{self._instructions.strip()}"
        
        return base_instructions

    def _build_context_string(self, remaining_budget_tokens: int) -> str:
        """
        Собирает строку контекста из self._project_context, не превышая бюджет токенов.
        Корректно обрабатывает режимы с включенным и выключенным RAG.
        """
        if not self._project_context:
            return ""

        context_parts_list = []
        # Грубый коэффициент перевода токенов в символы для предварительной оценки
        CHAR_PER_TOKEN_APPROX = 4
        char_budget = remaining_budget_tokens * CHAR_PER_TOKEN_APPROX
        current_total_text_length = 0

        # Собираем данные в зависимости от режима RAG
        items_to_add = []
        if self._rag_enabled:
            # Режим RAG: Сначала все саммари, потом все чанки
            summaries = []
            chunks = []
            for item in self._project_context:
                if item['type'] == 'summary':
                    header = self.tr("--- Обзор файла: {0} ---\n").format(item['file_path'])
                    summaries.append(header + item['content'] + "\n\n")
                elif item['type'] == 'chunk':
                    header = self.tr("--- Фрагмент ({0}) из файла: {1} ---\n").format(item.get('chunk_num', 0), item['file_path'])
                    chunks.append(header + item['content'] + "\n\n")
            items_to_add = summaries + chunks
        else:
            # Режим без RAG: только полные файлы
            for item in self._project_context:
                if item['type'] == 'full_file':
                    header = self.tr("--- Содержимое файла: {0} ---\n").format(item['file_path'])
                    items_to_add.append(header + item['content'] + "\n\n")

        # Добавляем собранные части в контекст, пока не исчерпается бюджет
        for text in items_to_add:
            if current_total_text_length + len(text) <= char_budget:
                context_parts_list.append(text)
                current_total_text_length += len(text)
            else:
                logger.warning(self.tr("Часть контекста проекта не поместилась в окно токенов и была обрезана."))
                self.apiIntermediateStep.emit(self.tr("Внимание: Контекст проекта был урезан, так как превышает лимит токенов."))
                break # Бюджет исчерпан

        return "".join(context_parts_list)


    @Slot(str) # <-- Изменено: теперь принимает только str, original_prompt удален
    def _on_api_response_received(self, response_text: str):
        """Обрабатывает ответ от API как обычный текстовый ответ."""
        self.add_model_response(response_text) # <-- Удален _handle_final_api_response
        self.apiResponseReceived.emit(response_text)

    @Slot(str)
    def _handle_final_api_error(self, error_message: str): self.apiErrorOccurred.emit(error_message)

    # --- Управление состоянием (геттеры/сеттеры) ---
    def set_project_type(self, ptype: Optional[str]):
        if ptype != self._project_type: self._mark_dirty()
        self._project_type = ptype

    def set_repo_url(self, url: str):
        if not url or url == self._repo_url: return
        self._mark_dirty()
        self._clear_project_context()
        self._project_type = 'github'
        self._local_path = None
        self._repo_url = url
        
        if not self._github_manager:
            self.statusMessage.emit(self.tr("GitHub не инициализирован."), 5000)
            return
            
        repo_data = self._github_manager.get_repo(url)
        if repo_data:
            self._repo_object, branch_from_url = repo_data
            self._available_branches = self._github_manager.get_available_branches(self._repo_object)
            self._repo_branch = (branch_from_url if branch_from_url in self._available_branches else self._repo_object.default_branch)
        else:
            self._repo_object, self._available_branches, self._repo_branch = None, [], None
            self.statusMessage.emit(self.tr("Не удалось получить репозиторий. Проверьте URL или токен GitHub."), 5000)

        self.projectDataChanged.emit()

    def set_repo_branch(self, branch_name: str):
        if not branch_name or branch_name == self._repo_branch: return
        self._mark_dirty()
        self._clear_project_context()
        self._repo_branch = branch_name
        self.projectDataChanged.emit()

    def set_local_path(self, path: str):
        if not path or path == self._local_path: return
        self._mark_dirty()
        self._clear_project_context()
        self._project_type = 'local'
        self._repo_url, self._repo_branch, self._repo_object, self._available_branches = None, None, None, []
        self._local_path = path
        self.projectDataChanged.emit()
        
    def _clear_project_context(self):
        """Очищает данные анализа."""
        self._project_context = []
        self._file_summaries_for_display = {} # <-- Очищаем и новый словарь саммари
        self.fileSummariesChanged.emit({})
        self._mark_dirty()


    # --- Управление историей чата ---
    def add_user_message(self, text: str):
        self._chat_history.append({"role": "user", "parts": [text], "excluded": False})
        self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())
    def add_model_response(self, text: str):
        self._chat_history.append({"role": "model", "parts": [text or ""], "excluded": False})
        self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())
    def toggle_api_exclusion(self, index: int):
        if 0 <= index < len(self._chat_history):
            self._chat_history[index]["excluded"] = not self._chat_history[index].get("excluded", False)
            self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())

    def toggle_all_messages_exclusion(self):
        """
        Исключает или включает все сообщения в истории чата.
        Если есть хотя бы одно неисключенное сообщение, исключает все.
        Если все сообщения уже исключены, включает все.
        """
        if not self._chat_history:
            return

        # Определяем, нужно ли нам исключать (True) или включать (False) сообщения.
        # Если хотя бы одно сообщение не исключено, то наша цель - исключить все.
        target_exclusion_state = any(not msg.get("excluded", False) for msg in self._chat_history)

        for msg in self._chat_history:
            msg["excluded"] = target_exclusion_state

        self._mark_dirty()
        self.historyChanged.emit(self.get_chat_history())
    
    # --- Управление Сессиями ---
    def new_session(self):
        self._project_type, self._repo_url, self._local_path, self._repo_branch = None, None, None, None
        self._repo_object, self._available_branches = None, []
        self._chat_history = []
        self._project_context = []
        self._file_summaries_for_display = {} # <-- Очищаем при создании новой сессии
        self._current_session_filepath = None
        # Убедимся, что расширения устанавливаются корректно, без начальных точек
        self._extensions = tuple()
        self._model_name = "gemini-1.5-flash-latest" # Устанавливаем по умолчанию актуальную модель
        self._max_output_tokens = 65536
        self._instructions = ""
        self._rag_enabled = True
        self._is_dirty = False

        self.sessionLoaded.emit()
        self.statusMessage.emit(self.tr("Новая сессия создана."), 3000)

    def load_session(self, filepath: str):
        loaded_data = db_manager.load_session_data(filepath)
        if not loaded_data:
            self.sessionError.emit(self.tr("Не удалось загрузить сессию: {0}").format(filepath))
            return

        meta, msgs, context = loaded_data

        # Загружаем всё из метаданных
        self._project_type = meta.get("project_type")
        self._repo_url = meta.get("repo_url")
        self._repo_branch = meta.get("repo_branch")
        self._local_path = meta.get("local_path")
        self._rag_enabled = bool(meta.get("rag_enabled", True))
        self._model_name = meta.get("model_name", "gemini-1.5-flash-latest") # Обновлено
        self._max_output_tokens = meta.get("max_output_tokens", 65536)
        # Убедимся, что расширения корректно парсятся из строки
        ext_str = meta.get("extensions", ".py .txt .md .json .html .css .js .yaml .yml .pdf .docx")
        self._extensions = tuple(p.strip() for p in re.split(r"[\s,]+", ext_str) if p.strip())
        self._instructions = meta.get("instructions", "")

        # Загружаем историю и контекст
        self._chat_history = msgs
        self._project_context = context
        # Заполняем _file_summaries_for_display из загруженного контекста
        self._file_summaries_for_display = {item['file_path']: item['content'] for item in self._project_context if item['type'] == 'summary'} # <-- Заполняем

        # Обновляем состояние сессии
        self._current_session_filepath = filepath
        self._is_dirty = False

        # Восстанавливаем объект репозитория, если это GitHub проект
        if self._project_type == 'github' and self._repo_url and self._github_manager:
            repo_data = self._github_manager.get_repo(self._repo_url)
            if repo_data:
                self._repo_object, _ = repo_data
                self._available_branches = self._github_manager.get_available_branches(self._repo_object)

        self.sessionLoaded.emit()
        self.statusMessage.emit(self.tr("Сессия '{0}' загружена.").format(os.path.basename(filepath)), 5000)


    def save_session(self, filepath: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        save_path = filepath or self._current_session_filepath
        if not save_path: return False, None
        
        metadata = {
            "project_type": self._project_type, "repo_url": self._repo_url,
            "repo_branch": self._repo_branch, "local_path": self._local_path,
            "rag_enabled": self._rag_enabled, "model_name": self._model_name,
            "max_output_tokens": self._max_output_tokens,
            "extensions": " ".join(self._extensions), "instructions": self._instructions
        }
        
        if db_manager.save_session_data(save_path, metadata, self._chat_history, self._project_context):
            self._current_session_filepath = save_path; self._is_dirty = False
            self.sessionStateChanged.emit(save_path, False)
            self.statusMessage.emit(self.tr("Сессия сохранена."), 5000); return True, save_path
        else:
            self.sessionError.emit(self.tr("Не удалось сохранить сессию: {0}").format(save_path)); return False, None

    # --- Остальные геттеры/сеттеры ---
    def get_project_type(self) -> Optional[str]: return self._project_type
    def get_repo_url(self) -> Optional[str]: return self._repo_url
    def get_local_path(self) -> Optional[str]: return self._local_path
    def get_selected_branch(self) -> Optional[str]: return self._repo_branch
    def get_available_branches(self) -> List[str]: return self._available_branches
    def get_available_models(self) -> List[str]: return self._available_models
    def get_model_name(self) -> str: return self._model_name
    def set_model_name(self, name: str):
        if name and name != self._model_name:
            self._model_name = name
            self._mark_dirty()
            # Запускаем асинхронную инициализацию новой модели и получение списка моделей
            # Порядок вызова _initialize_gemini() и _fetch_available_models() важен.
            # _initialize_gemini() загружает модель, а _fetch_available_models()
            # получает список доступных. Их запуск в _load_credentials() уже корректен.
            # Здесь, при смене модели пользователем, достаточно переинициализировать только текущую.
            self._initialize_gemini()
            # Вызывать _fetch_available_models() при каждом изменении модели не нужно,
            # список моделей статичен и загружается при старте/инициализации ключа.
            # Если пользователь вводит несуществующую модель вручную, _initialize_gemini()
            # вызовет ошибку.
    def get_max_tokens(self) -> int: return self._max_output_tokens
    def set_max_tokens(self, tokens: int):
        if tokens != self._max_output_tokens: self._max_output_tokens = tokens; self._mark_dirty()
    def get_extensions(self) -> Tuple[str, ...]: return self._extensions
    def set_extensions(self, ext_tuple: Tuple[str, ...]):
        if ext_tuple != self._extensions: self._extensions = ext_tuple; self._mark_dirty()
    def get_instructions(self) -> str: return self._instructions
    def set_instructions(self, text: str):
        if text != self._instructions: self._instructions = text; self._mark_dirty()
    def get_rag_enabled(self) -> bool: return self._rag_enabled
    def set_rag_enabled(self, enabled: bool):
        if enabled != self._rag_enabled: self._rag_enabled = enabled; self._mark_dirty()
    def get_chat_history(self) -> List[Dict[str, Any]]: return self._chat_history[:]
    def _mark_dirty(self):
        if not self._is_dirty: self._is_dirty = True; self.sessionStateChanged.emit(self._current_session_filepath, True)
    def is_dirty(self) -> bool: return self._is_dirty
    def get_current_session_filepath(self) -> Optional[str]: return self._current_session_filepath