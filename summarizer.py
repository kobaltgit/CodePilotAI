# --- Файл: summarizer.py ---

import os
import sys
import logging
from typing import Dict, Optional, List, Any, Tuple

from PySide6.QtCore import QObject, Signal, Slot, QThread

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

# Импортируем наши сплиттеры
from code_splitter import TreeSitterSplitter, RecursiveCharacterSplitter

# Настраиваем логгер для этого модуля
logger = logging.getLogger(__name__)

# Промпт для создания саммари файла (остается без изменений)
SUMMARIZATION_PROMPT_RU = """
Проанализируй содержимое этого файла:

--- НАЧАЛО ФАЙЛА: {file_path} ---
{file_content}
--- КОНЕЦ ФАЙЛА ---

Создай для него краткое, но емкое саммари (2-4 предложения).
В саммари обязательно отрази:
1. Основное назначение файла (что он делает, за что отвечает).
2. Ключевые классы, функции или компоненты, которые в нем определены.
3. Его основные зависимости от других частей проекта, если они очевидны из кода.

Ответ должен быть только текстом саммари, без лишних фраз и вступлений.
"""

SUMMARIZATION_PROMPT_EN = """
Analyze the contents of this file:

--- START OF FILE: {file_path} ---
{file_content}
--- END OF FILE ---

Create a brief but comprehensive summary (2-4 sentences) for it.
In the summary, be sure to reflect:
1. The main purpose of the file (what it does, what it is responsible for).
2. Key classes, functions, or components defined in it.
3. Its main dependencies on other parts of the project, if they are evident from the code.

The response should be only the summary text, without any extra phrases or introductions.
"""

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


class SummarizerWorker(QThread):
    """
    Рабочий поток, который выполняет анализ контента файлов.
    Теперь он сам читает файлы из переданных путей.
    """
    progress_updated = Signal(int, int, str)
    file_summarized = Signal(str, str)
    context_data_ready = Signal(list)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self,
                 file_paths: List[str],
                 project_type: str,
                 project_source_path: str,
                 repo_object: Optional[Any],
                 repo_branch: Optional[str],
                 github_manager: Optional[Any],
                 rag_enabled: bool,
                 gemini_api_key: str,
                 model_name: str,
                 app_lang: str = 'en'):
        super().__init__()
        self.file_paths = file_paths
        self.project_type = project_type
        self.project_source_path = project_source_path
        self.repo_object = repo_object
        self.repo_branch = repo_branch
        self.github_manager = github_manager
        self.rag_enabled = rag_enabled
        self.gemini_api_key = gemini_api_key
        self.model_name = model_name
        self._is_cancelled = False
        self.generative_model: Optional[genai.GenerativeModel] = None

        self.ts_splitter: Optional[TreeSitterSplitter] = None
        self.fallback_splitter = RecursiveCharacterSplitter(chunk_size=1000, chunk_overlap=150)
        self.summarization_prompt_template = SUMMARIZATION_PROMPT_RU if app_lang == 'ru' else SUMMARIZATION_PROMPT_EN
        self._initialize_tree_sitter()

    def _initialize_tree_sitter(self):
        """Пытается инициализировать TreeSitterSplitter."""
        try:
            if getattr(sys, 'frozen', False):
                base_path = os.path.dirname(sys.executable)
            else:
                base_path = os.path.dirname(os.path.abspath(__file__))

            system = sys.platform
            if system == 'win32':
                lib_name = 'languages.dll'
            elif system == 'darwin':
                lib_name = 'languages.dylib'
            else:
                lib_name = 'languages.so'
            
            lib_path = os.path.join(base_path, 'resources', 'grammars', lib_name)

            if os.path.exists(lib_path):
                self.ts_splitter = TreeSitterSplitter(lib_path)
                logger.info("Tree-sitter сплиттер успешно инициализирован.")
            else:
                logger.warning(f"Скомпилированная библиотека грамматик не найдена по пути '{lib_path}'. Будет использоваться только рекурсивный сплиттер.")
                self.ts_splitter = None
        except Exception as e:
            logger.error(f"Ошибка при инициализации TreeSitterSplitter: {e}. Будет использоваться только рекурсивный сплиттер.")
            self.ts_splitter = None
    
    def cancel(self):
        """Запрашивает отмену операции."""
        logger.info(self.tr("Получен запрос на отмену анализа."))
        self._is_cancelled = True

    @Slot()
    def run(self):
        """Основной метод потока, выполняющий чтение и анализ файлов."""
        logger.info(self.tr("Запуск потока анализа для {0} файлов. Режим RAG: {1}").format(len(self.file_paths), self.rag_enabled))

        try:
            if self.rag_enabled:
                if self._is_cancelled: return
                try:
                    genai.configure(api_key=self.gemini_api_key)
                    self.generative_model = genai.GenerativeModel(self.model_name)
                except Exception as e:
                    self.error_occurred.emit(self.tr("Ошибка инициализации модели Gemini: {0}").format(e)); return
            
            if self._is_cancelled: return

            total_count = len(self.file_paths)
            for i, file_path in enumerate(self.file_paths):
                if self._is_cancelled: break
                
                self.progress_updated.emit(i, total_count, os.path.basename(file_path))
                content, read_error = self._read_file_content(file_path)

                if self._is_cancelled: break

                if read_error:
                    self.error_occurred.emit(read_error)
                    continue
                if content is None: continue

                display_path = os.path.relpath(file_path, self.project_source_path) if self.project_type == 'local' else file_path
                context_for_this_file = []

                if self.rag_enabled:
                    summary_text = self._create_summary(display_path, content)
                    if self._is_cancelled: break
                    self.file_summarized.emit(display_path, summary_text)
                    context_for_this_file.append({'file_path': display_path, 'type': 'summary', 'chunk_num': 0, 'content': summary_text})
                    
                    if content.strip():
                        chunks = self._split_into_chunks(display_path, content)
                        for j, chunk_text in enumerate(chunks):
                            context_for_this_file.append({'file_path': display_path, 'type': 'chunk', 'chunk_num': j + 1, 'content': chunk_text})
                else:
                    context_for_this_file.append({'file_path': display_path, 'type': 'full_file', 'chunk_num': 0, 'content': content})
                    self.file_summarized.emit(display_path, self.tr("(Режим RAG отключен, файл используется целиком)"))

                if context_for_this_file: self.context_data_ready.emit(context_for_this_file)

            if self._is_cancelled:
                logger.warning(self.tr("Операция анализа была отменена пользователем."))
            else:
                self.progress_updated.emit(total_count, total_count, self.tr("Завершено"))
        
        finally:
            logger.info(self.tr("Поток анализа завершил свою работу."))
            self.finished.emit()

    def _read_file_content(self, file_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Читает контент файла из локального хранилища или GitHub."""
        try:
            from io import BytesIO

            content_bytes = None
            if self.project_type == 'local':
                with open(file_path, 'rb') as f:
                    content_bytes = f.read()
            elif self.project_type == 'github':
                content_bytes = self.github_manager.get_file_content(self.repo_object, file_path, self.repo_branch)

            if content_bytes is None: return None, None

            file_ext = os.path.splitext(file_path.lower())[1]
            if file_ext == '.docx':
                if docx is None: return None, self.tr("Пропущен DOCX (библиотека не установлена): {0}").format(os.path.basename(file_path))
                doc = docx.Document(BytesIO(content_bytes))
                return '\n'.join([p.text for p in doc.paragraphs]), None
            elif file_ext == '.pdf':
                if PdfReader is None: return None, self.tr("Пропущен PDF (библиотека не установлена): {0}").format(os.path.basename(file_path))
                reader = PdfReader(BytesIO(content_bytes))
                if reader.is_encrypted: return None, self.tr("Пропущен зашифрованный PDF: {0}").format(os.path.basename(file_path))
                return '\n'.join([p.extract_text() or "" for p in reader.pages]), None
            else:
                return content_bytes.decode('utf-8', errors='ignore'), None
        except Exception as e:
            return None, self.tr("Ошибка чтения файла {0}: {1}").format(os.path.basename(file_path), e)

    def _create_summary(self, file_path: str, content: str) -> str:
        """Создает саммари для контента файла с надежной обработкой ответа."""
        if not content.strip() or not self.generative_model:
            return self.tr("(Файл пуст или модель недоступна)")
        
        prompt = self.summarization_prompt_template.format(file_path=file_path, file_content=content)
        try:
            # Проверяем на отмену прямо перед отправкой запроса
            if self._is_cancelled: return self.tr("(Отменено)")
            
            response = self.generative_model.generate_content(prompt, request_options={"timeout": 180})

            # Проверяем на отмену сразу после получения ответа
            if self._is_cancelled: return self.tr("(Отменено)")

            # ГЛАВНОЕ ИЗМЕНЕНИЕ: Надежная проверка наличия контента
            if hasattr(response, "text") and response.text:
                return response.text.strip()
            else:
                # Если текста нет, выясняем причину и сообщаем об этом, не падая
                reason_text = self.tr("неизвестна")
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    reason_text = response.prompt_feedback.block_reason.name
                
                error_message = self.tr("Ошибка саммаризации для '{0}': Ответ API не содержит текста (причина: {1}).").format(file_path, reason_text)
                self.error_occurred.emit(error_message)
                return self.tr("(Ошибка: ответ API пуст)")

        except google_exceptions.ResourceExhausted as e:
            msg = self.tr("Исчерпаны квоты API Gemini. Прерывание. Ошибка: {0}").format(e)
            self.error_occurred.emit(msg)
            self.cancel() # Отменяем весь оставшийся анализ
            return self.tr("(Ошибка: Квоты API исчерпаны)")
        except Exception as e:
            # Это отловит другие неожиданные ошибки, включая сетевые
            error_message = self.tr("Ошибка саммаризации для '{0}': {1}").format(file_path, e)
            self.error_occurred.emit(error_message)
            return self.tr("(Ошибка: {0})").format(type(e).__name__)

    def _split_into_chunks(self, file_path: str, content: str) -> List[str]:
        """Разбивает контент на чанки."""
        _, file_extension = os.path.splitext(file_path)
        language = TreeSitterSplitter.LANGUAGE_MAP.get(file_extension.lower())
        if self.ts_splitter and language and self.ts_splitter.is_language_supported(language):
            return self.ts_splitter.split_text(content, language)
        else:
            return self.fallback_splitter.split_text(content)