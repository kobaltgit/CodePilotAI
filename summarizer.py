# --- Файл: summarizer.py ---

import os
import logging
from typing import Dict, Optional, List, Any

from PySide6.QtCore import QObject, Signal, Slot

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

from github_manager import GitHubManager
from github.Repository import Repository

# --- НОВЫЕ ИМПОРТЫ ---
from code_splitter import TreeSitterSplitter, RecursiveCharacterSplitter

# Настраиваем логгер для этого модуля
logger = logging.getLogger(__name__)

# Промпт для создания саммари файла. Теперь в двух версиях.
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


# --- СТАРЫЙ КЛАСС SimpleTextSplitter УДАЛЕН ---


# --- ПЕРЕРАБОТАННЫЙ WORKER ---
class SummarizerWorker(QObject):
    """
    Рабочий поток, который выполняет анализ файлов репозитория:
    1. Создает саммари с помощью Gemini.
    2. Разбивает содержимое файла на чанки с помощью подходящего сплиттера.
    3. Отправляет готовые документы и метаданные для добавления в векторную БД.
    """
    # Сигналы
    progress_updated = Signal(int, int)
    file_summarized = Signal(str, str)
    documents_for_db_ready = Signal(list, list)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self,
                 github_manager: GitHubManager,
                 repo: Repository,
                 branch_name: str,
                 files_to_summarize: Dict[str, int],
                 gemini_api_key: str,
                 model_name: str,
                 app_lang: str = 'en',
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.github_manager = github_manager
        self.repo = repo
        self.branch_name = branch_name
        self.files_to_summarize = files_to_summarize
        self.gemini_api_key = gemini_api_key
        self.model_name = model_name
        self._is_cancelled = False
        self.generative_model: Optional[genai.GenerativeModel] = None
        
        # --- НОВАЯ ЛОГИКА ИНИЦИАЛИЗАЦИИ СПЛИТТЕРОВ ---
        self.ts_splitter: Optional[TreeSitterSplitter] = None
        self.fallback_splitter = RecursiveCharacterSplitter(chunk_size=1000, chunk_overlap=150)
        
        # Выбираем шаблон промпта в зависимости от переданного app_lang
        if app_lang == 'ru':
            self.summarization_prompt_template = SUMMARIZATION_PROMPT_RU
        else:
            self.summarization_prompt_template = SUMMARIZATION_PROMPT_EN
            
        self._initialize_tree_sitter()

    def _initialize_tree_sitter(self):
        """Пытается инициализировать TreeSitterSplitter."""
        try:
            # Определяем путь к скомпилированной библиотеке
            if getattr(sys, 'frozen', False): # Для скомпилированного приложения
                base_path = os.path.dirname(sys.executable)
            else: # Для режима разработки
                base_path = os.path.dirname(os.path.abspath(__file__))
            
            lib_name = "languages.dll" if os.name == 'nt' else "languages.so"
            lib_path = os.path.join(base_path, 'resources', 'grammars', lib_name)
            
            if os.path.exists(lib_path):
                self.ts_splitter = TreeSitterSplitter(lib_path)
                logger.info("Tree-sitter сплиттер успешно инициализирован.")
            else:
                logger.warning(f"Скомпилированная библиотека грамматик не найдена по пути '{lib_path}'. Будет использоваться только рекурсивный сплиттер.")
        except Exception as e:
            logger.error(f"Ошибка при инициализации TreeSitterSplitter: {e}. Будет использоваться только рекурсивный сплиттер.")
            self.ts_splitter = None

    def cancel(self):
        """Запрашивает отмену операции."""
        logger.info(self.tr("Получен запрос на отмену анализа."))
        self._is_cancelled = True

    @Slot()
    def run(self):
        """Основной метод потока, выполняющий анализ."""
        logger.info(self.tr("Запуск потока анализа для {0} файлов.").format(len(self.files_to_summarize)))
        
        try:
            try:
                genai.configure(api_key=self.gemini_api_key)
                self.generative_model = genai.GenerativeModel(self.model_name)
            except Exception as e:
                error_msg = self.tr("Ошибка инициализации модели Gemini в SummarizerWorker: {0}").format(e)
                logger.error(error_msg)
                self.error_occurred.emit(error_msg)
                return

            processed_count = 0
            total_count = len(self.files_to_summarize)
            
            for file_path in self.files_to_summarize.keys():
                if self._is_cancelled:
                    logger.warning(self.tr("Операция анализа была отменена пользователем."))
                    break

                logger.debug(f"Анализ файла: {file_path}")
                
                content = self.github_manager.get_file_content(self.repo, file_path, self.branch_name)
                
                if content is None:
                    logger.warning(self.tr("Пропуск анализа для файла '{0}', так как не удалось получить его содержимое.").format(file_path))
                    processed_count += 1
                    self.progress_updated.emit(processed_count, total_count)
                    continue

                documents_to_add = []
                metadatas_to_add = []

                # 1. Создание саммари (логика осталась прежней)
                summary_text = self.tr("(Файл пуст)")
                if content.strip():
                    prompt = self.summarization_prompt_template.format(file_path=file_path, file_content=content)
                    try:
                        response = self.generative_model.generate_content(prompt, request_options={"timeout": 180})
                        summary_text = response.text.strip()
                        logger.info(self.tr("Успешно создано саммари для '{0}'.").format(file_path))
                    except google_exceptions.ResourceExhausted as e:
                        error_msg = self.tr("Исчерпаны квоты API Gemini при саммаризации '{0}'. Прерывание. Ошибка: {1}").format(file_path, e)
                        logger.error(error_msg)
                        self.error_occurred.emit(error_msg)
                        break 
                    except Exception as e:
                        summary_text = self.tr("(Ошибка саммаризации: {0})").format(type(e).__name__)
                        error_msg = self.tr("Ошибка API Gemini при саммаризации файла '{0}': {1} - {2}").format(file_path, type(e).__name__, e)
                        logger.error(error_msg)
                        self.error_occurred.emit(self.tr("Ошибка саммаризации для '{0}', файл пропущен в саммари.").format(file_path))
                
                self.file_summarized.emit(file_path, summary_text)
                documents_to_add.append(summary_text)
                metadatas_to_add.append({'file_path': file_path, 'type': 'summary'})
                
                # 2. Разбиение на чанки (НОВАЯ ЛОГИКА)
                if content.strip():
                    _, file_extension = os.path.splitext(file_path)
                    language = TreeSitterSplitter.LANGUAGE_MAP.get(file_extension.lower())
                    
                    chunks = []
                    # Используем TreeSitterSplitter, если он доступен и язык поддерживается
                    if self.ts_splitter and language and self.ts_splitter.is_language_supported(language):
                        logger.debug(f"Использование Tree-sitter сплиттера для языка '{language}'...")
                        chunks = self.ts_splitter.split_text(content, language)
                    # В противном случае используем рекурсивный сплиттер
                    else:
                        logger.debug(f"Использование рекурсивного сплиттера для файла '{file_path}'...")
                        chunks = self.fallback_splitter.split_text(content)

                    logger.debug(f"Файл '{file_path}' разбит на {len(chunks)} чанков.")
                    for i, chunk_text in enumerate(chunks):
                        documents_to_add.append(chunk_text)
                        metadatas_to_add.append({'file_path': file_path, 'type': 'chunk', 'chunk_num': i + 1})

                # 3. Отправка данных в БД
                if documents_to_add:
                    self.documents_for_db_ready.emit(documents_to_add, metadatas_to_add)

                processed_count += 1
                self.progress_updated.emit(processed_count, total_count)
        
        finally:
            logger.info(self.tr("Поток анализа завершил свою работу."))
            self.finished.emit()

# --- Добавляем sys в импорты, если его еще нет в файле ---
import sys