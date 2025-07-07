# --- Файл: summarizer.py ---

import os
import sys
import logging
from typing import Dict, Optional, List, Any

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


class SummarizerWorker(QThread): # <-- Изменено: наследование от QThread
    """
    Рабочий поток, который выполняет анализ контента файлов.
    ...
    """
    # Сигналы
    progress_updated = Signal(int, int, str)
    # Сигнал для обновления UI в реальном времени (окно саммари)
    file_summarized = Signal(str, str)
    # Сигнал с готовым пакетом данных для записи в БД
    context_data_ready = Signal(list)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self,
                 files_content: Dict[str, str],
                 rag_enabled: bool,
                 gemini_api_key: str,
                 model_name: str,
                 app_lang: str = 'en',
                 parent: Optional[QObject] = None): # <-- parent теперь Optional[QObject], так как QThread не принимает QObject в __init__
        super().__init__() # <-- Изменено: parent удален, т.к. QThread сам родитель
        self.files_content = files_content
        self.rag_enabled = rag_enabled
        self.gemini_api_key = gemini_api_key
        self.model_name = model_name
        self._is_cancelled = False
        self.generative_model: Optional[genai.GenerativeModel] = None

        self.ts_splitter: Optional[TreeSitterSplitter] = None
        self.fallback_splitter = RecursiveCharacterSplitter(chunk_size=1000, chunk_overlap=150)

        # Выбираем шаблон промпта в зависимости от языка
        self.summarization_prompt_template = SUMMARIZATION_PROMPT_RU if app_lang == 'ru' else SUMMARIZATION_PROMPT_EN

        self._initialize_tree_sitter()

    # run() метод теперь не @Slot(), так как это метод QThread.
    # Внутри run() инициализация model будет также как в GeminiWorker.


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
        """Основной метод потока, выполняющий анализ."""
        logger.info(self.tr("Запуск потока анализа для {0} файлов. Режим RAG: {1}").format(len(self.files_content), self.rag_enabled))

        try:
            # Инициализация модели Gemini нужна только для RAG режима
            if self.rag_enabled:
                try:
                    genai.configure(api_key=self.gemini_api_key)
                    self.generative_model = genai.GenerativeModel(self.model_name)
                except Exception as e:
                    error_msg = self.tr("Ошибка инициализации модели Gemini: {0}").format(e)
                    logger.error(error_msg)
                    self.error_occurred.emit(error_msg)
                    return

            processed_count = 0
            total_count = len(self.files_content)

            for file_path, content in self.files_content.items():
                if self._is_cancelled:
                    logger.warning(self.tr("Операция анализа была отменена пользователем."))
                    break

                logger.debug(f"Анализ файла: {file_path}")
                self.progress_updated.emit(processed_count, total_count, file_path)
                
                context_for_this_file = []

                if self.rag_enabled:
                    # --- Логика для режима RAG (чанки и саммари) ---
                    # 1. Создание саммари
                    summary_text = self.tr("(Файл пуст)")
                    if content.strip() and self.generative_model:
                        prompt = self.summarization_prompt_template.format(file_path=file_path, file_content=content)
                        try:
                            response = self.generative_model.generate_content(prompt, request_options={"timeout": 180})
                            summary_text = response.text.strip()
                            logger.info(self.tr("Успешно создано саммари для '{0}'.").format(file_path))
                        except google_exceptions.ResourceExhausted as e:
                            error_msg = self.tr("Исчерпаны квоты API Gemini. Прерывание. Ошибка: {0}").format(e)
                            self.error_occurred.emit(error_msg)
                            break
                        except Exception as e:
                            summary_text = self.tr("(Ошибка саммаризации: {0})").format(type(e).__name__)
                            self.error_occurred.emit(self.tr("Ошибка саммаризации для '{0}', файл пропущен в саммари.").format(file_path))

                    self.file_summarized.emit(file_path, summary_text)
                    context_for_this_file.append({'file_path': file_path, 'type': 'summary', 'chunk_num': 0, 'content': summary_text})

                    # 2. Разбиение на чанки
                    if content.strip():
                        _, file_extension = os.path.splitext(file_path)
                        language = TreeSitterSplitter.LANGUAGE_MAP.get(file_extension.lower())

                        chunks = []
                        if self.ts_splitter and language and self.ts_splitter.is_language_supported(language):
                            chunks = self.ts_splitter.split_text(content, language)
                        else:
                            chunks = self.fallback_splitter.split_text(content)

                        for i, chunk_text in enumerate(chunks):
                            context_for_this_file.append({'file_path': file_path, 'type': 'chunk', 'chunk_num': i + 1, 'content': chunk_text})

                else:
                    # --- Логика для режима полных файлов (RAG выключен) ---
                    context_for_this_file.append({'file_path': file_path, 'type': 'full_file', 'chunk_num': 0, 'content': content})
                    # Для совместимости с окном саммари, отправим "саммари-заглушку"
                    self.file_summarized.emit(file_path, self.tr("(Режим RAG отключен, файл используется целиком)"))

                # Отправляем готовый пакет данных для этого файла
                if context_for_this_file:
                    self.context_data_ready.emit(context_for_this_file)

                processed_count += 1
                
            self.progress_updated.emit(total_count, total_count, self.tr("Завершено"))

        finally:
            logger.info(self.tr("Поток анализа завершил свою работу."))
            self.finished.emit()