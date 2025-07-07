# --- Файл: code_splitter.py ---

import os
import sys
import logging
from typing import List, Dict, Tuple, Optional

# Импортируем tree-sitter и его компоненты
from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

# --- Класс 1: Рекурсивный сплиттер для текста (наш fallback) ---
# Этот класс остается без изменений.

class RecursiveCharacterSplitter:
    """
    Простой рекурсивный сплиттер текста. Используется как fallback для
    файлов, не являющихся кодом, или для языков, не поддерживаемых TreeSitter.
    """
    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 150):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._separators = ["\n\n", "\n", " ", ""] # Универсальные разделители для текста
        self._length_function = len

    def split_text(self, text: str) -> List[str]:
        """Основной метод для вызова разбиения текста."""
        final_chunks = []
        # Начинаем с самого общего разделителя
        separator = self._separators[0]
        blocks = text.split(separator)
        
        # Рекурсивно обрабатываем блоки
        self._split_blocks(blocks, separator, final_chunks)
        return final_chunks

    def _split_blocks(self, blocks: List[str], separator: str, final_chunks: List[str]):
        """Рекурсивно разбивает блоки, пока они не станут меньше chunk_size."""
        current_chunk = ""
        for block in blocks:
            if not block:
                continue

            if self._length_function(block) > self.chunk_size:
                next_separator_index = self._separators.index(separator) + 1
                if next_separator_index < len(self._separators):
                    next_separator = self._separators[next_separator_index]
                    smaller_blocks = block.split(next_separator)
                    self._split_blocks(smaller_blocks, next_separator, final_chunks)
                else: 
                    if current_chunk:
                        final_chunks.append(current_chunk)
                    final_chunks.extend(self._force_split(block))
                    current_chunk = ""
            elif self._length_function(current_chunk + separator + block) <= self.chunk_size:
                current_chunk += separator + block
            else:
                if current_chunk:
                    final_chunks.append(current_chunk)
                current_chunk = block
        
        if current_chunk:
            final_chunks.append(current_chunk)

    def _force_split(self, text: str) -> List[str]:
        """Принудительно режет текст на части."""
        step = self.chunk_size - self.chunk_overlap
        return [text[i:i + self.chunk_size] for i in range(0, len(text), step)]


# --- Класс 2: "Умный" сплиттер кода на основе Tree-sitter ---

class TreeSitterSplitter:
    """
    Разделяет код на осмысленные чанки (функции, классы), используя
    синтаксические деревья, построенные с помощью Tree-sitter.
    """
    
    # --- НАЧАЛО ФИНАЛЬНЫХ ИЗМЕНЕНИЙ (Удаление проблемных языков) ---

    # Мастер-список ВСЕХ имен языков, которые мы успешно компилируем.
    # Если грамматика не компилируется или вызывает проблемы, она удаляется из этого списка.
    ALL_LANGUAGES: Tuple[str, ...] = (
        "python", "javascript", "html", "css", "json",
        "java", "c_sharp", "cpp", "go", "ruby", "rust", "bash", "yaml"
    )

    # Обновленная карта расширений файлов к КОНКРЕТНЫМ именам языков из списка выше.
    # Файлы с расширениями удаленных языков теперь будут обрабатываться fallback-сплиттером.
    LANGUAGE_MAP: Dict[str, str] = {
        ".py": "python",
        ".js": "javascript",
        ".html": "html",
        ".css": "css",
        ".json": "json",
        ".java": "java",
        ".cs": "c_sharp",
        ".cpp": "cpp",
        ".h": "cpp",
        ".hpp": "cpp",
        ".go": "go",
        ".rb": "ruby",
        ".rs": "rust",
        ".sh": "bash",
        ".yml": "yaml",
        ".yaml": "yaml",
        # ".ts": "typescript", # Удалены
        # ".tsx": "tsx",       # Удалены
        # ".php": "php",       # Удалены
        # ".sql": "sql",       # Удалены
    }
    
    # Карта языков к типам узлов, по которым нужно делать основное разделение.
    # Ключи здесь - это КОНКРЕТНЫЕ имена языков.
    # Удалены записи для языков, которые не поддерживаются.
    SPLIT_NODES_MAP: Dict[str, Tuple[str, ...]] = {
        "python": ("function_definition", "class_definition"),
        "javascript": ("function_declaration", "class_declaration", "method_definition"),
        # "typescript": ("function_declaration", "class_declaration", "method_definition", "interface_declaration"), # Удалено
        # "tsx": ("function_declaration", "class_declaration", "method_definition", "interface_declaration"), # Удалено
        "java": ("method_declaration", "class_declaration", "interface_declaration"),
        "c_sharp": ("method_declaration", "class_declaration", "struct_declaration", "interface_declaration"),
        "cpp": ("function_definition", "class_specifier", "struct_specifier"),
        "go": ("function_declaration", "method_declaration", "type_spec"),
        "rust": ("function_item", "struct_item", "enum_item", "impl_item"),
        # "php": ("function_definition", "class_declaration", "trait_declaration"), # Удалено
        "html": ("element",),
    }

    # --- КОНЕЦ ФИНАЛЬНЫХ ИЗМЕНЕНИЙ ---

    def __init__(self, compiled_library_path: str):
        if not os.path.exists(compiled_library_path):
            raise FileNotFoundError(f"Скомпилированная библиотека tree-sitter не найдена по пути: {compiled_library_path}")
            
        self.library_path = compiled_library_path
        self.parser = Parser()
        self.languages: Dict[str, Language] = {}
        self._load_languages()

        # Экземпляр fallback-сплиттера, который всегда доступен
        self.fallback_splitter = RecursiveCharacterSplitter(chunk_size=1000, chunk_overlap=150)


    def _load_languages(self):
        """Загружает все доступные языки из мастер-списка."""
        for lang_name in self.ALL_LANGUAGES:
            try:
                lang_obj = Language(self.library_path, name=lang_name) # <-- Изменено: добавлен 'name='
                self.languages[lang_name] = lang_obj
                logger.debug(f"Успешно загружена грамматика для '{lang_name}'")
            except Exception as e:
                logger.warning(f"Не удалось загрузить грамматику для '{lang_name}': {e}. Файлы этого типа будут обрабатываться базовым текстовым сплиттером.")

    
    def is_language_supported(self, lang_name: str) -> bool:
        """Проверяет, поддерживается ли (успешно ли загружен) данный язык."""
        return lang_name in self.languages

    def split_text(self, code: str, language: str) -> List[str]:
        """
        Основной метод для разделения кода.
        """
        # Если язык не поддерживается или не удалось загрузить грамматику, используем fallback
        if not self.is_language_supported(language):
            logger.debug(f"Язык '{language}' не поддерживается Tree-sitter, используется fallback (рекурсивный сплиттер).")
            return self.fallback_splitter.split_text(code)

        lang_obj = self.languages[language]
        self.parser.set_language(lang_obj)
        
        try:
            tree = self.parser.parse(bytes(code, "utf8"))
            root_node = tree.root_node
        except Exception as e:
            logger.error(f"Ошибка парсинга кода на языке '{language}': {e}. Возвращается единый чанк.")
            return [code] # Возвращаем как есть в случае ошибки парсинга

        target_node_types = self.SPLIT_NODES_MAP.get(language)
        if not target_node_types:
            logger.debug(f"Для языка '{language}' не определены узлы-разделители в SPLIT_NODES_MAP, используется fallback.")
            return self.fallback_splitter.split_text(code)

        split_nodes = self._find_split_nodes(root_node, target_node_types)
        
        # Если Tree-sitter не нашел структурных узлов, возвращаем весь код или используем fallback
        if not split_nodes:
            logger.debug(f"Tree-sitter не нашел структурных узлов для '{language}', используется fallback.")
            return self.fallback_splitter.split_text(code)
            
        chunks = []
        last_end = 0
        
        split_nodes.sort(key=lambda node: node.start_byte)

        for node in split_nodes:
            if node.start_byte > last_end:
                intermediate_code = code[last_end:node.start_byte].strip()
                if intermediate_code:
                    chunks.append(intermediate_code)
            
            node_code = node.text.decode('utf-8', errors='ignore').strip()
            if node_code:
                chunks.append(node_code)
            
            last_end = node.end_byte
            
        if len(code) > last_end:
            remaining_code = code[last_end:].strip()
            if remaining_code:
                chunks.append(remaining_code)

        # Финальная проверка: если чанк все еще слишком большой, режем его рекурсивно
        final_chunks = []
        for chunk in chunks:
            # Проверяем не по self.chunk_size, а по свойству fallback_splitter.chunk_size
            if len(chunk) > self.fallback_splitter.chunk_size * 1.2: # Даем небольшой запас
                logger.debug(f"Чанк (тип: {language}) все еще слишком большой ({len(chunk)} символов), применяется дополнительное разбиение.")
                final_chunks.extend(self.fallback_splitter.split_text(chunk))
            else:
                final_chunks.append(chunk)

        return final_chunks

    def _find_split_nodes(self, node, target_types: Tuple[str, ...], depth=0) -> List:
        found_nodes = []
        if depth > 4: 
            return []

        for child in node.children:
            # Важно: рекурсивно ищем только если текущий узел НЕ является целевым типом.
            # Иначе мы бы дублировали, например, вложенные функции.
            if child.type in target_types:
                found_nodes.append(child)
            else:
                found_nodes.extend(self._find_split_nodes(child, target_types, depth + 1))
        
        return found_nodes