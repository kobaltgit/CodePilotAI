# --- Файл: ast_parser.py ---

import os
import logging
from typing import Dict, Any, Optional, List

from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

# Карта расширений к языкам. Должна быть синхронизирована с code_splitter.py
LANGUAGE_MAP: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".java": "java",
    ".cs": "c_sharp",
    ".cpp": "cpp", ".h": "cpp", ".hpp": "cpp",
    # Добавьте другие языки по мере необходимости
}

# Запросы Tree-sitter для извлечения структуры кода для каждого языка.
# Это "сердце" нашего парсера.
LANGUAGE_QUERIES: Dict[str, str] = {
    "python": """
        (import_statement (dotted_name) @import)
        (import_from_statement module_name: (dotted_name) @import_from)
        (function_definition
            name: (identifier) @function.name
            parameters: (parameters) @function.parameters)
        (class_definition
            name: (identifier) @class.name
            superclasses: (argument_list)? @class.superclasses)
    """,
    "javascript": """
        (import_statement (import_clause) @import)
        (import_statement source: (string) @import_from)
        (function_declaration
            name: (identifier) @function.name
            parameters: (formal_parameters) @function.parameters)
        (class_declaration
            name: (identifier) @class.name
            (class_heritage (expression (identifier) @class.superclasses))?)
    """,
    # TODO: Добавить запросы для других поддерживаемых языков (Java, C#, C++ и т.д.)
}


class ASTParser:
    """
    Парсер, который использует Tree-sitter для извлечения
    структурной информации из кода: импорты, функции и классы.
    """

    def __init__(self, compiled_library_path: str):
        if not os.path.exists(compiled_library_path):
            raise FileNotFoundError(f"Скомпилированная библиотека tree-sitter не найдена: {compiled_library_path}")

        self.library_path = compiled_library_path
        self.parser = Parser()
        self.languages: Dict[str, Language] = {}
        self._load_languages()

    def _load_languages(self):
        """Загружает поддерживаемые языки из LANGUAGE_MAP."""
        for lang_name in set(LANGUAGE_MAP.values()):
            try:
                lang_obj = Language(self.library_path, name=lang_name)
                self.languages[lang_name] = lang_obj
                logger.debug(f"ASTParser: Успешно загружена грамматика для '{lang_name}'")
            except Exception as e:
                logger.warning(f"ASTParser: Не удалось загрузить грамматику для '{lang_name}': {e}")

    def get_language_from_extension(self, file_extension: str) -> Optional[str]:
        """Возвращает имя языка по расширению файла."""
        return LANGUAGE_MAP.get(file_extension.lower())

    def parse_code_structure(self, code_content: str, language: str) -> Dict[str, Any]:
        """
        Анализирует код и возвращает словарь со структурной информацией.
        """
        structure = {
            "imports": set(),
            "functions": [],
            "classes": {}
        }
        if language not in self.languages or language not in LANGUAGE_QUERIES:
            logger.debug(f"ASTParser: Пропуск анализа структуры для неподдерживаемого языка '{language}'")
            return structure

        lang_obj = self.languages[language]
        self.parser.set_language(lang_obj)
        
        try:
            tree = self.parser.parse(bytes(code_content, "utf8"))
            query_str = LANGUAGE_QUERIES[language]
            query = lang_obj.query(query_str)
            captures = query.captures(tree.root_node)
        except Exception as e:
            logger.error(f"ASTParser: Ошибка при парсинге или выполнении запроса для языка '{language}': {e}")
            return structure

        # Временное хранилище, чтобы связать имя класса/функции с их параметрами/суперклассами
        temp_items = {}

        for node, capture_name in captures:
            node_text = node.text.decode('utf-8').strip()

            if capture_name == "import":
                structure["imports"].add(node_text)
            elif capture_name == "import_from":
                # Мы можем улучшить это, чтобы обрабатывать `from x import y, z`
                # но пока просто добавим имя модуля.
                structure["imports"].add(node_text)
            elif capture_name.endswith(".name"):
                item_type, _ = capture_name.split('.')
                temp_items[node.start_byte] = {"type": item_type, "name": node_text}
            elif capture_name.endswith(".parameters"):
                # Находим родительский элемент (функцию) по начальной позиции
                parent_start_byte = node.parent.start_byte
                if parent_start_byte in temp_items:
                    temp_items[parent_start_byte]["signature"] = node_text
            elif capture_name.endswith(".superclasses"):
                # Находим родительский элемент (класс) по начальной позиции
                parent_start_byte = node.parent.start_byte
                if parent_start_byte in temp_items:
                    # Убираем скобки для чистоты
                    superclasses_text = node_text.strip("()")
                    temp_items[parent_start_byte]["superclasses"] = superclasses_text

        # Собираем финальную структуру
        for item in temp_items.values():
            if item["type"] == "function":
                signature = item.get("signature", "()")
                structure["functions"].append(f"{item['name']}{signature}")
            elif item["type"] == "class":
                superclasses = item.get("superclasses", "")
                inheritance = f"({superclasses})" if superclasses else ""
                structure["classes"][item['name']] = inheritance

        # Преобразуем set в отсортированный список для консистентности
        structure["imports"] = sorted(list(structure["imports"]))
        return structure