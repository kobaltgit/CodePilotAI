# --- Файл: chat_model.py ---

import os
import re
import json
import logging
import hashlib
import html
from typing import Optional, List, Dict, Any, Tuple, Union
from io import BytesIO

try:
    import git
except ImportError:
    git = None
    logging.warning("Библиотека 'GitPython' не найдена. Функция обновления из Git будет недоступна.")

from PySide6.QtCore import QObject, Signal, Slot, QThread, QDir

import db_manager
from dotenv import load_dotenv, set_key, find_dotenv
import google.generativeai as genai
import google.generativeai.types as genai_types
from google.api_core import exceptions as google_exceptions
import numpy as np

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
except ImportError:
    PdfReader = None
    logging.warning("Библиотека 'PyPDF2' не найдена. PDF файлы будут проигнорированы.")

# Глобальный лимит контекстного окна
CONTEXT_WINDOW_LIMIT = 1048576

# --- Воркер для Gemini API ---
class GeminiWorker(QThread):
    response_received = Signal(str)
    error_occurred = Signal(str)
    finished_work = Signal()

    def __init__(self, api_key: str, model_name: str, prompt_parts: List[Dict[str, Any]], max_output_tokens: int):
        super().__init__()
        self.api_key = api_key
        self.model_name = model_name
        self.prompt_parts = prompt_parts
        self.max_output_tokens_config = max_output_tokens
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        logger.info("GeminiWorker: Получен запрос на отмену.")

    def run(self):
        full_response_text = ""
        try:
            if self._is_cancelled:
                logger.info("GeminiWorker: Выполнение отменено до начала.")
                self.finished_work.emit()
                return

            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(self.model_name)

            logger.info(f"GeminiWorker: Отправка запроса к Gemini API для модели '{self.model_name}'...")
            generation_config = genai_types.GenerationConfig(max_output_tokens=self.max_output_tokens_config)

            response = model.generate_content(
                self.prompt_parts,
                generation_config=generation_config,
                request_options={"timeout": 180},
            )
            logger.info(f"GeminiWorker: Получен ответ от Gemini API.")

            if self._is_cancelled:
                logger.info("GeminiWorker: Выполнение отменено во время работы.")
                self.finished_work.emit()
                return

            if hasattr(response, "text") and response.text:
                logger.info(f"GeminiWorker: Ответ получен. Длина текста: {len(response.text)}.")
                self.response_received.emit(response.text)
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
            logger.info("GeminiWorker: Завершение работы.")
            self.finished_work.emit()
            self.deleteLater()


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
        self._file_summaries_for_display: Dict[str, str] = {} # Для отображения саммари
        self._current_session_filepath: Optional[str] = None
        self._is_dirty: bool = False
        # Специфичные для проекта
        self._project_type: Optional[str] = None  # 'github' или 'local'
        self._is_git_repo: bool = False
        self._is_partial_update: bool = False
        self._last_updated_files: List[str] = []
        self._repo_url: Optional[str] = None
        self._repo_branch: Optional[str] = None
        self._local_path: Optional[str] = None
        # Вспомогательные
        self._repo_object: Optional[Repository] = None
        self._available_branches: List[str] = []

        # --- Настройки ---
        self._model_name: str = "gemini-1.5-flash-latest"
        self._available_models: List[str] = [self._model_name]
        self._max_output_tokens: int = 65536
        self._extensions: Tuple[str, ...] = tuple()
        self._instructions: str = ""
        self._rag_enabled: bool = True
        self._semantic_search_enabled: bool = False # Новый режим по умолчанию выключен

        # --- Состояние токенов ---
        self._current_prompt_tokens: int = 0
        self._token_limit_for_display: int = CONTEXT_WINDOW_LIMIT

        # --- Воркеры и менеджеры ---
        self._analysis_worker: Optional[SummarizerWorker] = None
        self._analysis_thread: Optional[QThread] = None
        self._github_manager: Optional[GitHubManager] = None
        self._gemini_worker: Optional[GeminiWorker] = None

        self._load_credentials()
        self.new_session()

    # --- Управление Ключами и Токенами ---
    def _load_credentials(self):
        load_dotenv(dotenv_path=self._dotenv_path, override=True)
        self._gemini_api_key = os.getenv("GEMINI_API_KEY")
        self._gemini_api_key_loaded = bool(self._gemini_api_key)
        status = self.tr("Загружен") if self._gemini_api_key_loaded else self.tr("Не найден!")
        self.geminiApiKeyStatusChanged.emit(self._gemini_api_key_loaded, self.tr("Ключ API: {0}").format(status))
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
        """
        if not self._gemini_api_key:
            self.geminiApiKeyStatusChanged.emit(False, self.tr("Ключ API: Отсутствует"))
            self.statusMessage.emit(self.tr("Ключ API Gemini отсутствует."), 5000)
            self._available_models = [] 
            self.availableModelsChanged.emit([])
            return

        try:
            genai.configure(api_key=self._gemini_api_key)
            logger.info("ChatModel: Получение списка моделей Gemini...")
            models = genai.list_models()
            self._available_models = sorted([m.name.replace("models/", "") for m in models if 'generateContent' in m.supported_generation_methods])
            self.availableModelsChanged.emit(self._available_models)
            logger.info(f"ChatModel: Список моделей ({len(self._available_models)}) успешно получен.")

            if self._model_name not in self._available_models:
                if self._available_models:
                    old_model_name = self._model_name
                    self._model_name = self._available_models[0]
                    logger.warning(self.tr("Выбранная модель '{0}' недоступна. Установлена первая доступная: '{1}'.").format(old_model_name, self._model_name))
                    self.statusMessage.emit(self.tr("Модель '{0}' недоступна. Установлена: '{1}'.").format(old_model_name, self._model_name), 5000)
                else:
                    self.statusMessage.emit(self.tr("Нет доступных моделей Gemini."), 5000)
                    self._model_name = ""

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

    def _initialize_github_manager(self):
        if not self._github_token: return
        self._github_manager = GitHubManager(self._github_token)
        rate_info = self._github_manager.rate_limit_info if self._github_manager.is_authenticated() else self.tr("Ошибка!")
        self.githubTokenStatusChanged.emit(self._github_manager.is_authenticated(), self.tr("Токен GitHub: {0}").format(rate_info))

    # --- Анализ проекта ---
    def start_project_analysis(self):
        """Запускает анализ проекта, независимо от его типа (GitHub или локальный)."""
        self._is_partial_update = False
        is_ready, error_msg = self._is_ready_for_analysis()
        if not is_ready:
            self.analysisError.emit(error_msg)
            return

        if self._analysis_thread and self._analysis_thread.isRunning():
            self.statusMessage.emit(self.tr("Анализ уже запущен."), 3000)
            return

        self._clear_project_context()

        self.statusMessage.emit(self.tr("Поиск файлов для анализа..."), 0)
        file_paths = self._get_file_paths_for_analysis()
        if not file_paths:
            self.analysisError.emit(self.tr("Не найдено подходящих файлов для анализа. Проверьте путь и выбранные расширения."))
            return

        self.analysisStarted.emit()
        self.statusMessage.emit(self.tr("Начат анализ {0} файлов...").format(len(file_paths)), 0)

        # 1. Создаем поток и воркер
        self._analysis_thread = QThread()
        self._analysis_worker = SummarizerWorker(
            file_paths=file_paths,
            project_type=self._project_type,
            project_source_path=self._local_path or self._repo_url,
            repo_object=self._repo_object,
            repo_branch=self._repo_branch,
            github_manager=self._github_manager,
            rag_enabled=self._rag_enabled,
            semantic_search_enabled=self._semantic_search_enabled,
            gemini_api_key=self._gemini_api_key,
            model_name=self._model_name,
            app_lang=self._app_language
        )

        # 2. Перемещаем воркер в поток
        self._analysis_worker.moveToThread(self._analysis_thread)

        # 3. Подключаем сигналы
        # Сигналы от воркера к модели
        self._analysis_worker.context_data_ready.connect(self._on_context_data_ready)
        self._analysis_worker.file_summarized.connect(self._on_file_summarized)
        self._analysis_worker.progress_updated.connect(self.analysisProgressUpdated)
        self._analysis_worker.error_occurred.connect(self.analysisError)
        
        # Запуск воркера при старте потока
        self._analysis_thread.started.connect(self._analysis_worker.run)

        # Корректное завершение и очистка
        self._analysis_worker.finished.connect(self._on_analysis_finished) # Сначала обработаем завершение в модели
        self._analysis_worker.finished.connect(self._analysis_thread.quit) # Затем остановим цикл событий потока
        self._analysis_thread.finished.connect(self._analysis_worker.deleteLater) # По завершении потока удалим воркер
        self._analysis_thread.finished.connect(self._analysis_thread.deleteLater) # И сам поток
        self._analysis_thread.finished.connect(lambda: setattr(self, '_analysis_worker', None))
        self._analysis_thread.finished.connect(lambda: setattr(self, '_analysis_thread', None))

        # 4. Запускаем поток
        self._analysis_thread.start()

    def _is_ready_for_analysis(self) -> Tuple[bool, str]:
        """Проверяет, все ли готово к запуску анализа."""
        if not self._gemini_api_key_loaded and (self._rag_enabled or self._semantic_search_enabled):
            return False, self.tr("Ключ API Gemini необходим для анализа в режиме RAG или семантического поиска.")
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
            files_to_process, _ = self._github_manager.get_repo_file_tree(self._repo_object, self._repo_branch, self._extensions)
            file_paths = list(files_to_process.keys())
        elif self._project_type == 'local':
            ignored_dirs = {"venv", ".venv", "__pycache__", ".git", ".vscode", ".idea", "node_modules"}
            for root, dirs, files in os.walk(self._local_path, topdown=True):
                dirs[:] = [d for d in dirs if d not in ignored_dirs]
                for file in files:
                    if file.lower().endswith(self._extensions):
                        full_path = os.path.join(root, file)
                        file_paths.append(full_path)

        logger.info(f"Найдено {len(file_paths)} файлов для анализа.")
        return file_paths

    def cancel_analysis(self):
        if self._analysis_worker and self._analysis_thread and self._analysis_thread.isRunning():
            self._analysis_worker.cancel()
            self.statusMessage.emit(self.tr("Отмена анализа..."), 3000)

    def start_context_update_from_git(self):
        """
        Находит измененные и новые файлы в локальном Git-репозитории и запускает
        их частичный переанализ.
        """
        if not self.is_git_repo():
            self.analysisError.emit(self.tr("Текущий проект не является локальным Git-репозиторием."))
            return
            
        if self._analysis_thread and self._analysis_thread.isRunning():
            self.statusMessage.emit(self.tr("Анализ уже запущен."), 3000)
            return

        try:
            repo = git.Repo(self._local_path)
            
            # Находим измененные (modified) и новые (untracked) файлы
            modified_files = [item.a_path for item in repo.index.diff(None)]
            untracked_files = repo.untracked_files
            changed_paths = set(modified_files + untracked_files)

            if not changed_paths:
                self.statusMessage.emit(self.tr("Нет изменений для обновления."), 3000)
                return

            # Фильтруем файлы по выбранным расширениям
            files_to_reanalyze = [
                os.path.join(self._local_path, p) for p in changed_paths 
                if p.lower().endswith(self._extensions)
            ]

            if not files_to_reanalyze:
                self.statusMessage.emit(self.tr("Нет измененных файлов с нужными расширениями."), 3000)
                return

            logger.info(f"Обнаружено {len(files_to_reanalyze)} измененных файлов для переанализа.")
            self.analysisStarted.emit()
            self.statusMessage.emit(self.tr("Обновление контекста для {0} файлов...").format(len(files_to_reanalyze)), 0)

            # Удаляем старый контекст для этих файлов
            # Нормализуем пути для корректного сравнения
            relative_paths_to_remove = {os.path.relpath(fp, self._local_path).replace('\\', '/') for fp in files_to_reanalyze}
            
            # --- НОВЫЙ КОД ---
            self._is_partial_update = True
            self._last_updated_files = sorted(list(relative_paths_to_remove))
            # --- КОНЕЦ НОВОГО КОДА ---

            self._project_context = [
                item for item in self._project_context 
                if item.get('file_path') not in relative_paths_to_remove
            ]
            
            self._project_context = [
                item for item in self._project_context 
                if item.get('file_path') not in relative_paths_to_remove
            ]
            for rel_path in relative_paths_to_remove:
                self._file_summaries_for_display.pop(rel_path, None)

            # Запускаем воркер для анализа только измененных файлов
            self._analysis_thread = QThread()
            self._analysis_worker = SummarizerWorker(
                file_paths=files_to_reanalyze, # Передаем только измененные файлы
                project_type=self._project_type,
                project_source_path=self._local_path,
                repo_object=None,
                repo_branch=None,
                github_manager=None,
                rag_enabled=self._rag_enabled,
                semantic_search_enabled=self._semantic_search_enabled,
                gemini_api_key=self._gemini_api_key,
                model_name=self._model_name,
                app_lang=self._app_language
            )
            self._analysis_worker.moveToThread(self._analysis_thread)
            self._analysis_worker.context_data_ready.connect(self._on_context_data_ready)
            self._analysis_worker.file_summarized.connect(self._on_file_summarized)
            self._analysis_worker.progress_updated.connect(self.analysisProgressUpdated)
            self._analysis_worker.error_occurred.connect(self.analysisError)
            self._analysis_thread.started.connect(self._analysis_worker.run)
            self._analysis_worker.finished.connect(self._on_analysis_finished)
            self._analysis_worker.finished.connect(self._analysis_thread.quit)
            self._analysis_thread.finished.connect(self._analysis_worker.deleteLater)
            self._analysis_thread.finished.connect(self._analysis_thread.deleteLater)
            self._analysis_thread.finished.connect(lambda: setattr(self, '_analysis_worker', None))
            self._analysis_thread.finished.connect(lambda: setattr(self, '_analysis_thread', None))
            self._analysis_thread.start()

        except git.InvalidGitRepositoryError:
            self.analysisError.emit(self.tr("Ошибка: Папка перестала быть Git-репозиторием."))
        except Exception as e:
            logger.error(f"Ошибка при обновлении контекста из Git: {e}", exc_info=True)
            self.analysisError.emit(self.tr("Неожиданная ошибка при работе с Git: {0}").format(e))

    @Slot(list)
    def _on_context_data_ready(self, context_data_batch: List[Dict[str, Any]]):
        self._project_context.extend(context_data_batch)
        self._mark_dirty()

    @Slot(str, str)
    def _on_file_summarized(self, file_path: str, summary: str):
        self._file_summaries_for_display[file_path] = summary
        self.fileSummariesChanged.emit(self._file_summaries_for_display)

    @Slot()
    def _on_analysis_finished(self):
        if self._is_partial_update and self._last_updated_files:
            files_str = ", ".join(self._last_updated_files)
            msg = self.tr("[Система]: Контекст был обновлен для следующих файлов: {0}").format(files_str)
            self.add_system_message(msg)
            self.statusMessage.emit(self.tr("Обновление контекста завершено."), 5000)
            # Сбрасываем флаги
            self._is_partial_update = False
            self._last_updated_files = []
        else:
            self.statusMessage.emit(self.tr("Анализ проекта завершен."), 5000)

        self.analysisFinished.emit()
        self.fileSummariesChanged.emit(self._file_summaries_for_display)
        
    # --- RAG и основной запрос к API ---
    def send_request_to_api(self, user_input: str):
        if not self._is_ready_for_request(): return

        user_input_stripped = user_input.strip()
        if not user_input_stripped:
            self.statusMessage.emit(self.tr("Введите ваш запрос."), 3000)
            return

        self.add_user_message(user_input_stripped)
        self.apiRequestStarted.emit()

        final_prompt_parts = self._build_final_prompt()
        if not final_prompt_parts:
            self.apiRequestFinished.emit()
            return

        self.statusMessage.emit(self.tr("Отправка запроса ({0} т.)...").format(self._current_prompt_tokens), 0)

        self._gemini_worker = GeminiWorker(
            api_key=self._gemini_api_key,
            model_name=self._model_name,
            prompt_parts=final_prompt_parts,
            max_output_tokens=self._max_output_tokens,
        )
        self._gemini_worker.response_received.connect(self._on_api_response_received)
        self._gemini_worker.error_occurred.connect(self._handle_final_api_error)
        self._gemini_worker.finished_work.connect(self.apiRequestFinished)
        self._gemini_worker.finished_work.connect(lambda: setattr(self, '_gemini_worker', None))
        self._gemini_worker.start()

    def _is_ready_for_request(self) -> bool:
        if self._gemini_worker and self._gemini_worker.isRunning():
            self.statusMessage.emit(self.tr("Дождитесь завершения предыдущего запроса."), 3000); return False
        if not self._gemini_api_key_loaded:
            self.apiErrorOccurred.emit(self.tr("Ключ API не загружен.")); return False
        return True

    def _build_final_prompt(self) -> List[Dict[str, Any]]:
        """
        Собирает финальный промпт для API, используя гибридную RAG-стратегию.
        Включает системные инструкции, саммари, релевантные чанки (если вкл. семантический поиск)
        и историю чата.
        """
        # --- Вспомогательные функции ---
        def clean_message(msg: Dict[str, Any]) -> Dict[str, Any]:
            cleaned_parts = [str(p) for p in msg.get("parts", []) if p is not None]
            return {"role": msg["role"], "parts": cleaned_parts}

        def _count_tokens_helper(parts_list: List[Dict[str, Any]]) -> Optional[int]:
            if not parts_list: return 0
            try:
                temp_model = genai.GenerativeModel(self._model_name)
                # API Gemini требует чередования ролей для count_tokens
                temp_prompt = []
                last_role = None
                for msg in parts_list:
                    if not msg.get("role") or not msg.get("parts"): continue
                    if last_role == msg["role"] and len(temp_prompt) > 0:
                        temp_prompt[-1] = clean_message(msg)
                    else:
                        temp_prompt.append(clean_message(msg))
                        last_role = msg["role"]
                return temp_model.count_tokens(temp_prompt).total_tokens
            except Exception as e:
                logger.warning(self.tr("Ошибка при подсчете токенов: {0}. Использование грубой оценки.").format(e))
                return sum(len(p) for msg in parts_list for p in msg.get("parts", [])) // 4

        # --- Начало сборки ---
        self.apiIntermediateStep.emit(self.tr("Подготовка промпта..."))

        final_prompt_parts = []
        prompt_token_budget = CONTEXT_WINDOW_LIMIT - self._max_output_tokens
        current_tokens = 0

        # --- НОВЫЙ ШАГ: Сборка карты проекта (Code Graph) ---
        project_structure_map = self._build_project_structure_map()
        # --- КОНЕЦ НОВОГО ШАГА ---

        # 1. Системные инструкции
        instructions_text = self._build_system_instructions(project_structure_map)
        instructions_part = [
            {"role": "user", "parts": [instructions_text]},
            {"role": "model", "parts": [self.tr("OK. Я готов к работе.")]}
        ]
        instr_tokens = _count_tokens_helper(instructions_part)
        if instr_tokens is None or current_tokens + instr_tokens >= prompt_token_budget:
            self.apiErrorOccurred.emit(self.tr("Системные инструкции слишком длинные.")); return []
        final_prompt_parts.extend(instructions_part); current_tokens += instr_tokens

        # 2. Контекст проекта (саммари и чанки)
        context_str = self._build_context_string(prompt_token_budget - current_tokens)
        if context_str:
            context_part = [
                {"role": "user", "parts": [self.tr("**Контекст из файлов проекта:**\n{0}").format(context_str)]},
                {"role": "model", "parts": [self.tr("OK. Контекст получен.")]}
            ]
            context_tokens = _count_tokens_helper(context_part)
            if context_tokens and current_tokens + context_tokens < prompt_token_budget:
                final_prompt_parts.extend(context_part); current_tokens += context_tokens
            else:
                self.apiIntermediateStep.emit(self.tr("Контекст проекта частично урезан из-за лимита токенов."))

        # 3. История чата
        # 3. История чата
        history_to_consider = self._chat_history[:-1]
        history_part = []
        for message in reversed(history_to_consider):
            if message.get("excluded", False): continue
            
            # --- НОВАЯ ЛОГИКА: Преобразуем 'system' в 'user' для API ---
            message_for_api = clean_message(message)
            if message_for_api.get("role") == "system":
                message_for_api["role"] = "user"
            # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

            message_tokens = _count_tokens_helper([message_for_api])
            if message_tokens and current_tokens + message_tokens <= prompt_token_budget:
                history_part.insert(0, message_for_api)
                current_tokens += message_tokens
            else:
                self.apiIntermediateStep.emit(self.tr("Часть истории чата исключена из-за лимита токенов."))
                break
        final_prompt_parts.extend(history_part)

        # 4. Последний запрос пользователя
        last_user_message = self._chat_history[-1]
        cleaned_last_message = clean_message(last_user_message)
        last_message_tokens = _count_tokens_helper([cleaned_last_message])
        if not last_message_tokens or (current_tokens + last_message_tokens > prompt_token_budget):
            self.apiErrorOccurred.emit(self.tr("Недостаточно места для вашего запроса. Попробуйте исключить сообщения из истории.")); return []
        final_prompt_parts.append(cleaned_last_message); current_tokens += last_message_tokens

        # 5. Финальная очистка и подсчет
        final_cleaned_messages = self._cleanup_roles(final_prompt_parts)
        self._current_prompt_tokens = _count_tokens_helper(final_cleaned_messages) or 0
        self.tokenCountUpdated.emit(self._current_prompt_tokens, CONTEXT_WINDOW_LIMIT)

        return final_cleaned_messages

    def _cleanup_roles(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Гарантирует чередование ролей user/model в финальном промпте."""
        if not messages: return []
        cleaned_list = []
        last_role = None
        for msg in messages:
            current_role = msg.get("role")
            if not current_role: continue
            if not cleaned_list:
                cleaned_list.append(msg); last_role = current_role
                continue
            if current_role == last_role:
                # Объединяем контент user или заменяем model
                if current_role == "user":
                    cleaned_list[-1]["parts"].extend(msg["parts"])
                else: # model
                    cleaned_list[-1] = msg # Заменяем предыдущий ответ модели
            else:
                cleaned_list.append(msg); last_role = current_role
        return cleaned_list

    def _build_system_instructions(self, project_structure_map: str) -> str:
        # --- Наша внутренняя, "секретная" инструкция ---
        file_save_instruction_ru = "ВАЖНОЕ ПРАВИЛО: Если ты генерируешь код для совершенно нового файла, всегда указывай предлагаемое имя файла на отдельной строке прямо перед блоком кода в формате `File: path/to/filename.ext`."
        file_save_instruction_en = "IMPORTANT RULE: If you generate code for a brand new file, always specify the suggested filename on a separate line right before the code block, in the format `File: path/to/filename.ext`."
        internal_instruction = file_save_instruction_ru if self._app_language == 'ru' else file_save_instruction_en

        lang_instruction_phrase = self.tr("на русском языке") if self._app_language == 'ru' else "in English"
        base_instructions = ""
        if self._project_context:
            base_instructions = self.tr(
                "Ты — мой ассистент по программированию и другим направлениям, это зависит от контекста. Анализируй предоставленный контекст и отвечай на вопросы. "
                "Отвечай {0}, если не указано иное. При ответе всегда используй Pygments для подсветки кода."
            ).format(lang_instruction_phrase)
        else:
            base_instructions = self.tr(
                "Ты — полезный и разносторонний ассистент. Отвечай на вопросы четко и по делу. "
                "Отвечай {0}, если не указано иное."
            ).format(lang_instruction_phrase)
        
        user_instructions_text = self._instructions.strip()

        # Собираем финальный промпт
        final_parts = [internal_instruction, base_instructions]
        
        # Добавляем карту структуры проекта, если она есть
        if project_structure_map:
            final_parts.append(project_structure_map)

        if user_instructions_text:
            user_instructions_header = self.tr("Дополнительные инструкции от пользователя:")
            final_parts.append(f"{user_instructions_header}\n{user_instructions_text}")

        return "\n\n".join(part for part in final_parts if part)

    def _build_project_structure_map(self) -> str:
        """
        Собирает текстовое представление структуры проекта (Code Graph)
        из данных, полученных от AST-парсера.
        """
        structure_items = [item for item in self._project_context if item.get('type') == 'structure']
        if not structure_items:
            return ""

        output_lines = [self.tr("--- Обзор структуры проекта (Code-Graph) ---")]
        
        # Группируем по файлам
        files_map = {}
        for item in structure_items:
            file_path = item.get('file_path')
            if file_path not in files_map:
                files_map[file_path] = {}
            # Объединяем словари, если для одного файла пришло несколько (хотя не должно)
            files_map[file_path].update(item.get('content', {}))

        sorted_files = sorted(files_map.keys())

        for file_path in sorted_files:
            structure = files_map[file_path]
            output_lines.append(f"\nFile: {file_path}")
            
            imports = structure.get('imports')
            if imports:
                output_lines.append("  - Imports:")
                for imp in imports:
                    output_lines.append(f"    - {imp}")
            
            classes = structure.get('classes')
            if classes:
                output_lines.append("  - Defines Classes:")
                for class_name, inheritance in classes.items():
                    output_lines.append(f"    - {class_name}{inheritance}")

            functions = structure.get('functions')
            if functions:
                output_lines.append("  - Defines Functions:")
                for func_sig in functions:
                    output_lines.append(f"    - {func_sig}")
        
        output_lines.append("\n--- Конец обзора структуры ---")
        return "\n".join(output_lines)

    def _build_context_string(self, remaining_budget_tokens: int) -> str:
        """
        Собирает строку контекста проекта, используя либо семантический поиск, либо полный контекст.
        """
        if not self._project_context or not self.get_chat_history(): return ""

        last_user_message = self.get_chat_history()[-1]["parts"][0]

        # --- Шаг 1: Разделяем контекст на саммари и чанки ---
        all_summaries = [item for item in self._project_context if item['type'] == 'summary']
        all_chunks = [item for item in self._project_context if item['type'] == 'chunk']

        relevant_items = []

        # --- Шаг 2: Выбираем чанки ---
        if self._rag_enabled and self._semantic_search_enabled and all_chunks:
            self.apiIntermediateStep.emit(self.tr("Выполняется семантический поиск релевантных фрагментов..."))
            top_n = 10 # Количество самых релевантных чанков для включения

            try:
                # Получаем эмбеддинг для запроса пользователя
                query_embedding_result = genai.embed_content(
                    model='models/text-embedding-004',
                    content=last_user_message,
                    task_type="RETRIEVAL_QUERY"
                )
                query_embedding = np.array(query_embedding_result['embedding'])

                # Рассчитываем косинусное сходство
                chunk_embeddings = np.array([chunk['embedding'] for chunk in all_chunks])
                similarities = np.dot(chunk_embeddings, query_embedding) / (np.linalg.norm(chunk_embeddings, axis=1) * np.linalg.norm(query_embedding))

                # Получаем индексы топ-N самых похожих чанков
                top_indices = np.argsort(similarities)[-top_n:][::-1]

                relevant_chunks = [all_chunks[i] for i in top_indices]
                relevant_items.extend(relevant_chunks)
                self.apiIntermediateStep.emit(self.tr("Найдено {0} релевантных фрагментов кода.").format(len(relevant_chunks)))

            except Exception as e:
                self.apiIntermediateStep.emit(self.tr("Ошибка семантического поиска: {0}").format(e))
                logger.error(f"Ошибка семантического поиска: {e}", exc_info=True)
                # В случае ошибки, откатываемся к использованию всех чанков
                relevant_items.extend(all_chunks)

        elif self._rag_enabled: # RAG включен, но семантический поиск выключен
            self.apiIntermediateStep.emit(self.tr("Добавление всех фрагментов кода в контекст..."))
            relevant_items.extend(all_chunks)
        else: # RAG выключен
             full_files = [item for item in self._project_context if item['type'] == 'full_file']
             relevant_items.extend(full_files)

        # --- Шаг 3: Собираем финальную строку ---
        context_parts = []
        char_budget = remaining_budget_tokens * 4 # Грубая оценка
        current_len = 0

        # Сначала всегда добавляем все саммари
        if self._rag_enabled:
            for summary in all_summaries:
                header = self.tr("--- Обзор файла: {0} ---\n").format(summary['file_path'])
                text = header + summary['content'] + "\n\n"
                if current_len + len(text) <= char_budget:
                    context_parts.append(text); current_len += len(text)
                else: break

        # Затем добавляем релевантные чанки/файлы
        for item in relevant_items:
            if item['type'] == 'chunk':
                header = self.tr("--- Фрагмент ({0}) из файла: {1} ---\n").format(item.get('chunk_num', 0), item['file_path'])
            else: # full_file
                header = self.tr("--- Содержимое файла: {0} ---\n").format(item['file_path'])

            text = header + item['content'] + "\n\n"
            if current_len + len(text) <= char_budget:
                context_parts.append(text); current_len += len(text)
            else:
                self.apiIntermediateStep.emit(self.tr("Часть контекста урезана из-за лимита токенов."))
                break

        return "".join(context_parts)

    @Slot(str)
    def _on_api_response_received(self, response_text: str):
        self.add_model_response(response_text)
        self.apiResponseReceived.emit(response_text)

    @Slot(str)
    def _handle_final_api_error(self, error_message: str): self.apiErrorOccurred.emit(error_message)

    # --- Управление состоянием (геттеры/сеттеры) ---
    def set_project_type(self, ptype: Optional[str]):
        if ptype != self._project_type: self._mark_dirty()
        self._project_type = ptype
        self.projectDataChanged.emit()

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

        # Проверяем, является ли это Git-репозиторием
        if git and os.path.isdir(os.path.join(path, '.git')):
            self._is_git_repo = True
            logger.info(f"Локальный проект '{path}' определен как Git-репозиторий.")
        else:
            self._is_git_repo = False
        
        self.projectDataChanged.emit()

    def _clear_project_context(self):
        self._project_context = []
        self._file_summaries_for_display = {}
        self.fileSummariesChanged.emit({})
        self._mark_dirty()

    # --- Управление историей чата ---
    def add_user_message(self, text: str):
        self._chat_history.append({"role": "user", "parts": [text], "excluded": False})
        self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())

    def add_model_response(self, text: str):
        self._chat_history.append({"role": "model", "parts": [text or ""], "excluded": False})
        self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())

    def add_system_message(self, text: str):
        """Добавляет системное сообщение в историю чата."""
        self._chat_history.append({"role": "system", "parts": [text], "excluded": False})
        self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())

    def toggle_api_exclusion(self, index: int):
        if 0 <= index < len(self._chat_history):
            self._chat_history[index]["excluded"] = not self._chat_history[index].get("excluded", False)
            self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())

    def toggle_all_messages_exclusion(self):
        if not self._chat_history: return
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
        self._file_summaries_for_display = {}
        self._current_session_filepath = None
        self._extensions = tuple()
        self._model_name = "gemini-1.5-flash-latest"
        self._max_output_tokens = 65536
        self._instructions = ""
        self._rag_enabled = True
        self._semantic_search_enabled = False
        self._is_dirty = False

        self.sessionLoaded.emit()
        self.statusMessage.emit(self.tr("Новая сессия создана."), 3000)

    def get_current_session_filepath(self) -> Optional[str]:
        """Возвращает путь к текущему файлу сессии, если он есть."""
        return self._current_session_filepath

    def load_session(self, filepath: str):
        try:
            logger.debug(f"--- НАЧАЛО ЗАГРУЗКИ СЕССИИ: {os.path.basename(filepath)} ---")
            
            logger.debug("Шаг 1: Вызов db_manager.load_session_data...")
            loaded_data = db_manager.load_session_data(filepath)
            if not loaded_data:
                logger.error("Шаг 1 ПРОВАЛЕН: db_manager не вернул данные.")
                self.sessionError.emit(self.tr("Не удалось загрузить данные из файла сессии: {0}").format(os.path.basename(filepath)))
                return
            logger.debug("Шаг 1 УСПЕХ: Данные из файла получены.")

            meta, msgs, context = loaded_data
            
            logger.debug("Шаг 2: Начинаем парсинг метаданных.")
            self._project_type = meta.get("project_type")
            self._repo_url = meta.get("repo_url")
            self._repo_branch = meta.get("repo_branch")
            self._local_path = meta.get("local_path")
            self._rag_enabled = bool(meta.get("rag_enabled", True))
            self._semantic_search_enabled = bool(meta.get("semantic_search_enabled", False))
            self._model_name = meta.get("model_name", "gemini-1.5-flash-latest")
            
            try:
                self._max_output_tokens = int(meta.get("max_output_tokens"))
            except (ValueError, TypeError):
                self._max_output_tokens = 65536 # Значение по умолчанию
            
            ext_str = meta.get("extensions") or ".py .txt .md .json .html .css .js .yaml .yml .pdf .docx"
            self._extensions = tuple(p.strip() for p in re.split(r"[\s,]+", ext_str) if p.strip())
            
            self._instructions = meta.get("instructions", "")
            logger.debug("Шаг 2 УСПЕХ: Метаданные обработаны.")
            
            logger.debug("Шаг 3: Обработка истории и контекста.")
            self._chat_history = msgs
            self._project_context = context
            
            logger.debug("Шаг 3.1: Создание словаря саммари для отображения...")
            self._file_summaries_for_display = {
                item.get('file_path'): item.get('content') 
                for item in self._project_context 
                if item.get('type') == 'summary' and item.get('file_path')
            }
            logger.debug("Шаг 3.1 УСПЕХ: Словарь саммари создан.")
            
            self._current_session_filepath = filepath
            self._is_dirty = False
            logger.debug("Шаг 3 УСПЕХ: История, контекст и путь к сессии установлены.")

            logger.debug("Шаг 4: Обработка данных проекта (GitHub).")
            if self._project_type == 'github' and self._repo_url and self._github_manager:
                repo_data = self._github_manager.get_repo(self._repo_url)
                if repo_data:
                    self._repo_object, _ = repo_data
                    self._available_branches = self._github_manager.get_available_branches(self._repo_object)
            logger.debug("Шаг 4 УСПЕХ: Данные проекта обработаны.")

            logger.debug("Шаг 5: Отправка сигнала sessionLoaded.emit()...")
            self.sessionLoaded.emit()
            self.statusMessage.emit(self.tr("Сессия '{0}' загружена.").format(os.path.basename(filepath)), 5000)
            logger.info(f"--- УСПЕШНОЕ ЗАВЕРШЕНИЕ ЗАГРУЗКИ СЕССИИ: {os.path.basename(filepath)} ---")

        except Exception as e:
            # --- САМОЕ ВАЖНОЕ: ЛОГИРОВАНИЕ ПОЛНОЙ ОШИБКИ ---
            logger.error(
                f"КРИТИЧЕСКАЯ ОШИБКА в ChatModel.load_session при обработке файла '{os.path.basename(filepath)}'.",
                exc_info=True  # Эта строка добавит полный traceback в лог
            )
            error_message = self.tr("Произошла критическая ошибка при обработке данных сессии. См. лог-файл для деталей.")
            self.sessionError.emit(error_message)

    def save_session(self, filepath: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        save_path = filepath or self._current_session_filepath
        if not save_path: return False, None

        metadata = {
            "project_type": self._project_type, "repo_url": self._repo_url,
            "repo_branch": self._repo_branch, "local_path": self._local_path,
            "rag_enabled": self._rag_enabled, "model_name": self._model_name,
            "max_output_tokens": self._max_output_tokens,
            "extensions": " ".join(self._extensions), "instructions": self._instructions,
            "semantic_search_enabled": self._semantic_search_enabled
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
            self._initialize_gemini()

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
    def get_project_context(self) -> List[Dict[str, Any]]:
        """Возвращает текущий контекст проекта (список саммари и чанков)."""
        return self._project_context[:]
    def _mark_dirty(self):
        if not self._is_dirty: self._is_dirty = True; self.sessionStateChanged.emit(self._current_session_filepath, True)
    def is_dirty(self) -> bool: return self._is_dirty

    def get_original_file_content(self, file_path: str) -> Optional[str]:
        """
        Находит и возвращает оригинальное содержимое файла.
        Приоритет 1: Прямое чтение с диска (для локальных) или через API (для GitHub).
        Приоритет 2 (Fallback): Сборка из сохраненного в сессии контекста.
        """
        # --- Приоритет 1: Прямое чтение из источника ---
        source_content: Optional[str] = None
        try:
            if self._project_type == 'local' and self._local_path:
                full_disk_path = os.path.join(self._local_path, file_path)
                if os.path.exists(full_disk_path):
                    with open(full_disk_path, 'rb') as f:
                        source_content = f.read().decode('utf-8', errors='ignore')
                    logger.info(f"Успешно прочитан оригинал файла '{file_path}' с диска.")

            elif self._project_type == 'github' and self._github_manager and self._repo_object and self._repo_branch:
                content_bytes = self._github_manager.get_file_content(self._repo_object, file_path, self._repo_branch)
                if content_bytes is not None:
                    source_content = content_bytes.decode('utf-8', errors='ignore')
                    logger.info(f"Успешно получен оригинал файла '{file_path}' из GitHub API.")
            
            if source_content is not None:
                return source_content
        except Exception as e:
            logger.warning(f"Ошибка при прямом чтении файла '{file_path}': {e}. Попытка использования резервного метода.")

        # --- Приоритет 2: Резервный метод (сборка из контекста) ---
        logger.warning(f"Не удалось прочитать '{file_path}' из источника. Используется резервный метод сборки из контекста.")
        
        if not self._project_context: return None

        file_items = [
            item for item in self._project_context
            if item.get('file_path') == file_path and item.get('type') in ('full_file', 'chunk')
        ]

        if not file_items:
            logger.warning(f"Резервный метод не удался: контент для файла '{file_path}' в контексте не найден.")
            return None

        if file_items[0].get('type') == 'full_file':
            return file_items[0].get('content', '')

        sorted_chunks = sorted(file_items, key=lambda x: x.get('chunk_num', 0))
        full_content = "\n\n".join(chunk.get('content', '') for chunk in sorted_chunks)
        
        logger.debug(f"Оригинальное содержимое для '{file_path}' собрано из {len(sorted_chunks)} чанков (резервный метод).")
        return full_content
    
    def is_git_repo(self) -> bool:
        """Возвращает True, если текущий локальный проект является Git-репозиторием."""
        return self._project_type == 'local' and self._is_git_repo

    def get_semantic_search_enabled(self) -> bool: return self._semantic_search_enabled

    def set_semantic_search_enabled(self, enabled: bool):
        if enabled != self._semantic_search_enabled:
            self._semantic_search_enabled = enabled
            self._mark_dirty()

    def save_generated_file(self, file_path: str, content: str) -> Tuple[bool, str]:
        """Saves content to a specified file path."""
        try:
            dir_name = os.path.dirname(file_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            logger.info(f"Successfully saved generated file to: {file_path}")
            return True, self.tr("Файл '{0}' успешно сохранен.").format(os.path.basename(file_path))
        except Exception as e:
            logger.error(f"Error saving generated file to {file_path}: {e}", exc_info=True)
            return False, self.tr("Ошибка сохранения файла '{0}': {1}").format(os.path.basename(file_path), e)
        
    def export_chat_to_string(self, export_format: str) -> Optional[str]:
        """
        Экспортирует историю чата в строку заданного формата.
        :param export_format: 'markdown' или 'html'
        :return: Строка с отформатированным чатом или None, если история пуста.
        """
        if not self._chat_history:
            return None

        if export_format == 'markdown':
            return self._export_to_markdown()
        elif export_format == 'html':
            return self._export_to_html()
        else:
            logger.error(f"Неизвестный формат экспорта: {export_format}")
            return None

    def _export_to_markdown(self) -> str:
        """Экспортирует чат в формат Markdown."""
        from markdown import markdown as md_render
        lines = []
        for msg in self._chat_history:
            role = msg.get("role")
            content = (msg.get("parts", [""])[0] or "").strip()
            
            if role == "user":
                prefix = self.tr("**Вы:**\n\n")
                # Экранируем блочные цитаты, чтобы они не ломали форматирование
                formatted_content = "> " + content.replace("\n", "\n> ")
                lines.append(prefix + formatted_content)
            elif role == "model":
                prefix = self.tr("**ИИ:**\n\n")
                lines.append(prefix + content)
            elif role == "system":
                prefix = self.tr("*Система: ")
                lines.append(prefix + content.strip() + "*")
            
        return "\n\n---\n\n".join(lines)

    def _export_to_html(self) -> str:
        """Экспортирует чат в формат HTML, используя стили приложения."""
        from markdown import markdown as md_render
        
        # Получаем CSS стили из шаблона чата для консистентности
        # Это грязный, но эффективный способ без дублирования кода
        css_styles = ""
        try:
            # Путь к шаблону чата
            script_dir = os.path.dirname(os.path.abspath(__file__))
            template_path = os.path.join(script_dir, "chat_template.html")
            with open(template_path, 'r', encoding='utf-8') as f:
                content = f.read()
                # Извлекаем содержимое тега <style>
                start = content.find('<style>') + len('<style>')
                end = content.find('</style>')
                if start > -1 and end > -1:
                    css_styles = content[start:end]
        except Exception as e:
            logger.warning(f"Не удалось прочитать CSS из chat_template.html: {e}")

        # Собираем HTML сообщения
        messages_html = []
        for msg in self._chat_history:
            role = msg.get("role")
            content = msg.get("parts", [""])[0] or ""
            
            prefix_text = ""
            if role == "user":
                prefix_text = self.tr('Вы:')
            elif role == "model":
                prefix_text = self.tr('ИИ:')
            
            prefix_html = f'<span class="prefix">{prefix_text}</span>' if prefix_text else ""
            
            content_html = ""
            if role == 'user':
                content_html = f'<pre>{html.escape(content)}</pre>'
            elif role == 'model':
                content_html = md_render(content, extensions=["fenced_code", "codehilite", "nl2br", "tables"])
            elif role == 'system':
                content_html = f'<i>{html.escape(content)}</i>'
            
            messages_html.append(f"""
                <div class="message {role}-message">
                    {prefix_html}
                    <div class="content">
                        {content_html}
                    </div>
                </div>
            """)

        # Собираем финальный HTML документ
        return f"""
<!DOCTYPE html>
<html lang="{self._app_language}">
<head>
    <meta charset="UTF-8">
    <title>{self.tr("Экспорт диалога")}</title>
    <style>
        {css_styles}
        /* Дополнительные стили для экспорта */
        body {{ padding: 20px; }}
        .message {{ max-width: 900px; margin: 0 auto 20px auto; }}
    </style>
</head>
<body>
    <div id="chat-container">
        {''.join(messages_html)}
    </div>
</body>
</html>
        """