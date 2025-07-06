# --- Файл: chat_model.py ---

import os
import re
import json
import logging
import hashlib
from typing import Optional, List, Dict, Any, Tuple

from PySide6.QtCore import QObject, Signal, Slot, QThread, QDir

import db_manager
from dotenv import load_dotenv, set_key, find_dotenv
import google.generativeai as genai
import google.generativeai.types as genai_types
from google.api_core import exceptions as google_exceptions
from chromadb.types import Collection

# Наши модули
from github_manager import GitHubManager
from github.Repository import Repository
from summarizer import SummarizerWorker
from vector_db_manager import VectorDBManager

logger = logging.getLogger(__name__)

CONTEXT_WINDOW_LIMIT = 1048576

class GeminiWorker(QObject):
    response_received = Signal(str, list) # Добавляем original_prompt
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
                logger.info("GeminiWorker: Запрос отменен до старта.")
                self.finished_work.emit()
                return

            generation_config = genai_types.GenerationConfig(max_output_tokens=self.max_output_tokens_config)
            logger.info(f"GeminiWorker: Отправка запроса (max_tokens_response={self.max_output_tokens_config}).")
            
            response = self.model.generate_content(
                self.prompt_parts,
                generation_config=generation_config,
                request_options={"timeout": 180},
            )

            if self._is_cancelled:
                logger.info("GeminiWorker: Запрос отменен после получения ответа.")
                self.finished_work.emit()
                return

            if hasattr(response, "text") and response.text:
                # Передаем оригинальный промпт вместе с ответом
                self.response_received.emit(response.text, self.prompt_parts)
            else:
                reason = self.tr("Неизвестно")
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    reason = response.prompt_feedback.block_reason.name
                self.error_occurred.emit(self.tr("Генерация прервана. Причина: {0}").format(reason))

        except Exception as e:
            if not self._is_cancelled:
                err_msg = self.tr("Ошибка API Gemini: {0} - {1}").format(type(e).__name__, e)
                logger.error(err_msg)
                self.error_occurred.emit(err_msg)
        finally:
            logger.info("GeminiWorker: Завершение работы run().")
            self.finished_work.emit()


class ChatModel(QObject):
    # --- Сигналы ---
    geminiApiKeyStatusChanged = Signal(bool, str)
    githubTokenStatusChanged = Signal(bool, str)
    availableModelsChanged = Signal(list)
    repoDataChanged = Signal(str, str, list)
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

    def __init__(self, app_lang: str = 'en', parent=None): # Добавляем app_lang
        super().__init__(parent)
        self._app_language = app_lang # Сохраняем язык приложения
        # --- Состояние аутентификации ---
        self._dotenv_path: Optional[str] = find_dotenv()
        self._gemini_api_key: Optional[str] = None
        self._github_token: Optional[str] = None
        self._gemini_api_key_loaded: bool = False
        self._github_token_loaded: bool = False

        # --- Состояние сессии ---
        self._repo_object: Optional[Repository] = None
        self._repo_url: Optional[str] = None
        self._repo_branch: Optional[str] = None
        self._available_branches: List[str] = []
        self._chat_history: List[Dict[str, Any]] = []
        self._file_summaries: Dict[str, str] = {}
        self._repo_file_tree: Optional[str] = None # НОВОЕ ПОЛЕ
        self._current_session_filepath: Optional[str] = None
        self._is_dirty: bool = False
        
        # --- Состояние векторной БД ---
        self._vector_db_path: Optional[str] = None
        self._vector_db_manager = VectorDBManager()
        self._current_collection: Optional[Collection] = None

        # --- Настройки ---
        self._model_name: str = "gemini-1.5-flash-latest"
        self._available_models: List[str] = [self._model_name]
        self._max_output_tokens: int = 65536
        self._extensions: Tuple[str, ...] = ( ".py", ".txt", ".md", ".json", ".html", ".css", ".js", ".yaml", ".yml", ".pdf", ".docx")
        self._instructions: str = ""

        # --- Состояние токенов ---
        self._current_prompt_tokens: int = 0
        self._token_limit_for_display: int = CONTEXT_WINDOW_LIMIT

        # --- Воркеры и менеджеры ---
        self._gemini_model: Optional[genai.GenerativeModel] = None
        self._gemini_worker: Optional[GeminiWorker] = None
        self._summarizer_worker: Optional[SummarizerWorker] = None
        self._summarizer_thread: Optional[QThread] = None
        self._github_manager: Optional[GitHubManager] = None
        self._current_request_thread: Optional[QThread] = None

        self._load_credentials()
        self.new_session()

    # --- Управление Ключами и Токенами ---
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
        if not value:
            self.statusMessage.emit(self.tr("{0} не может быть пустым.").format(key_name), 3000)
            return False
        try:
            path = self._dotenv_path or os.path.join(os.getcwd(), ".env")
            if set_key(path, key_name, value, quote_mode="always"):
                self._dotenv_path = path; self._load_credentials()
                self.statusMessage.emit(self.tr("{0} успешно сохранен.").format(key_name), 5000); return True
            else:
                self.statusMessage.emit(self.tr("Ошибка сохранения {0}.").format(key_name), 5000); return False
        except Exception as e:
            self.statusMessage.emit(self.tr("Ошибка сохранения: {0}").format(e), 0); return False

    def save_gemini_api_key(self, key: str) -> bool: return self._save_credential("GEMINI_API_KEY", key)
    def save_github_token(self, token: str) -> bool: return self._save_credential("GITHUB_TOKEN", token)

    # --- Инициализация сервисов ---
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
        if self._github_manager.is_authenticated():
            self.githubTokenStatusChanged.emit(True, self.tr("Токен GitHub: Загружен ({0})").format(self._github_manager.rate_limit_info))
        else:
            self.githubTokenStatusChanged.emit(False, self.tr("Токен GitHub: Ошибка!")); self.statusMessage.emit(self.tr("Неверный токен GitHub."), 5000)

    def _fetch_available_models(self):
        try:
            models = genai.list_models()
            self._available_models = sorted([m.name.replace("models/", "") for m in models if 'generateContent' in m.supported_generation_methods])
            self.availableModelsChanged.emit(self._available_models)
        except Exception as e:
            self.statusMessage.emit(self.tr("Не удалось загрузить список моделей."), 5000)

    @staticmethod
    def _generate_collection_name(repo_url: str, branch: str) -> str:
        """Создает детерминированное и безопасное имя для коллекции ChromaDB."""
        base_string = f"{repo_url.lower()}_{branch.lower()}"
        safe_string = re.sub(r'[^a-zA-Z0-9_-]', '_', base_string)
        if len(safe_string) > 50:
             safe_string = hashlib.sha256(safe_string.encode('utf-8')).hexdigest()[:50]
        return f"repo_{safe_string}"

    # --- Анализ репозитория ---
    def start_repository_analysis(self):
        if not all([self._github_manager, self._repo_object, self._repo_branch, self._gemini_model]):
            self.analysisError.emit(self.tr("Не все компоненты готовы к анализу (репозиторий, ветка, модель Gemini)."))
            return
        if self._summarizer_thread and self._summarizer_thread.isRunning():
            self.statusMessage.emit(self.tr("Анализ уже запущен."), 3000); return

        collection_name = self._generate_collection_name(self._repo_url, self._repo_branch)
        logger.info(f"Подготовка векторной коллекции: {collection_name}")
        self._vector_db_manager.delete_collection(collection_name)
        self._current_collection = self._vector_db_manager.create_or_get_collection(collection_name)
        if not self._current_collection:
            self.analysisError.emit(self.tr("Не удалось создать векторную базу данных для анализа.")); return
        
        files_to_process, _ = self._github_manager.get_repo_file_tree(self._repo_object, self._repo_branch, self._extensions)
        if not files_to_process:
            self.analysisError.emit(self.tr("В этой ветке не найдено файлов с указанными расширениями.")); return
        
        # Получаем и сохраняем дерево файлов
        self.apiIntermediateStep.emit(self.tr("Получение дерева файлов репозитория..."))
        self._repo_file_tree = self._github_manager.get_repo_file_tree_text(self._repo_object, self._repo_branch)

        self._file_summaries = {}; self.fileSummariesChanged.emit(self._file_summaries)
        self._mark_dirty(); self.analysisStarted.emit()
        self.statusMessage.emit(self.tr("Начат анализ {0} файлов в '{1}'...").format(len(files_to_process), self._repo_branch), 0)

        # --- НОВЫЙ СПОСОБ ЗАПУСКА ВОРКЕРА ---
        self._summarizer_thread = QThread()
        self._summarizer_worker = SummarizerWorker(
            github_manager=self._github_manager, repo=self._repo_object,
            branch_name=self._repo_branch, files_to_summarize=files_to_process,
            gemini_api_key=self._gemini_api_key, model_name=self._model_name,
            app_lang=self._app_language
        )
        self._summarizer_worker.moveToThread(self._summarizer_thread)

        # Подключение сигналов
        self._summarizer_worker.file_summarized.connect(self._on_file_summarized)
        self._summarizer_worker.documents_for_db_ready.connect(self._on_documents_for_db_ready)
        self._summarizer_worker.progress_updated.connect(self._on_analysis_progress)
        self._summarizer_worker.error_occurred.connect(self.analysisError)
        self._summarizer_worker.finished.connect(self._on_analysis_finished)
        
        # Связываем запуск/остановку потока
        self._summarizer_thread.started.connect(self._summarizer_worker.run)
        self._summarizer_worker.finished.connect(self._summarizer_thread.quit)
        self._summarizer_thread.finished.connect(self._summarizer_worker.deleteLater)
        self._summarizer_thread.finished.connect(self._summarizer_thread.deleteLater)
        self._summarizer_thread.finished.connect(self._cleanup_summarizer_thread)

        self._summarizer_thread.start()

    def cancel_analysis(self):
        if self._summarizer_thread and self._summarizer_thread.isRunning():
            if self._summarizer_worker:
                self._summarizer_worker.cancel()
            self.statusMessage.emit(self.tr("Отмена анализа..."), 3000)

    def _cleanup_summarizer_thread(self):
        logger.debug("Очистка ссылок на воркер и поток анализа.")
        self._summarizer_worker = None
        self._summarizer_thread = None
            
    @Slot(str, str)
    def _on_file_summarized(self, file_path: str, summary: str):
        self._file_summaries[file_path] = summary; self._mark_dirty(); self.fileSummariesChanged.emit(self._file_summaries)

    @Slot(list, list)
    def _on_documents_for_db_ready(self, documents: List[str], metadatas: List[Dict[str, Any]]):
        if self._current_collection is not None and self._vector_db_manager:
            logger.debug(f"Получено {len(documents)} документов от воркера для добавления в БД.")
            self._vector_db_manager.add_documents_batch(self._current_collection, documents, metadatas)
            self._mark_dirty()
        else:
            if self._current_collection is None:
                logger.error("Получены документы для БД, но текущая коллекция не установлена!")
            if self._vector_db_manager is None:
                logger.error("Получены документы для БД, но VectorDBManager не инициализирован!")

    @Slot(int, int)
    def _on_analysis_progress(self, processed: int, total: int):
        self.analysisProgressUpdated.emit(processed, total, "")
        self.statusMessage.emit(self.tr("Анализ... {0}/{1}").format(processed, total), 0)

    @Slot()
    def _on_analysis_finished(self):
        self.analysisFinished.emit()
        self.statusMessage.emit(self.tr("Анализ репозитория завершен."), 5000)
        # Очистка теперь происходит в _cleanup_summarizer_thread

    # --- RAG и основной запрос к API ---
    def send_request_to_api(self, user_input: str):
        if not self._is_ready_for_request(): return
        
        user_input_stripped = user_input.strip()
        if not user_input_stripped:
            self.statusMessage.emit(self.tr("Введите ваш запрос."), 3000); return
            
        self.apiRequestStarted.emit(); self.add_user_message(user_input_stripped)
        
        self.apiIntermediateStep.emit(self.tr("Этап 1: Поиск релевантных фрагментов в базе знаний..."))
        retrieved_docs = self._vector_db_manager.query(self._current_collection, user_input_stripped, n_results=30)
        
        if not retrieved_docs:
            self.apiIntermediateStep.emit(self.tr("Релевантных фрагментов не найдено. Ответ будет основан на истории чата."))
            final_context = ""
        else:
            self.apiIntermediateStep.emit(self.tr("Этап 2: Формирование контекста из {0} фрагментов...").format(len(retrieved_docs)))
            final_context = self._build_final_context_from_docs(retrieved_docs)

        final_prompt_parts = self._build_final_prompt(final_context)
        
        if not final_prompt_parts:
            self.apiErrorOccurred.emit(self.tr("Ошибка: Не удалось сформировать запрос. Слишком большой объем данных.")); self.apiRequestFinished.emit(); return

        self._start_gemini_worker(final_prompt_parts)

    def _start_gemini_worker(self, prompt: List[Dict[str, Any]]):
        """Запускает GeminiWorker с заданным промптом."""
        self._gemini_worker = GeminiWorker(self._gemini_model, prompt, self._max_output_tokens)
        thread = QThread()
        self._gemini_worker.moveToThread(thread)
        # Подключаем универсальный обработчик
        self._gemini_worker.response_received.connect(self._on_api_response_received)
        
        self._gemini_worker.error_occurred.connect(self._handle_final_api_error)
        thread.started.connect(self._gemini_worker.run)
        self._gemini_worker.finished_work.connect(thread.quit)
        self._gemini_worker.finished_work.connect(self._handle_worker_finished)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_request_thread)
        thread.start()
        self._current_request_thread = thread
        
    def _is_ready_for_request(self) -> bool:
        if self._current_request_thread and self._current_request_thread.isRunning():
            self.statusMessage.emit(self.tr("Дождитесь завершения предыдущего запроса."), 3000); return False
        if not self._gemini_api_key_loaded or not self._github_token_loaded:
            self.apiErrorOccurred.emit(self.tr("Ключи API не загружены.")); return False
        if not self._repo_url or not self._current_collection:
            self.apiErrorOccurred.emit(self.tr("Репозиторий не проанализирован. Нажмите 'Анализировать'.")); return False
        if self._vector_db_manager.get_collection_doc_count(self._current_collection) == 0:
            self.apiErrorOccurred.emit(self.tr("База знаний пуста. Запустите анализ.")); return False
        return True

    def _build_final_context_from_docs(self, docs: List[Dict[str, Any]]) -> str:
        context_parts = []
        included_docs = set()
        
        logger.info(self.tr("Извлеченные релевантные документы для контекста ({0} шт.):").format(len(docs)))
        for i, doc in enumerate(sorted(docs, key=lambda x: x.get('distance', 1.0))):
            file_path = doc.get('metadata', {}).get('file_path', self.tr('Неизвестный файл'))
            doc_type = doc.get('metadata', {}).get('type', 'chunk')
            distance = doc.get('distance', -1)
            content_preview = doc.get('document', '')[:100].replace('\n', ' ')
            logger.info(self.tr("  {0}. Файл: '{1}', Тип: '{2}', Дистанция: {3:.4f}, Содержимое: '{4}...'").format(
                i + 1, file_path, doc_type, distance, content_preview))

        for doc in sorted(docs, key=lambda x: x.get('distance', 1.0)):
            doc_content = doc.get('document', '')
            if doc_content in included_docs: continue
            
            metadata = doc.get('metadata', {})
            file_path = metadata.get('file_path', self.tr('Неизвестный файл'))
            doc_type = metadata.get('type', 'chunk')
            
            header = self.tr("--- Фрагмент из файла: {0} (Тип: {1}) ---\n").format(file_path, doc_type)
            context_parts.append(header + doc_content + "\n" + "-"*20 + "\n")
            included_docs.add(doc_content)
        
        return "".join(context_parts)

    def _build_final_prompt(self, context_str: str) -> List[Dict[str, Any]]:
        if not self._gemini_model: return []

        def clean_message(msg: Dict[str, Any]) -> Dict[str, Any]:
            return {"role": msg["role"], "parts": msg["parts"]}

        prompt_token_budget = CONTEXT_WINDOW_LIMIT - self._max_output_tokens
        current_tokens = 0
        
        instructions_part = []
        lang_instruction_phrase = self.tr("на русском языке") if self._app_language == 'ru' else self.tr("in English")
        
        base_system_instructions = self.tr(
            "Ты — мой высококвалифицированный ассистент по программированию и анализу кода. "
            "Тебе предоставлен контекст, который включает:\n"
            "1. Полное дерево файлов репозитория.\n"
            "2. Фрагменты кода (chunks) и краткие описания (summaries) некоторых файлов, которые я счел релевантными.\n\n"
            "Твоя задача — отвечать на мои вопросы о коде.\n\n"
            "**КРИТИЧЕСКИ ВАЖНОЕ ПРАВИЛО:**\n"
            "Если для ответа на вопрос тебе не хватает информации из предоставленных фрагментов, "
            "но ты видишь нужный файл в **дереве файлов**, ты должен запросить его содержимое. "
            "Для этого твой ответ должен быть ТОЛЬКО JSON-объектом строго следующего формата:\n"
            "```json\n"
            '{{"action": "request_file", "file_path": "полный/путь/к/файлу.py"}}\n'
            "```\n"
            "Не добавляй никакого другого текста или объяснений, кроме этого JSON. Я автоматически обработаю твой запрос, "
            "предоставлю тебе содержимое файла, и ты сможешь дать окончательный ответ на мой первоначальный вопрос.\n\n"
            "Если же информации достаточно, или ты не уверен, какой файл нужен, или вопрос не о коде, "
            "отвечай как обычно, основываясь на предоставленном контексте. "
            "Всегда объясняй, что и почему ты предлагаешь изменить. "
            "Предлагай коммиты в стиле Conventional Commits, когда это уместно."
        )

        user_instructions_text = self._instructions.strip()
        if user_instructions_text:
            base_system_instructions += self.tr("\n\nДополнительные пользовательские инструкции:\n{0}\n").format(user_instructions_text)
        
        final_language_instruction = self.tr("Пожалуйста, отвечай на все вопросы {0}, если не указано иное.").format(lang_instruction_phrase)
        combined_instructions = f"{base_system_instructions.strip()}\n\n{final_language_instruction}"
        
        instructions_part.extend([
            {"role": "user", "parts": [combined_instructions.strip()]},
            {"role": "model", "parts": [self.tr("ОК. Я готов к работе. Правила запроса файлов и язык приняты.")]}
        ])
        
        # Добавляем дерево файлов в контекст
        if self._repo_file_tree:
            file_tree_part = [
                {"role": "user", "parts": [self.tr("**Полное дерево файлов проекта:**\n```\n{0}\n```").format(self._repo_file_tree)]},
                {"role": "model", "parts": [self.tr("OK. Дерево файлов проекта получено.")]}
            ]
            instructions_part.extend(file_tree_part)
        
        history_to_consider = self._chat_history[:-1] 
        last_user_message = self._chat_history[-1]
        cleaned_last_user_message = clean_message(last_user_message)

        try:
            base_parts = instructions_part + [cleaned_last_user_message]
            base_tokens = self._gemini_model.count_tokens(base_parts).total_tokens
            current_tokens += base_tokens
        except Exception as e:
            if current_tokens > prompt_token_budget:
                self.apiErrorOccurred.emit(self.tr("Ошибка: Инструкции и последний вопрос уже превышают лимит токенов ({0}).").format(current_tokens))
                return []

        context_part = []
        if context_str:
            context_wrapper = [
                {"role": "user", "parts": [self.tr("**Контекст из релевантных фрагментов проекта:**\n{0}").format(context_str)]},
                {"role": "model", "parts": [self.tr("OK. Контекст из фрагментов получен.")]}
            ]
            try:
                context_tokens = self._gemini_model.count_tokens(context_wrapper).total_tokens
                if current_tokens + context_tokens <= prompt_token_budget:
                    context_part = context_wrapper; current_tokens += context_tokens
                else:
                    self.apiIntermediateStep.emit(self.tr("ПРЕДУПРЕЖДЕНИЕ: Контекст из файлов слишком большой и будет проигнорирован."))
            except Exception: pass

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

        final_prompt_parts = instructions_part + context_part + history_part + [cleaned_last_user_message]
        self.tokenCountUpdated.emit(current_tokens, CONTEXT_WINDOW_LIMIT)
        return final_prompt_parts
    
    @Slot(str, list)
    def _on_api_response_received(self, response_text: str, original_prompt: List[Dict[str, Any]]):
        """Универсальный обработчик ответа от API. Либо обрабатывает JSON-запрос, либо отдает текст."""
        # Пытаемся распарсить как JSON
        try:
            # Ищем JSON внутри ```json ... ```
            match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if match:
                json_str = match.group(1)
                data = json.loads(json_str)
                action = data.get("action")
                file_path = data.get("file_path")

                if action == "request_file" and file_path:
                    logger.info(f"ИИ запросил файл: {file_path}")
                    self.apiIntermediateStep.emit(self.tr("ИИ запросил файл: {0}. Получаю содержимое...").format(file_path))
                    self._handle_file_request(file_path, original_prompt)
                    return
            
            # Если не нашли в ```json```, пробуем парсить строку напрямую
            data = json.loads(response_text)
            action = data.get("action")
            file_path = data.get("file_path")
            if action == "request_file" and file_path:
                logger.info(f"ИИ запросил файл: {file_path}")
                self.apiIntermediateStep.emit(self.tr("ИИ запросил файл: {0}. Получаю содержимое...").format(file_path))
                self._handle_file_request(file_path, original_prompt)
                return

        except (json.JSONDecodeError, AttributeError):
            # Это не JSON-запрос, обрабатываем как обычный текстовый ответ
            self._handle_final_api_response(response_text)

    def _handle_file_request(self, file_path: str, original_prompt: List[Dict[str, Any]]):
        """Обрабатывает JSON-запрос файла от ИИ."""
        if not self._github_manager or not self._repo_object or not self._repo_branch:
            self.apiErrorOccurred.emit(self.tr("Ошибка: Невозможно получить файл, нет данных о репозитории."))
            return
        
        # НОРМАЛИЗАЦИЯ ПУТИ: Удаляем префикс с именем репозитория, если он есть
        path_to_fetch = file_path.removeprefix(f"{self._repo_object.name}/")

        content = self._github_manager.get_file_content(self._repo_object, file_path, self._repo_branch)

        if content is None:
            error_msg = self.tr("Не удалось получить содержимое запрошенного файла '{0}'. Возможно, он не существует или доступ запрещен.").format(file_path)
            self.apiErrorOccurred.emit(error_msg)
            self.apiRequestFinished.emit()
            return
        
        # Проверка размера файла (например, 200КБ лимит)
        if len(content) > 200 * 1024:
            error_msg = self.tr("Запрошенный файл '{0}' слишком большой ({1:.1f} KB). Обработка прервана.").format(file_path, len(content)/1024)
            self.apiErrorOccurred.emit(error_msg)
            self.apiRequestFinished.emit()
            return

        self.apiIntermediateStep.emit(self.tr("Файл '{0}' получен. Формирую новый запрос к ИИ...").format(file_path))

        # Формируем новый контекст с содержимым файла
        file_content_part = {
            "role": "user",
            "parts": [self.tr("Вот запрошенное содержимое файла '{0}':\n\n```\n{1}\n```\n\nТеперь, пожалуйста, ответь на мой первоначальный вопрос, используя эту новую информацию.").format(file_path, content)]
        }
        
        # Вставляем новый контекст перед последним сообщением пользователя
        new_prompt = original_prompt[:-1] + [file_content_part] + original_prompt[-1:]
        
        # Перезапускаем воркер с новым, дополненным промптом
        self._start_gemini_worker(new_prompt)

    @Slot(str)
    def _handle_final_api_response(self, response_text: str):
        self.add_model_response(response_text); self.apiResponseReceived.emit(response_text)

    @Slot(str)
    def _handle_final_api_error(self, error_message: str): self.apiErrorOccurred.emit(error_message)
    @Slot()
    def _handle_worker_finished(self): self.apiRequestFinished.emit()
    @Slot()
    def _cleanup_request_thread(self): self._gemini_worker = None; self._current_request_thread = None

    # --- Управление состоянием (геттеры/сеттеры) ---
    def set_repo_url(self, url: str):
        if not url or url == self._repo_url: return
        if not self._github_manager: self.statusMessage.emit(self.tr("GitHub не инициализирован."), 5000); return

        repo_data = self._github_manager.get_repo(url)
        if not repo_data:
            self.statusMessage.emit(self.tr("Не удалось получить доступ к репозиторию."), 5000)
            self._repo_object, self._repo_url, self._repo_branch, self._available_branches = None, url, None, []
            self.repoDataChanged.emit(url, "", []); return

        self._repo_object, branch_from_url = repo_data; self._repo_url = self._repo_object.html_url
        self.statusMessage.emit(self.tr("Загрузка списка веток..."), 0)
        self._available_branches = self._github_manager.get_available_branches(self._repo_object)
        
        self._repo_branch = (branch_from_url if branch_from_url in self._available_branches 
                             else self._repo_object.default_branch)

        self._clear_analysis_data()
        self.repoDataChanged.emit(self._repo_url, self._repo_branch, self._available_branches)
        self.statusMessage.emit(self.tr("Репозиторий '{0}' загружен.").format(self._repo_object.full_name), 5000)

    def set_repo_branch(self, branch_name: str):
        if not branch_name or branch_name == self._repo_branch: return
        self._repo_branch = branch_name
        self._clear_analysis_data()
        self.repoDataChanged.emit(self._repo_url, self._repo_branch, self._available_branches)
        self.statusMessage.emit(self.tr("Выбрана ветка: {0}. Требуется повторный анализ.").format(branch_name), 0)

    # --- Управление историей чата ---
    def add_user_message(self, text: str):
        if not text: return
        self._chat_history.append({"role": "user", "parts": [text], "excluded": False})
        self._mark_dirty(); self.historyChanged.emit(self.get_chat_history()); self._update_token_count()
    def add_model_response(self, text: str):
        self._chat_history.append({"role": "model", "parts": [text or ""], "excluded": False})
        self._mark_dirty(); self.historyChanged.emit(self.get_chat_history())
    def toggle_api_exclusion(self, index: int):
        if 0 <= index < len(self._chat_history):
            self._chat_history[index]["excluded"] = not self._chat_history[index].get("excluded", False)
            self._mark_dirty(); self.historyChanged.emit(self.get_chat_history()); self._update_token_count()

    def _update_token_count(self):
        if not self._gemini_model: self.tokenCountUpdated.emit(0, self._token_limit_for_display); return
        prompt_parts = []
        if self._instructions: prompt_parts.extend([{"role": "user", "parts": [f"**...**\n{self._instructions}"]}, {"role": "model", "parts": ["OK."]}])
        for msg in self._chat_history:
            if not msg.get("excluded", False): prompt_parts.append({"role": msg["role"], "parts": msg["parts"]})
        try:
            self._current_prompt_tokens = self._gemini_model.count_tokens(prompt_parts).total_tokens if prompt_parts else 0
            self.tokenCountUpdated.emit(self._current_prompt_tokens, self._token_limit_for_display)
        except Exception: self.tokenCountUpdated.emit(0, self._token_limit_for_display)

    # --- Управление Сессиями ---
    def new_session(self):
        self._repo_url, self._repo_object, self._repo_branch, self._available_branches = None, None, None, []
        self._chat_history = []
        self._clear_analysis_data()
        self._current_session_filepath = None
        temp_dir_name = f"temp_session_{os.urandom(8).hex()}"
        self._vector_db_path = os.path.join(QDir.tempPath(), "GitGeminiPro", temp_dir_name)
        self._vector_db_manager.set_db_path(self._vector_db_path)
        
        self._extensions = (".py", ".txt", ".md", ".json", ".html", ".css", ".js", ".yaml", ".yml")
        self._model_name = self._available_models[0] if self._available_models else "gemini-1.5-flash-latest"
        self._max_output_tokens = 65536; self._instructions = ""
        self._is_dirty = False
        
        self.sessionLoaded.emit()
        self.repoDataChanged.emit("", "", []); self._update_token_count()
        self.statusMessage.emit(self.tr("Новая сессия создана."), 3000)

    def _clear_analysis_data(self):
        self._file_summaries = {}
        self._repo_file_tree = None # Сбрасываем дерево файлов
        if self._current_collection:
             collection_name = self._current_collection.name
             logger.info(f"Очистка данных анализа: удаление коллекции '{collection_name}'")
             self._vector_db_manager.delete_collection(collection_name)
        self._current_collection = None
        self.fileSummariesChanged.emit(self._file_summaries)
        self._mark_dirty()

    def load_session(self, filepath: str):
        loaded_data = db_manager.load_session_data(filepath)
        if not loaded_data:
            self.sessionError.emit(self.tr("Не удалось загрузить сессию: {0}").format(filepath))
            return

        meta, msgs, summaries = loaded_data
        
        self._chat_history = msgs
        self._file_summaries = summaries
        self._repo_file_tree = meta.get("repo_file_tree") # Загружаем дерево файлов
        self._model_name = meta.get("model_name", "gemini-1.5-flash-latest")
        self._max_output_tokens = meta.get("max_output_tokens", 65536)
        self._extensions = tuple(p.strip() for p in meta.get("extensions", ".py").split())
        self._instructions = meta.get("instructions", "")
        self._current_session_filepath = filepath
        self._is_dirty = False

        self._vector_db_path = meta.get("vector_db_path")
        if not self._vector_db_path:
            self._vector_db_path = filepath.replace(db_manager.SESSION_EXTENSION, "_vectordb")
        self._vector_db_manager.set_db_path(self._vector_db_path)

        self._repo_url = meta.get("repo_url")
        self._repo_branch = meta.get("repo_branch")
        
        if self._repo_url and self._github_manager:
            repo_data = self._github_manager.get_repo(self._repo_url)
            if repo_data:
                self._repo_object, _ = repo_data
                self._available_branches = self._github_manager.get_available_branches(self._repo_object)
                if self._repo_branch not in self._available_branches:
                    self.statusMessage.emit(self.tr("Предупреждение: сохраненная ветка '{0}' не найдена. Установлена ветка по умолчанию.").format(self._repo_branch), 5000)
                    self._repo_branch = self._repo_object.default_branch
            else:
                self.statusMessage.emit(self.tr("Предупреждение: не удалось получить доступ к репозиторию '{0}'.").format(self._repo_url), 5000)
                self._repo_object, self._available_branches = None, []
        else:
            self._repo_object, self._available_branches = None, []

        if self._repo_url and self._repo_branch:
            collection_name = self._generate_collection_name(self._repo_url, self._repo_branch)
            self._current_collection = self._vector_db_manager.create_or_get_collection(collection_name)
        else:
            self._current_collection = None

        self.sessionLoaded.emit() 
        self.repoDataChanged.emit(self._repo_url, self._repo_branch, self._available_branches)
        self.fileSummariesChanged.emit(self._file_summaries)
        self._update_token_count()
        self.statusMessage.emit(self.tr("Сессия '{0}' загружена.").format(os.path.basename(filepath)), 5000)

    def save_session(self, filepath: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        save_path = filepath or self._current_session_filepath
        if not save_path: return False, None
        
        if not self._vector_db_path or "temp_session" in self._vector_db_path:
             self._vector_db_path = save_path.replace(db_manager.SESSION_EXTENSION, "_vectordb")
             self._vector_db_manager.set_db_path(self._vector_db_path)
        
        metadata = {
            "repo_url": self._repo_url, "repo_branch": self._repo_branch,
            "vector_db_path": self._vector_db_path, "repo_file_tree": self._repo_file_tree,
            "model_name": self._model_name, "max_output_tokens": self._max_output_tokens, 
            "extensions": " ".join(self._extensions), "instructions": self._instructions
        }
        
        if db_manager.save_session_data(save_path, metadata, self._chat_history, self._file_summaries):
            self._current_session_filepath = save_path; self._is_dirty = False
            self.sessionStateChanged.emit(save_path, False)
            self.statusMessage.emit(self.tr("Сессия сохранена."), 5000); return True, save_path
        else:
            self.sessionError.emit(self.tr("Не удалось сохранить сессию: {0}").format(save_path)); return False, None

    # --- Остальные геттеры/сеттеры ---
    def get_repo_url(self) -> Optional[str]: return self._repo_url
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
        if text != self._instructions: self._instructions = text; self._mark_dirty(); self._update_token_count()
    def get_chat_history(self) -> List[Dict[str, Any]]: return self._chat_history[:]
    def _mark_dirty(self):
        if not self._is_dirty: self._is_dirty = True; self.sessionStateChanged.emit(self._current_session_filepath, True)
    def is_dirty(self) -> bool: return self._is_dirty
    def get_current_session_filepath(self) -> Optional[str]: return self._current_session_filepath