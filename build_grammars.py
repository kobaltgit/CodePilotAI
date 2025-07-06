# --- Файл: build_grammars.py ---

import os
import platform
import logging
from tree_sitter import Language

# Настройка простого логгера для скрипта
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# --- Конфигурация ---
# Папка с исходниками грамматик
GRAMMARS_SRC_DIR = 'grammars'

# Папка, куда будет помещена скомпилированная библиотека.
OUTPUT_DIR = os.path.join('resources', 'grammars')

def get_library_filename() -> str:
    """Возвращает корректное имя файла библиотеки для текущей ОС."""
    system = platform.system()
    if system == 'Windows':
        return 'languages.dll'
    elif system == 'Darwin':  # macOS
        return 'languages.dylib'
    else:  # Linux и другие
        return 'languages.so'

def find_grammar_dirs(base_dir: str) -> list[str]:
    """Находит все валидные директории с грамматиками в указанной папке."""
    grammar_paths = []
    if not os.path.isdir(base_dir):
        logger.error(f"Ошибка: Директория '{base_dir}' не найдена.")
        return []

    for entry in os.listdir(base_dir):
        path = os.path.join(base_dir, entry)
        # Валидная директория с грамматикой должна содержать подпапку 'src'
        if os.path.isdir(path) and 'src' in os.listdir(path):
            grammar_paths.append(entry) # Теперь добавляем просто имя папки
            logger.info(f"  - Найдена грамматика: {entry}")
    
    return grammar_paths

def main():
    """Основная функция для сборки библиотеки грамматик."""
    logger.info("--- Запуск компиляции грамматик Tree-sitter ---")

    # Сохраняем оригинальный рабочий каталог, чтобы вернуться в него позже
    original_cwd = os.getcwd()
    
    # --- Вычисляем АБСОЛЮТНЫЕ пути до того, как сменим директорию ---
    src_dir_abs = os.path.abspath(GRAMMARS_SRC_DIR)
    output_dir_abs = os.path.abspath(OUTPUT_DIR)
    os.makedirs(output_dir_abs, exist_ok=True)

    library_filename = get_library_filename()
    output_library_path_abs = os.path.join(output_dir_abs, library_filename)

    logger.info(f"Выходная библиотека будет создана по пути: '{output_library_path_abs}'")
    
    try:
        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Переходим в директорию с грамматиками ---
        logger.info(f"\nПереход в рабочий каталог: '{src_dir_abs}'")
        os.chdir(src_dir_abs)

        # Сканируем грамматики уже находясь внутри папки 'grammars'
        logger.info("\nСканирование исходников грамматик...")
        # Теперь пути будут простыми именами папок: 'tree-sitter-python', и т.д.
        grammar_dirs = find_grammar_dirs('.') 
        
        if not grammar_dirs:
            logger.error("\nНе найдены валидные директории с грамматиками. Компиляция невозможна.")
            logger.error(f"Пожалуйста, убедитесь, что вы склонировали репозитории грамматик в папку '{src_dir_abs}'.")
            return

        # Компилируем библиотеку, используя простые имена папок
        logger.info("\nНачало компиляции... (Это может занять несколько минут)")
        Language.build_library(
            output_library_path_abs, # Используем абсолютный путь для вывода
            grammar_dirs # Используем простые имена для входа
        )
        logger.info("\n--- Компиляция успешно завершена! ---")
        logger.info(f"Библиотека '{library_filename}' создана в папке '{output_dir_abs}'.")
        logger.info("Теперь можно запустить основное приложение.")

    except Exception as e:
        logger.error("\n--- ОШИБКА КОМПИЛЯЦИИ! ---")
        logger.error(f"Произошла ошибка: {e}")
        logger.error("\nПожалуйста, убедитесь, что у вас установлен и настроен компилятор C/C++.")
        logger.error("Для Windows: установите 'Build Tools for Visual Studio'.")
        logger.error("Для Linux (Debian/Ubuntu): выполните 'sudo apt-get install build-essential'.")
        logger.error("Для macOS: установите Xcode Command Line Tools ('xcode-select --install').")
    
    finally:
        # --- ОБЯЗАТЕЛЬНО: Возвращаемся в исходный каталог ---
        os.chdir(original_cwd)
        logger.info(f"\nВозврат в исходный каталог: '{original_cwd}'")


if __name__ == '__main__':
    main()