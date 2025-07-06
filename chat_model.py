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

# Глобальный лимит контекстного окна
CONTEXT_WINDOW_LIMIT = 1048576

# --- Воркер для Gemini API (без изменений) ---
class GeminiWorker(QObject):
    response_received = Signal(str, list)
    error_occurred = Signal(str)
    finished_work = Signal()

    def __init__(self, model: genai.GenerativeModel, prompt_parts: List[Dict[str, Any]], max_output_tokens: int):
        super().__init__()
        self.model = model
        self.prompt_parts = prompt_parts
        self.max_output_tokens_config = max_output_tokens
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        logger.info("GeminiWorker: Получен запрос на отмену.")

    @Slot()
    def run(self):
        try:
            if self._is_cancelled:
                self.finished_work.emit()
                return

            generation_config = genai_types.GenerationConfig(max_output_tokens=self.max_output_tokens_config)
            response = self.model.generate_content(
                self.prompt_parts,
                generation_config=generation_config,
                request_options={"timeout": 180},
            )

            if self._is_cancelled:
                self.finished_work.emit()
                return

            if hasattr(response, "text") and response.text:
                self.response_received.emit(response.text, self.prompt_parts)
            else:
                reason = "Неизвестно"
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    reason = response.prompt_feedback.block_reason.name
                self.error_occurred.emit(self.tr("Генерация прервана. Причина: {0}").format(reason))

        except Exception as e:
            if not self._is_cancelled:
                err_msg = self.tr("Ошибка API Gemini: {0} - {1}").format(type(e).__name__, e)
                logger.error(err_msg)
                self.error_occurred.emit(err_msg)
        finally:
            self.finished_work.emit()


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
        self._project_context: List[Dict[str, Any]] = []
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
        self._model_name: str = "gemini-1.5-flash-latest"
        self._available_models: List[str] = [self._model_name]
        self._max_output_tokens: int = 65536
        self._extensions: Tuple[str, ...] = tuple()
        self._instructions: str = ""
        self._rag_enabled: bool = True # RAG включен по умолчанию

        # --- Состояние токенов ---
        self._current_prompt_tokens: int = 0
        self._token_limit_for_display: int = CONTEXT_WINDOW_LIMIT

        # --- Воркеры и менеджеры ---
        self._gemini_model: Optional[genai.GenerativeModel] = None
        self._analysis_worker: Optional[SummarizerWorker] = None
        self._analysis_thread: Optional[QThread] = None
        self._github_manager: Optional[GitHubManager] = None
        self._gemini_worker_thread: Optional[QThread] = None

        self._load_credentials()
        self.new_session()

    # --- Управление Ключами и Токенами (без изменений) ---
    def _load_credentials(self):
        load_dotenv(dotenv_path=self._dotenv_path, override=True)
        self._gemini_api_key = os.getenv("GEMINI_API_KEY")
        self._gemini_api_key_loaded = bool(self._gemini_api_key)
        status = self.tr("Загружен") if self._gemini_api_key_loaded else self.tr("Не найден!")
        self.geminiApiKeyStatusChanged.emit(self._gemini_api_key_loaded, self.tr("Ключ API: {0}").format(status))
        if self._gemini_api_key_loaded: self._initialize_gemini()

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

    # --- Инициализация сервисов (без изменений) ---
    def _initialize_gemini(self):
        if not self._gemini_api_key: return
        try:
            genai.configure(api_key=self._gemini_api_key)
            self._gemini_model = genai.GenerativeModel(self._model_name)
            self._fetch_available_models()
        except Exception as e:
            self.statusMessage.emit(self.tr("Ошибка Gemini: {0}").format(e), 0); self._gemini_model = None

    def _initialize_github_manager(self):
        if not self._github_token: return
        self._github_manager = GitHubManager(self._github_token)
        rate_info = self._github_manager.rate_limit_info if self._github_manager.is_authenticated() else self.tr("Ошибка!")
        self.githubTokenStatusChanged.emit(self._github_manager.is_authenticated(), self.tr("Токен GitHub: {0}").format(rate_info))

    def _fetch_available_models(self):
        try:
            models = genai.list_models()
            self._available_models = sorted([m.name.replace("models/", "") for m in models if 'generateContent' in m.supported_generation_methods])
            self.availableModelsChanged.emit(self._available_models)
        except Exception:
            self.statusMessage.emit(self.tr("Не удалось загрузить список моделей."), 5000)

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

        # 2. Получаем контент файлов в зависимости от типа проекта
        self.statusMessage.emit(self.tr("Сбор файлов для анализа..."), 0)
        files_content = self._get_files_content_for_analysis()
        if not files_content:
            self.analysisError.emit(self.tr("Не найдено подходящих файлов для анализа в указанном источнике."))
            return

        # 3. Запускаем SummarizerWorker
        self.analysisStarted.emit()
        self.statusMessage.emit(self.tr("Начат анализ {0} файлов...").format(len(files_content)), 0)

        self._analysis_thread = QThread()
        self._analysis_worker = SummarizerWorker(
            files_content=files_content,
            rag_enabled=self._rag_enabled,
            gemini_api_key=self._gemini_api_key,
            model_name=self._model_name,
            app_lang=self._app_language
        )
        self._analysis_worker.moveToThread(self._analysis_thread)

        # Подключение сигналов
        self._analysis_worker.context_data_ready.connect(self._on_context_data_ready)
        self._analysis_worker.file_summarized.connect(self._on_file_summarized)
        self._analysis_worker.progress_updated.connect(self.analysisProgressUpdated)
        self._analysis_worker.error_occurred.connect(self.analysisError)
        self._analysis_worker.finished.connect(self._on_analysis_finished)

        self._analysis_thread.started.connect(self._analysis_worker.run)
        self._analysis_worker.finished.connect(self._analysis_thread.quit)
        self._analysis_thread.finished.connect(self._analysis_worker.deleteLater)
        self._analysis_thread.finished.connect(self._analysis_thread.deleteLater)

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

    def _get_files_content_for_analysis(self) -> Dict[str, str]:
        """Собирает контент файлов из GitHub или локальной папки."""
        files_content = {}
        if self._project_type == 'github':
            files_to_process, _ = self._github_manager.get_repo_file_tree(self._repo_object, self._repo_branch, self._extensions)
            for file_path in files_to_process:
                content = self._github_manager.get_file_content(self._repo_object, file_path, self._repo_branch)
                if content is not None: files_content[file_path] = content
        elif self._project_type == 'local':
            ignored_dirs = {"venv", ".venv", "__pycache__", ".git", ".vscode", ".idea", "node_modules"}
            for root, dirs, files in os.walk(self._local_path, topdown=True):
                dirs[:] = [d for d in dirs if d not in ignored_dirs]
                for file in files:
                    if file.endswith(self._extensions):
                        file_path_full = os.path.join(root, file)
                        try:
                            with open(file_path_full, 'r', encoding='utf-8', errors='ignore') as f:
                                files_content[os.path.relpath(file_path_full, self._local_path)] = f.read()
                        except Exception as e:
                            logger.warning(self.tr("Не удалось прочитать локальный файл {0}: {1}").format(file, e))
        return files_content

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
        # Этот метод нужен для окна SummariesWindow, которое показывает только саммари.
        # Собираем их отдельно от общего _project_context
        current_summaries = {item['file_path']: item['content'] for item in self._project_context if item['type'] == 'summary'}
        current_summaries[file_path] = summary
        self.fileSummariesChanged.emit(current_summaries)

    @Slot()
    def _on_analysis_finished(self):
        self.analysisFinished.emit()
        self.statusMessage.emit(self.tr("Анализ проекта завершен."), 5000)
        # После завершения анализа обновляем словарь саммари целиком
        final_summaries = {item['file_path']: item['content'] for item in self._project_context if item['type'] == 'summary'}
        self.fileSummariesChanged.emit(final_summaries)


    # --- RAG и основной запрос к API (переработано) ---
    def send_request_to_api(self, user_input: str):
        if not self._is_ready_for_request(): return

        self.add_user_message(user_input.strip())
        self.apiRequestStarted.emit()

        final_prompt_parts = self._build_final_prompt()
        if not final_prompt_parts:
            self.apiErrorOccurred.emit(self.tr("Ошибка: Не удалось сформировать запрос. Слишком большой объем данных."))
            self.apiRequestFinished.emit()
            return

        self._start_gemini_worker(final_prompt_parts)

    def _start_gemini_worker(self, prompt: List[Dict[str, Any]]):
        """Запускает GeminiWorker с заданным промптом."""
        if self._gemini_worker_thread and self._gemini_worker_thread.isRunning():
            return
            
        self._gemini_worker_thread = QThread()
        worker = GeminiWorker(self._gemini_model, prompt, self._max_output_tokens)
        worker.moveToThread(self._gemini_worker_thread)
        
        worker.response_received.connect(self._on_api_response_received)
        worker.error_occurred.connect(self._handle_final_api_error)
        self._gemini_worker_thread.started.connect(worker.run)
        worker.finished_work.connect(self._gemini_worker_thread.quit)
        worker.finished_work.connect(worker.deleteLater)
        self._gemini_worker_thread.finished.connect(self._gemini_worker_thread.deleteLater)
        self._gemini_worker_thread.finished.connect(self.apiRequestFinished)
        
        self._gemini_worker_thread.start()

    def _is_ready_for_request(self) -> bool:
        if self._gemini_worker_thread and self._gemini_worker_thread.isRunning():
            self.statusMessage.emit(self.tr("Дождитесь завершения предыдущего запроса."), 3000); return False
        if not self._gemini_api_key_loaded:
            self.apiErrorOccurred.emit(self.tr("Ключ API не загружен.")); return False
        return True

    def _build_final_prompt(self) -> List[Dict[str, Any]]:
        """Собирает финальный промпт, включая контекст из self._project_context."""
        if not self._gemini_model: return []

        # Функция для очистки сообщений
        def clean_message(msg: Dict[str, Any]) -> Dict[str, Any]:
            return {"role": msg["role"], "parts": msg["parts"]}

        # Бюджет на всё, кроме ответа модели
        prompt_token_budget = CONTEXT_WINDOW_LIMIT - self._max_output_tokens
        current_tokens = 0
        
        # 1. Системные инструкции
        final_prompt_parts = []
        instructions_text = self._build_system_instructions()
        instructions_part = [
            {"role": "user", "parts": [instructions_text]},
            {"role": "model", "parts": [self.tr("OK. Я готов к работе.")]}
        ]
        instr_tokens = self._gemini_model.count_tokens(instructions_part).total_tokens
        if current_tokens + instr_tokens < prompt_token_budget:
            final_prompt_parts.extend(instructions_part)
            current_tokens += instr_tokens

        # 2. Контекст проекта (если есть)
        context_str = self._build_context_string(prompt_token_budget - current_tokens)
        if context_str:
            context_part = [
                {"role": "user", "parts": [self.tr("**Контекст из файлов проекта:**\n{0}").format(context_str)]},
                {"role": "model", "parts": [self.tr("OK. Контекст получен.")]}
            ]
            context_tokens = self._gemini_model.count_tokens(context_part).total_tokens
            if current_tokens + context_tokens < prompt_token_budget:
                 final_prompt_parts.extend(context_part)
                 current_tokens += context_tokens

        # 3. История чата
        history_to_consider = self._chat_history[:-1] # Все, кроме последнего запроса пользователя
        last_user_message = self._chat_history[-1]
        
        history_part = []
        for message in reversed(history_to_consider):
            if message.get("excluded", False): continue
            cleaned_message = clean_message(message)
            try:
                message_tokens = self._gemini_model.count_tokens([cleaned_message]).total_tokens
                if current_tokens + message_tokens <= prompt_token_budget:
                    history_part.insert(0, cleaned_message); current_tokens += message_tokens
                else: break
            except Exception: break
            
        final_prompt_parts.extend(history_part)
        
        # 4. Последний запрос пользователя
        cleaned_last_message = clean_message(last_user_message)
        last_message_tokens = self._gemini_model.count_tokens([cleaned_last_message]).total_tokens
        if current_tokens + last_message_tokens <= prompt_token_budget:
            final_prompt_parts.append(cleaned_last_message)
            current_tokens += last_message_tokens
        else:
            self.apiErrorOccurred.emit(self.tr("Недостаточно места для вашего запроса. Попробуйте исключить сообщения из истории."))
            return []

        self.tokenCountUpdated.emit(current_tokens, CONTEXT_WINDOW_LIMIT)
        return final_prompt_parts

    def _build_system_instructions(self) -> str:
        """Собирает текст системных инструкций."""
        lang_instruction_phrase = self.tr("на русском языке") if self._app_language == 'ru' else "in English"
        base_instructions = self.tr(
            "Ты — мой ассистент по программированию. Анализируй предоставленный контекст и отвечай на вопросы. "
            "Отвечай {0}, если не указано иное.").format(lang_instruction_phrase)
        if self._instructions.strip():
            return f"{base_instructions}\n\nДополнительные инструкции:\n{self._instructions.strip()}"
        return base_instructions

    def _build_context_string(self, budget: int) -> str:
        """Собирает строку контекста из self._project_context, не превышая бюджет токенов."""
        if not self._project_context: return ""
        
        context_parts = []
        current_size = 0

        # Сначала добавляем все саммари (они самые важные и короткие)
        summaries = [item for item in self._project_context if item['type'] == 'summary']
        for summary in summaries:
            header = self.tr("--- Обзор файла: {0} ---\n").format(summary['file_path'])
            text = header + summary['content'] + "\n\n"
            part_size = len(text)
            if current_size + part_size > budget * 4: break # Грубая проверка по символам
            context_parts.append(text)
            current_size += part_size

        # Затем добавляем чанки или полные файлы
        content_items = [item for item in self._project_context if item['type'] in ('chunk', 'full_file')]
        for item in content_items:
            if item['type'] == 'chunk':
                header = self.tr("--- Фрагмент ({0}) из файла: {1} ---\n").format(item['chunk_num'], item['file_path'])
            else: # full_file
                header = self.tr("--- Содержимое файла: {0} ---\n").format(item['file_path'])
            text = header + item['content'] + "\n\n"
            part_size = len(text)
            if current_size + part_size > budget * 4: continue # Пропускаем, если слишком большой
            context_parts.append(text)
            current_size += part_size

        return "".join(context_parts)

    @Slot(str, list)
    def _on_api_response_received(self, response_text: str, original_prompt: List[Dict[str, Any]]):
        self.add_model_response(response_text)
        self.apiResponseReceived.emit(response_text)

    @Slot(str)
    def _handle_final_api_error(self, error_message: str): self.apiErrorOccurred.emit(error_message)

    # --- Управление состоянием (геттеры/сеттеры, переработано) ---
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
        self.fileSummariesChanged.emit({})
        self._mark_dirty()

    # --- Управление историей чата (без изменений) ---
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
    
    # --- Управление Сессиями (переработано) ---
    def new_session(self):
        self._project_type, self._repo_url, self._local_path, self._repo_branch = None, None, None, None
        self._repo_object, self._available_branches = None, []
        self._chat_history = []
        self._project_context = []
        self._current_session_filepath = None
        self._extensions = tuple(ext for ext in [".py", ".txt", ".md", ".json", ".html", ".css", ".js"] if not ext.startswith(('.', '_')))
        self._model_name = "gemini-1.5-flash-latest"
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
        self._model_name = meta.get("model_name", "gemini-1.5-flash-latest")
        self._max_output_tokens = meta.get("max_output_tokens", 65536)
        self._extensions = tuple(p.strip() for p in meta.get("extensions", ".py").split())
        self._instructions = meta.get("instructions", "")
        
        # Загружаем историю и контекст
        self._chat_history = msgs
        self._project_context = context
        
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
        if name and name != self._model_name: self._model_name = name; self._mark_dirty(); self._initialize_gemini()
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