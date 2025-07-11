# --- Файл: db_manager.py ---

import sqlite3
import os
import datetime
import json
import logging
from typing import Optional, Dict, List, Tuple, Any
from io import BytesIO
import numpy as np

logger = logging.getLogger(__name__)

# НОВОЕ РАСШИРЕНИЕ ФАЙЛА СЕССИИ
SESSION_EXTENSION = ".cpai"

# --- ОБНОВЛЕННАЯ СХЕМА БАЗЫ ДАННЫХ ---
DATABASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    id INTEGER PRIMARY KEY DEFAULT 1,
    -- --- Информация о проекте ---
    project_type TEXT CHECK(project_type IN ('github', 'local')), -- 'github' или 'local'
    repo_url TEXT,         -- для project_type = 'github'
    repo_branch TEXT,      -- для project_type = 'github'
    local_path TEXT,       -- для project_type = 'local'
    -- --- Настройки анализа и чата ---
    rag_enabled BOOLEAN,   -- был ли включен RAG (разбиение на чанки)
    model_name TEXT,
    max_output_tokens INTEGER,
    extensions TEXT,
    instructions TEXT,
    -- --- Временные метки ---
    created_at TIMESTAMP,
    last_saved_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK(role IN ('user', 'model')),
    content TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    order_index INTEGER NOT NULL,
    excluded_from_api BOOLEAN NOT NULL DEFAULT 0
);

-- Новая таблица для хранения всего контекста (заменяет file_summaries)
CREATE TABLE IF NOT EXISTS context_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('summary', 'chunk', 'full_file', 'structure')), -- Тип контента
    chunk_num INTEGER, -- Порядковый номер чанка (для type='chunk')
    content TEXT NOT NULL,
    embedding BLOB, -- Векторное представление чанка (для type='chunk' и семантического поиска)
    UNIQUE(file_path, type, chunk_num)
);

CREATE INDEX IF NOT EXISTS idx_messages_order ON messages (order_index);
CREATE INDEX IF NOT EXISTS idx_context_filepath ON context_data (file_path);
"""


def dict_factory(cursor, row):
    """Фабрика для преобразования строк sqlite в словари."""
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}


def _get_connection(filepath: str) -> Optional[sqlite3.Connection]:
    """Устанавливает соединение с БД сессии и настраивает его."""
    try:
        conn = sqlite3.connect(filepath, timeout=10)
        conn.row_factory = dict_factory
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn
    except sqlite3.Error as e:
        logger.error(f"Не удалось установить соединение с базой данных '{filepath}': {e}")
        return None


def init_session_db(filepath: str) -> bool:
    """
    Создает или обновляет файл БД сессии и инициализирует таблицы.
    Возвращает True в случае успеха, False при ошибке.
    """
    if not filepath.endswith(SESSION_EXTENSION):
        logger.error(f"Ошибка: Файл должен иметь расширение {SESSION_EXTENSION}, а не '{filepath}'")
        return False
    try:
        logger.info(f"Инициализация/проверка БД сессии: {filepath}")
        dir_name = os.path.dirname(filepath)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        with _get_connection(filepath) as conn:
            if conn:
                conn.executescript(DATABASE_SCHEMA)
                # Логика миграции здесь больше не нужна, т.к. мы создаем новую структуру
            else:
                return False
        logger.debug(f"БД сессии успешно инициализирована/проверена.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при инициализации БД {filepath}: {e}")
        return False
    except OSError as e:
        logger.error(f"Ошибка файловой системы при создании/доступе к {filepath}: {e}")
        return False


def load_session_data(
    filepath: str,
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """
    Загружает метаданные, сообщения и контекст из файла сессии.
    Автоматически определяет старый формат и выполняет миграцию.
    """
    if not os.path.exists(filepath):
        logger.error(f"Файл сессии не найден: {filepath}")
        return None

    if not init_session_db(filepath):
        logger.error(f"Не удалось инициализировать/обновить файл сессии '{filepath}' перед загрузкой.")
        return None

    try:
        logger.info(f"Загрузка данных сессии из: {filepath}")
        with _get_connection(filepath) as conn:
            if not conn: return None

            # --- Проверяем, какой формат у сессии (новый или старый) ---
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='context_data';")
            is_new_format = cursor.fetchone() is not None

            # --- ЗАГРУЗКА ДЛЯ НОВОГО ФОРМАТА ---
            if is_new_format:
                logger.debug("Обнаружен новый формат сессии (таблица 'context_data').")
                
                # Загружаем метаданные
                metadata = conn.execute("SELECT * FROM metadata WHERE id = 1").fetchone() or {}
                
                # Загружаем сообщения
                messages_cursor = conn.execute("SELECT role, content, excluded_from_api FROM messages ORDER BY order_index ASC")
                messages_list = [
                    {"role": row["role"], "parts": [row["content"]], "excluded": bool(row.get("excluded_from_api", False))}
                    for row in messages_cursor.fetchall()
                ]
                
                # Загружаем контекст
                context_data_list = []
                table_info_cursor = conn.execute("PRAGMA table_info(context_data);")
                existing_columns = {col['name'] for col in table_info_cursor.fetchall()}
                columns_to_select = ['file_path', 'type', 'chunk_num', 'content']
                if 'embedding' in existing_columns:
                    columns_to_select.append('embedding')
                select_query = f"SELECT {', '.join(columns_to_select)} FROM context_data ORDER BY file_path, chunk_num ASC"
                context_cursor = conn.execute(select_query)
                for row in context_cursor.fetchall():
                    item = dict(row)
                    if item.get('type') == 'structure' and isinstance(item.get('content'), str):
                        try: item['content'] = json.loads(item['content'])
                        except json.JSONDecodeError: continue
                    if 'embedding' in item and item['embedding'] is not None:
                        try: item['embedding'] = np.load(BytesIO(item['embedding']), allow_pickle=True)
                        except Exception: item['embedding'] = None
                    else:
                        item['embedding'] = None
                    context_data_list.append(item)

                logger.info(f"Сессия нового формата успешно загружена.")
                return metadata, messages_list, context_data_list

            # --- МИГРАЦИЯ И ЗАГРУЗКА ДЛЯ СТАРОГО ФОРМАТА ---
            else:
                logger.warning("Обнаружен старый формат сессии. Выполняется миграция 'на лету'...")
                
                # Загружаем метаданные
                metadata = conn.execute("SELECT * FROM metadata WHERE id = 1").fetchone() or {}
                
                # Загружаем сообщения
                messages_cursor = conn.execute("SELECT role, content, excluded_from_api FROM messages ORDER BY order_index ASC")
                messages_list = [
                    {"role": row["role"], "parts": [row["content"]], "excluded": bool(row.get("excluded_from_api", False))}
                    for row in messages_cursor.fetchall()
                ]

                # Мигрируем file_summaries в context_data
                context_data_list = []
                summaries_cursor = conn.execute("SELECT file_path, summary FROM file_summaries ORDER BY file_path ASC")
                for row in summaries_cursor.fetchall():
                    context_data_list.append({
                        'file_path': row['file_path'], 'type': 'summary',
                        'chunk_num': 0, 'content': row['summary'], 'embedding': None
                    })
                
                metadata['migrated_from_old_format'] = True
                logger.info(f"Сессия старого формата успешно смигрирована и загружена.")
                return metadata, messages_list, context_data_list

    except sqlite3.Error as e:
        logger.error(f"Критическая ошибка SQLite при загрузке сессии {filepath}: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке сессии {filepath}: {e}", exc_info=True)
        return None

    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при загрузке сессии {filepath}: {e}")
        # Добавляем проверку на старый формат для более дружелюбного сообщения
        if "no such table: context_data" in str(e) or "no such table: file_summaries" in str(e):
             logger.error("Это может быть файл сессии старого формата, который не удалось смигрировать, или файл поврежден.")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке сессии {filepath}: {e}")
        return None


def save_session_data(
    filepath: str,
    metadata_dict: Dict[str, Any],
    messages_list: List[Dict[str, Any]],
    context_data_list: List[Dict[str, Any]]
) -> bool:
    """
    Сохраняет (перезаписывает) все данные сессии в файл.
    """
    if not init_session_db(filepath):
        return False

    try:
        logger.info(f"Сохранение данных сессии в: {filepath}")
        current_time = datetime.datetime.now()
        metadata_dict["last_saved_at"] = current_time
        if not metadata_dict.get("created_at"):
            metadata_dict["created_at"] = current_time

        with _get_connection(filepath) as conn:
            if not conn: return False
            cursor = conn.cursor()
            cursor.execute("BEGIN TRANSACTION;")

            try:
                # Сохранение метаданных
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO metadata (
                        id, project_type, repo_url, repo_branch, local_path, rag_enabled,
                        model_name, max_output_tokens, extensions, instructions,
                        created_at, last_saved_at
                    )
                    VALUES (
                        1, :project_type, :repo_url, :repo_branch, :local_path, :rag_enabled,
                        :model_name, :max_output_tokens, :extensions, :instructions,
                        :created_at, :last_saved_at
                    )
                    """,
                    metadata_dict,
                )

                # Сохранение сообщений (без изменений)
                cursor.execute("DELETE FROM messages;")
                messages_to_insert = [
                    (
                        msg.get("role"),
                        (msg.get("parts", [""])[0] if msg.get("parts") else ""),
                        current_time,
                        index,
                        1 if msg.get("excluded", False) else 0,
                    )
                    for index, msg in enumerate(messages_list)
                ]
                cursor.executemany(
                    "INSERT INTO messages (role, content, timestamp, order_index, excluded_from_api) VALUES (?, ?, ?, ?, ?)",
                    messages_to_insert,
                )

                # Сохранение контекста
                # Сохранение контекста
                cursor.execute("DELETE FROM context_data;")
                context_to_insert = []
                for item in context_data_list:
                    embedding_blob = None
                    # Сериализация эмбеддинга, если он есть
                    if 'embedding' in item and isinstance(item.get('embedding'), np.ndarray):
                        try:
                            buffer = BytesIO()
                            np.save(buffer, item['embedding'])
                            embedding_blob = buffer.getvalue()
                        except Exception as e:
                            logger.warning(f"Не удалось сериализовать эмбеддинг для '{item.get('file_path')}': {e}. Эмбеддинг не будет сохранен.")

                    # --- НОВАЯ ЛОГИКА: Сериализация 'content' в JSON для типа 'structure' ---
                    content_to_save = item.get("content")
                    if item.get('type') == 'structure' and isinstance(content_to_save, dict):
                        content_to_save = json.dumps(content_to_save, ensure_ascii=False)
                    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

                    chunk_num_val = item.get("chunk_num")
                    if chunk_num_val is None or not isinstance(chunk_num_val, int):
                        chunk_num_val = 0

                    context_to_insert.append((
                        item.get("file_path"),
                        item.get("type"),
                        chunk_num_val,
                        content_to_save,
                        embedding_blob
                    ))

                if context_to_insert:
                    cursor.executemany(
                        "INSERT INTO context_data (file_path, type, chunk_num, content, embedding) VALUES (?, ?, ?, ?, ?)",
                        context_to_insert
                    )

                conn.commit()
                logger.info(f"Сессия успешно сохранена. Сообщений: {len(messages_to_insert)}, Элементов контекста: {len(context_to_insert)}")
                return True

            except Exception as e:
                logger.error(f"Ошибка во время транзакции сохранения сессии, откат: {e}", exc_info=True)
                conn.rollback()
                return False

    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при сохранении сессии {filepath}: {e}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка при сохранении сессии {filepath}: {e}", exc_info=True)
        return False