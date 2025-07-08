# --- Файл: github_manager.py ---

import logging
import base64
import re
from typing import Optional, Dict, Tuple, List, Set

from PySide6.QtCore import QObject

from github import Github, UnknownObjectException, BadCredentialsException, RateLimitExceededException
from github.Repository import Repository
from github.ContentFile import ContentFile
from github.GitTreeElement import GitTreeElement

# Настраиваем логгер для этого модуля
logger = logging.getLogger(__name__)

# Папки, которые стандартно игнорируются при анализе репозитория
DEFAULT_IGNORED_DIRS = {
    "venv", ".venv", "__pycache__", ".git", ".vscode", ".idea", 
    "node_modules", "build", "dist", "target", "out", "bin", "obj",
    "docs", "examples", "tests", "test", "samples"
}

class GitHubManager(QObject):
    """
    Класс для инкапсуляции логики взаимодействия с GitHub API.
    Отвечает за аутентификацию, получение информации о репозитории и чтение файлов.
    """

    def __init__(self, token: Optional[str]):
        """
        Инициализирует менеджер с помощью Personal Access Token (PAT).

        Args:
            token: Персональный токен доступа GitHub.
        """
        super().__init__()
        self.token = token
        self.gh: Optional[Github] = None
        self.rate_limit_info: str = self.tr("Неизвестно")
        
        if self.token:
            try:
                self.gh = Github(self.token)
                rate_limit = self.gh.get_rate_limit()
                self.rate_limit_info = self.tr("Осталось {0}/{1}").format(rate_limit.core.remaining, rate_limit.core.limit)
                logger.info(self.tr("GitHubManager инициализирован успешно. Лимит запросов: {0}").format(self.rate_limit_info))
            except BadCredentialsException:
                logger.error(self.tr("Ошибка аутентификации GitHub: Неверный токен (BadCredentialsException)."))
                self.gh = None
            except Exception as e:
                logger.error(self.tr("Неожиданная ошибка при инициализации GitHub-клиента: {0}").format(e))
                self.gh = None
        else:
            logger.warning(self.tr("GitHubManager инициализирован без токена. Доступ будет только к публичным репозиториям."))
            self.gh = Github()

    def is_authenticated(self) -> bool:
        """Проверяет, аутентифицирован ли клиент."""
        return self.gh is not None and self.token is not None

    def _parse_repo_url(self, repo_url: str) -> Optional[Tuple[str, str, Optional[str]]]:
        """
        Извлекает 'владелец/имя_репозитория' и опционально 'ветку' из URL.
        """
        pattern = r"(?:https?://)?(?:www\.)?github\.com/([\w\.\-]+)/([\w\.\-]+)(?:/tree/([\w\.\-]+))?"
        match = re.search(pattern, repo_url)
        if match:
            owner, repo_name, branch = match.groups()
            logger.debug(self.tr("URL '{0}' распарсен как '{1}/{2}' (Ветка: {3})").format(repo_url, owner, repo_name, branch))
            return owner, repo_name, branch
        logger.warning(self.tr("Не удалось распарсить URL репозитория: '{0}'").format(repo_url))
        return None

    def get_repo(self, repo_url: str) -> Optional[Tuple[Repository, Optional[str]]]:
        """
        Получает объект репозитория и имя ветки из URL.
        """
        if not self.gh:
            logger.error(self.tr("Невозможно получить репозиторий: клиент GitHub не инициализирован."))
            return None

        parsed_data = self._parse_repo_url(repo_url)
        if not parsed_data:
            return None
        
        owner, repo_name, branch_from_url = parsed_data
        repo_identifier = f"{owner}/{repo_name}"

        try:
            repo = self.gh.get_repo(repo_identifier)
            logger.info(self.tr("Успешно получен доступ к репозиторию: {0}").format(repo.full_name))
            return repo, branch_from_url
        except UnknownObjectException:
            logger.error(self.tr("Репозиторий '{0}' не найден или является приватным без доступа.").format(repo_identifier))
            return None
        except BadCredentialsException:
            logger.error(self.tr("Ошибка аутентификации при доступе к репозиторию. Проверьте ваш токен."))
            return None
        except RateLimitExceededException:
            logger.error(self.tr("Превышен лимит запросов к GitHub API. Попробуйте позже."))
            return None
        except Exception as e:
            logger.error(self.tr("Неожиданная ошибка при получении репозитория: {0}").format(e))
            return None
        
    def get_available_branches(self, repo: Repository) -> List[str]:
        """Возвращает отсортированный список имен всех веток в репозитории."""
        if not repo:
            return []
        try:
            branches = [branch.name for branch in repo.get_branches()]
            default_branch = repo.default_branch
            if default_branch in branches:
                branches.remove(default_branch)
                branches.insert(0, default_branch)
            logger.info(self.tr("Найдено {0} веток для репозитория '{1}'.").format(len(branches), repo.full_name))
            return branches
        except Exception as e:
            logger.error(self.tr("Не удалось получить список веток для '{0}': {1}").format(repo.full_name, e))
            return []

    def get_repo_file_tree(
        self, 
        repo: Repository,
        branch_name: str,
        extensions: Tuple[str, ...],
        ignored_dirs: Set[str] = DEFAULT_IGNORED_DIRS,
        max_file_size_kb: int = 512
    ) -> Tuple[Dict[str, int], List[str]]:
        """
        Получает плоский список путей к файлам в указанной ветке репозитория.
        """
        logger.info(self.tr("Начинается анализ дерева файлов для '{0}' в ветке '{1}'...").format(repo.full_name, branch_name))
        try:
            branch = repo.get_branch(branch_name)
            tree = repo.get_git_tree(branch.commit.sha, recursive=True)
            logger.info(self.tr("Получено дерево файлов, {0} элементов.").format(len(tree.tree)))
        except UnknownObjectException:
            msg = self.tr("Ветка '{0}' не найдена в репозитории '{1}'.").format(branch_name, repo.full_name)
            logger.error(msg)
            return {}, [self.tr("Ошибка: Ветка '{0}' не найдена.").format(branch_name)]
        except Exception as e:
            msg = self.tr("Не удалось получить дерево файлов для ветки '{0}': {1}").format(branch_name, e)
            logger.error(msg)
            return {}, [self.tr("Ошибка: Не удалось получить дерево файлов: {0}").format(e)]

        filtered_files: Dict[str, int] = {}
        skipped_info: List[str] = []
        max_size_bytes = max_file_size_kb * 1024

        for element in tree.tree:
            if element.type == "blob":
                if any(part in ignored_dirs for part in element.path.split('/')):
                    continue
                if not element.path.lower().endswith(extensions):
                    continue
                if element.size > max_size_bytes:
                    skipped_info.append(self.tr("Пропущен (размер > {0}KB): {1}").format(max_file_size_kb, element.path))
                    continue
                filtered_files[element.path] = element.size

        logger.info(self.tr("Анализ дерева завершен. Найдено подходящих файлов: {0}. Пропущено: {1}.").format(len(filtered_files), len(skipped_info)))
        return filtered_files, skipped_info
    
    def get_repo_file_tree_text(self, repo: Repository, branch_name: str, ignored_dirs: Set[str] = DEFAULT_IGNORED_DIRS) -> str:
        """
        Получает строковое представление дерева файлов репозитория, похожее на вывод 'tree'.
        """
        if not repo:
            return self.tr("Ошибка: Репозиторий не предоставлен.")
            
        logger.info(self.tr("Получение текстового представления дерева файлов для '{0}' в ветке '{1}'...").format(repo.full_name, branch_name))
        try:
            branch = repo.get_branch(branch_name)
            tree_elements = repo.get_git_tree(branch.commit.sha, recursive=True).tree
            
            paths = [element.path for element in tree_elements if not any(part in ignored_dirs for part in element.path.split('/'))]
            paths.sort()

            tree_dict = {}
            for path in paths:
                parts = path.split('/')
                current_level = tree_dict
                for part in parts:
                    if part not in current_level:
                        current_level[part] = {}
                    current_level = current_level[part]

            def build_tree_string(d, indent=''):
                s = ''
                items = sorted(d.keys())
                for i, key in enumerate(items):
                    connector = '└── ' if i == len(items) - 1 else '├── '
                    s += indent + connector + key + '\n'
                    if d[key]:
                        extension = '    ' if i == len(items) - 1 else '│   '
                        s += build_tree_string(d[key], indent + extension)
                return s

            project_name = repo.name
            return f"{project_name}/\n" + build_tree_string(tree_dict)

        except Exception as e:
            msg = self.tr("Не удалось сгенерировать дерево файлов: {0}").format(e)
            logger.error(msg)
            return msg

    def get_file_content(self, repo: Repository, file_path: str, branch_name: str) -> Optional[bytes]:
        """
        Получает содержимое одного файла из указанной ветки репозитория в виде байтов.
        """
        if not file_path:
            logger.warning(self.tr("get_file_content вызван с пустым путем. Пропуск."))
            return None

        logger.debug(self.tr("Запрос содержимого файла: {0} из ветки {1}").format(file_path, branch_name))
        try:
            content_file = repo.get_contents(file_path, ref=branch_name)

            if isinstance(content_file, list):
                logger.warning(self.tr("Путь '{0}' указывает на директорию, а не на файл. Пропуск.").format(file_path))
                return None

            if content_file.encoding == "base64" and content_file.content:
                # Возвращаем сырые байты, не декодируя в UTF-8
                decoded_bytes = base64.b64decode(content_file.content)
                return decoded_bytes
            else:
                logger.warning(self.tr("Файл '{0}' пуст или имеет неизвестную кодировку: {1}").format(file_path, content_file.encoding))
                return b""  # Возвращаем пустые байты
        except UnknownObjectException:
            logger.error(self.tr("Файл '{0}' не найден в ветке '{1}'.").format(file_path, branch_name))
            return None
        except Exception as e:
            logger.error(self.tr("Не удалось получить содержимое файла '{0}': {1}").format(file_path, e))
            return None