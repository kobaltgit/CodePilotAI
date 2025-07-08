# --- Файл: log_viewer_window.py ---

import sys
import os
import logging
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QLineEdit, QHBoxLayout, QLabel, QPushButton, QApplication
)
from PySide6.QtCore import (
    QObject, Signal, QThread, QFile, QTextStream, Slot, QIODevice, QFileSystemWatcher, Qt, QStringConverter, QTimer # Добавляем QTimer
)
from PySide6.QtGui import (
    QTextCharFormat, QTextCursor, QColor, QFont, QKeySequence, QAction, QTextDocument
)

logger = logging.getLogger(__name__)

class LogFileReaderWorker(QObject):
    """
    Рабочий поток для чтения лог-파일 in real time.
    Использует QFileSystemWatcher для отслеживания изменений и ротации файла.
    Применяет паттерн "открыть-прочитать-закрыть" для предотвращения конфликтов доступа.
    """
    new_log_line = Signal(str)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self, log_file_path: str):
        super().__init__()
        self._log_file_path = log_file_path
        # self._file и self._stream не будут держаться открытыми постоянно
        self._file_size_at_last_read = 0 # Для отслеживания позиции чтения
        self._watcher = QFileSystemWatcher(self) # Пэрентим watcher к воркеру

        # Подключение сигналов
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_directory_changed)

        self._is_running = True

    def run(self):
        """Основной метод потока."""
        logger.debug(f"LogFileReaderWorker: Метод run() запущен для {self._log_file_path}")
        self._setup_watcher()

        if self._is_running:
            self._read_all_current_data()
            logger.debug("LogFileReaderWorker: Первичное чтение файла логов завершено.")

        # Запускаем цикл обработки событий QThread, необходимый для работы QFileSystemWatcher.
        logger.debug("LogFileReaderWorker: Вход в цикл обработки событий потока (exec()).")
        self.thread().exec()
        logger.debug("LogFileReaderWorker: Выход из цикла обработки событий потока.")

        self.finished.emit()

    @Slot()
    def stop(self):
        """
        Слот для полной остановки воркера. Выполняется в его собственном потоке.
        Производит очистку ресурсов и затем завершает цикл событий потока.
        """
        logger.debug("LogFileReaderWorker: Слот stop() вызван в потоке воркера.")
        self._is_running = False
        self._cleanup_watcher()
        if self.thread():
            logger.debug("LogFileReaderWorker: Завершение цикла событий потока (quit()).")
            self.thread().quit()

    def _cleanup_watcher(self):
        """Вспомогательный метод для очистки QFileSystemWatcher."""
        logger.debug("LogFileReaderWorker: Выполняется очистка QFileSystemWatcher.")
        try:
            if self._watcher.files():
                self._watcher.removePaths(self._watcher.files())
            if self._watcher.directories():
                self._watcher.removePaths(self._watcher.directories())
            logger.debug("LogFileReaderWorker: Очистка QFileSystemWatcher завершена.")
        except Exception as e:
            logger.error(f"Ошибка при очистке QFileSystemWatcher: {e}")


    def _setup_watcher(self):
        """Настраивает QFileSystemWatcher для отслеживания файла и его директории."""
        if not self._is_running: return

        # Удаляем любые существующие пути, чтобы избежать дубликатов или отслеживания старых файлов
        if self._watcher.files(): self._watcher.removePaths(self._watcher.files())
        if self._watcher.directories(): self._watcher.removePaths(self._watcher.directories())

        dir_path = os.path.dirname(self._log_file_path)

        # Всегда отслеживаем директорию для обнаружения ротации/создания файла
        if dir_path and os.path.exists(dir_path):
            if dir_path not in self._watcher.directories():
                self._watcher.addPath(dir_path)
                logger.debug(f"Added directory {dir_path} to watcher.")
        else:
            self.error_occurred.emit(self.tr("Папка логов не существует: {0}").format(dir_path))
            logger.error(f"Log directory does not exist: {dir_path}. Cannot start watching.")
            self._is_running = False
            return

        # Если файл уже существует при запуске, добавляем его в watcher
        if os.path.exists(self._log_file_path):
            if self._log_file_path not in self._watcher.files():
                 self._watcher.addPath(self._log_file_path)
                 logger.debug(f"Log file {os.path.basename(self._log_file_path)} added to watcher during setup.")
        else:
            logger.warning(self.tr("Файл лога не найден при запуске: {0}. Ожидание создания.").format(os.path.basename(self._log_file_path)))

    def _read_all_current_data(self):
        """
        Открывает файл, читает все доступные новые данные с позиции _file_size_at_last_read,
        обновляет _file_size_at_last_read и ЗАКРЫВАЕТ файл.
        """
        if not self._is_running: return
        if not os.path.exists(self._log_file_path):
            # logger.debug(self.tr("Попытка чтения, но файл лога '{0}' не существует.").format(self._log_file_path))
            self._file_size_at_last_read = 0 # Если файл исчез, сбрасываем позицию
            return

        file = None
        stream = None
        current_pos_before_read = self._file_size_at_last_read # Запоминаем текущую позицию

        try:
            file = QFile(self._log_file_path)
            # Добавлен флаг Unbuffered для более немедленной реакции на запись
            if not file.open(QIODevice.OpenModeFlag.ReadOnly | QIODevice.OpenModeFlag.Text | QIODevice.OpenModeFlag.Unbuffered):
                error_msg = self.tr("Не удалось открыть файл лога для чтения: {0}").format(file.errorString())
                logger.error(error_msg)
                self.error_occurred.emit(error_msg)
                return

            stream = QTextStream(file)
            # Установка кодировки UTF-8, используя QStringConverter
            try:
                stream.setEncoding(QStringConverter.Encoding.Utf8)
                # logger.debug("Установлена кодировка UTF-8 для QTextStream через QStringConverter.")
            except AttributeError as e:
                # Если Utf8 недоступен через QStringConverter.Encoding
                error_msg_encoding = self.tr("Не удалось установить кодировку UTF-8 через QStringConverter.Encoding. Ошибка: {0}").format(e)
                logger.error(error_msg_encoding)
                self.error_occurred.emit(error_msg_encoding)
                try:
                    stream.setEncoding(QStringConverter.Encoding.System)
                    logger.warning(self.tr("Использована системная кодировка как fallback для {0}.").format(os.path.basename(self._log_file_path)))
                except AttributeError as e_system:
                    critical_msg_encoding = self.tr("Критическая ошибка: Не удалось установить системную кодировку через QStringConverter.Encoding. Ошибка: {0}").format(e_system)
                    logger.critical(critical_msg_encoding)
                    self.error_occurred.emit(critical_msg_encoding)
                    file.close() # Убедимся, что файл закрыт
                    return


            current_size = file.size()

            if current_pos_before_read > current_size:
                # Файл был усечен (меньше, чем наша последняя позиция) - это ротация.
                # Начинаем чтение с начала (позиция 0).
                logger.info(self.tr("Файл лога '{0}' усечен (старый размер чтения: {1}, новый размер: {2}). Чтение с начала.").format(
                    os.path.basename(self._log_file_path), current_pos_before_read, current_size))
                file.seek(0)
                self._file_size_at_last_read = 0 # Сбрасываем сохраненную позицию
            else:
                # Файл увеличился или не изменился, или это первое чтение.
                # Начинаем чтение с последней сохраненной позиции.
                file.seek(current_pos_before_read)
                # logger.debug(self.tr("Перемещение к сохраненной позиции {0} в файле лога '{1}'.").format(
                #    current_pos_before_read, os.path.basename(self._log_file_path)))

            # Читаем все доступные новые строки
            lines_read = 0
            while not stream.atEnd():
                line = stream.readLine()
                if line is not None:
                    self.new_log_line.emit(line)
                    lines_read += 1
                else:
                     if stream.status() != QTextStream.Status.Ok:
                         logger.warning(self.tr("Ошибка чтения из QTextStream. Статус: {0}").format(stream.status()))
                     break

            # Обновляем позицию после чтения
            self._file_size_at_last_read = file.pos()
            # if lines_read > 0:
            #      # logger.debug(...) - REMOVED
            #      pass
            # else:
            #      # logger.debug(...) - REMOVED
            #      pass

        except Exception as e:
            error_msg = self.tr("Ошибка при чтении данных из лог-файла '{0}': {1}").format(os.path.basename(self._log_file_path), e)
            logger.error(error_msg, exc_info=True)
            self.error_occurred.emit(error_msg)
        finally:
            # Всегда закрываем файл в конце операции чтения
            if file and file.isOpen():
                file.close()
                # logger.debug(f"Log file {os.path.basename(self._log_file_path)} closed after reading.")


    @Slot(str)
    def _on_file_changed(self, path: str):
        """
        Слот, срабатывающий при изменении файла.
        Запрашивает чтение новых данных.
        """
        if not self._is_running: return
        # logger.debug(self.tr("Сигнал fileChanged для '{0}'. Попытка чтения новых данных.").format(os.path.basename(path))) # Potential trigger
        # Вызываем чтение новых данных, которое откроет, прочитает и закроет файл
        self._read_all_current_data()


    @Slot(str)
    def _on_directory_changed(self, path: str):
        """
        Слот, срабатывающий при изменении директории логов.
        Используется в основном для обнаружения ротации файла.
        """
        if not self._is_running: return

        log_file_exists_now = os.path.exists(self._log_file_path)
        log_file_currently_watched = self._log_file_path in self._watcher.files()

        if not log_file_exists_now and log_file_currently_watched:
            # Файл лога, который мы отслеживали, исчез (вероятно, ротирован).
            # Удаляем старый путь из watcher файлов, он должен был быть закрыт уже
            # благодаря паттерну "открыть-прочитать-закрыть".
            logger.info(self.tr("Файл лога '{0}' исчез. Вероятно, ротация. Удаляем из watcher.").format(os.path.basename(self._log_file_path)))
            try:
                 self._watcher.removePath(self._log_file_path)
                 # logger.debug(f"Removed {self._log_file_path} from watcher files.") # Potential trigger
            except Exception as e:
                 logger.warning(f"Ошибка при удалении пути из watcher: {e}")

            self._file_size_at_last_read = 0 # Сбрасываем позицию для чтения нового файла

        elif log_file_exists_now and not log_file_currently_watched:
            # Файл лога появился, и мы его НЕ отслеживали.
            # Это признак начальной загрузки или ротации, создавшей новый файл с тем же именем.
            logger.info(self.tr("Обнаружен файл лога '{0}'. Вероятно, начальная загрузка или ротация. Начинаем отслеживание и читаем с начала.").format(os.path.basename(self._log_file_path)))
            try:
                 self._watcher.addPath(self._log_file_path) # Добавляем новый (или пересозданный) файл в watcher файлов
                 # logger.debug(f"Added {self._log_file_path} back to watcher files.") # Potential trigger
            except Exception as e:
                 logger.warning(f"Ошибка при добавлении пути в watcher: {e}")

            self._file_size_at_last_read = 0 # Гарантируем чтение с начала для нового файла
            self._read_all_current_data() # Читаем содержимое нового файла

        # else: If file exists and is watched, and directory changed, it's likely harmless.
        # File content changes are handled by _on_file_changed (fileChanged signal).
        # Do nothing else here to avoid re-opening loops.


class LogViewerWindow(QWidget):
    """
    Немодальное окно для отображения логов приложения в реальном времени.
    Включает функцию поиска и подсветки.
    """

    stop_worker_requested = Signal()

    def __init__(self, log_file_path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Логи приложения"))
        self.setMinimumSize(800, 600)
        self.setWindowFlag(Qt.WindowType.Window) # Делает его окном верхнего уровня
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose) # Указываем Qt удалить виджет после закрытия

        self._log_file_path = log_file_path # Сохраняем путь к лог-файлу
        self._thread: Optional[QThread] = None
        self._worker: Optional[LogFileReaderWorker] = None

        self._init_ui()
        # Запускаем чтение при инициализации (теперь это происходит в отдельном потоке)
        self._start_log_reading()

    def _init_ui(self):
        """Инициализирует элементы пользовательского интерфейса окна."""
        main_layout = QVBoxLayout(self)

        # Панель поиска
        search_layout = QHBoxLayout()
        search_label = QLabel(self.tr("Поиск:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(self.tr("Введите текст для поиска..."))
        self.search_input.textChanged.connect(self._highlight_search_text)

        self.prev_match_button = QPushButton("▲")
        self.prev_match_button.setToolTip(self.tr("Предыдущее совпадение (Shift+Enter)"))
        self.prev_match_button.setFixedSize(24, 24)
        self.prev_match_button.clicked.connect(self._find_previous)

        self.next_match_button = QPushButton("▼")
        self.next_match_button.setToolTip(self.tr("Следующее совпадение (Enter)"))
        self.next_match_button.setFixedSize(24, 24)
        self.next_match_button.clicked.connect(self._find_next)

        search_layout.addWidget(search_label)
        search_layout.addWidget(self.search_input, 1)
        search_layout.addWidget(self.prev_match_button)
        search_layout.addWidget(self.next_match_button)

        main_layout.addLayout(search_layout)

        # QTextEdit для отображения логов
        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setFont(QFont("Consolas", 10))
        self.log_text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap) # Отключаем автоматический перенос строк
        main_layout.addWidget(self.log_text_edit)

        # Метка статуса поиска
        self.search_status_label = QLabel(self.tr("Поиск: 0/0"))
        main_layout.addWidget(self.search_status_label)

        self._current_search_query = ""
        self._search_results_count = 0
        self._current_match_index = -1

        # Подключение Enter/Shift+Enter в поле поиска
        self.search_input.returnPressed.connect(self._handle_search_enter_key)

        # Кнопка для очистки логов в окне
        clear_logs_button = QPushButton(self.tr("Очистить окно логов"))
        clear_logs_button.clicked.connect(self.log_text_edit.clear)
        main_layout.addWidget(clear_logs_button, 0, Qt.AlignmentFlag.AlignRight)


    def _handle_search_enter_key(self):
        """Обрабатывает нажатие Enter в поле поиска."""
        if QApplication.queryKeyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._find_previous()
        else:
            self._find_next()

    def _start_log_reading(self):
        """Запускает поток для чтения лог-файла."""
        if self._thread is not None and self._thread.isRunning():
            logger.debug("LogViewerWindow: Поток чтения логов уже запущен.")
            return

        self._thread = QThread(self)
        self._worker = LogFileReaderWorker(self._log_file_path)
        self._worker.moveToThread(self._thread)

        # --- Ключевые соединения ---
        # 1. При старте потока запускаем выполнение воркера
        self._thread.started.connect(self._worker.run)
        # 2. Соединяем наш сигнал-запрос на остановку со слотом воркера
        self.stop_worker_requested.connect(self._worker.stop) 
        # 3. Когда поток окончательно остановится, помечаем его и воркер на удаление
        self._thread.finished.connect(self._worker.deleteLater)

        # --- Соединения для данных и ошибок ---
        self._worker.new_log_line.connect(self.append_log_line)
        self._worker.error_occurred.connect(lambda msg: logger.error(f"Log viewer worker error: {msg}"))

        self._thread.start()
        logger.info(f"LogViewerWindow: Запущено чтение логов из {self._log_file_path} в отдельном потоке.")

    def closeEvent(self, event):
        """Перехватывает событие закрытия окна для остановки потока."""
        logger.debug("LogViewerWindow closeEvent triggered. Инициируется остановка потока.")
        self._stop_log_reading()
        event.accept()

    def _stop_log_reading(self):
        """Инициирует корректную остановку потока чтения логов."""
        logger.debug("Запрос на остановку потока чтения логов...")
        if self._thread and self._thread.isRunning():
            logger.debug("Поток активен. Отправка сигнала stop_worker_requested и ожидание завершения...")
            self.stop_worker_requested.emit()
            
            # Ждем, пока поток действительно завершится.
            if not self._thread.wait(5000): # Даем 5 секунд на завершение
                logger.warning("Поток чтения логов не завершился штатно. Принудительное завершение (terminate).")
                self._thread.terminate()
            else:
                logger.debug("Поток чтения логов успешно остановлен.")
        else:
            logger.debug("Поток не был запущен или уже остановлен, пропуск.")

        # Обнуляем ссылки после того, как поток остановлен
        self._thread = None
        self._worker = None


    @Slot(str)
    def append_log_line(self, line: str):
        """Добавляет новую строку лога в текстовое поле."""
        self.log_text_edit.append(line)
        # Автоматическая прокрутка вниз, если пользователь не прокрутил вверх вручную
        sb = self.log_text_edit.verticalScrollBar()
        # Проверяем, находится ли скроллbar почти внизу
        if sb.maximum() - sb.value() < 50: # Порог в 50 пикселей от нижнего края
            sb.setValue(sb.maximum())

        # Переподсветка, если активен поиск, только для новой добавленной строки
        # Это оптимизация, чтобы не переподсвечивать весь документ каждый раз.
        # Более сложная логика, если документ очень большой, может потребовать
        # пересмотра, но для логов, которые растут, это более эффективно.
        if self._current_search_query:
             # Найти совпадения только в последней добавленной строке
             new_line_start_pos = self.log_text_edit.document().characterCount() - len(line) - 1 # -1 для символа новой строки
             if new_line_start_pos < 0: new_line_start_pos = 0 # На случай пустого документа ранее

             cursor = self.log_text_edit.textCursor()
             cursor.setPosition(new_line_start_pos)

             format = QTextCharFormat()
             format.setBackground(QColor("#a4c639")) # Цвет подсветки

             found_cursor = self.log_text_edit.document().find(self._current_search_query, cursor, QTextDocument.FindFlags())

             while not found_cursor.isNull() and found_cursor.position() >= new_line_start_pos:
                 # Убеждаемся, что совпадение находится в пределах новой строки
                 if found_cursor.position() <= self.log_text_edit.document().characterCount() - 1:
                     found_cursor.mergeCharFormat(format)
                     self._search_results_count += 1 # Увеличиваем общий счетчик
                     found_cursor = self.log_text_edit.document().find(self._current_search_query, found_cursor, QTextDocument.FindFlags())
                 else:
                     break # Вышли за пределы новой строки (случается при перекрытии)
             self._update_search_status() # Обновляем статус-лейбл

    def _highlight_search_text(self, text: str):
        """Подсвечивает найденные совпадения во всем тексте логов."""
        self._current_search_query = text
        self._search_results_count = 0
        self._current_match_index = -1 # Сбрасываем индекс для нового поиска
        self.search_status_label.setText(self.tr("Поиск: 0/0"))

        doc = self.log_text_edit.document()
        cursor = self.log_text_edit.textCursor()

        # Очищаем предыдущую подсветку во всем документе
        cursor.beginEditBlock()
        format = QTextCharFormat()
        format.setBackground(QColor(Qt.GlobalColor.transparent))
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.mergeCharFormat(format)
        cursor.endEditBlock()

        if not text:
            return

        # Подсветка всех совпадений
        highlight_format = QTextCharFormat()
        highlight_format.setBackground(QColor("#a4c639")) # Цвет подсветки

        cursor = doc.find(text, 0)

        while not cursor.isNull():
            cursor.mergeCharFormat(highlight_format)
            self._search_results_count += 1
            cursor = doc.find(text, cursor)

        if self._search_results_count > 0:
            self._current_match_index = 0
            self._update_search_status()
            self._scroll_to_match(0)
        else:
             self.search_status_label.setText(self.tr("Поиск: 0/0"))


    def _find_next(self):
        """Переходит к следующему совпадению."""
        if not self._current_search_query or self._search_results_count <= 1:
            # Если 0 или 1 совпадение, нет смысла искать следующее
            return

        # Выполняем поиск следующего, начиная с текущей позиции курсора
        cursor = self.log_text_edit.textCursor()
        # Устанавливаем начальную позицию поиска после текущего выделения
        start_position = cursor.selectionEnd()
        if start_position >= self.log_text_edit.document().characterCount() - 1:
             start_position = 0 # Если в конце, переходим в начало для циклического поиска

        flags = QTextDocument.FindFlags() # Поиск вперед
        found_cursor = self.log_text_edit.document().find(self._current_search_query, 0, flags)

        if found_cursor.isNull():
             # Если не нашли дальше, начинаем поиск сначала документа (циклически)
             found_cursor = self.log_text_edit.document().find(self._current_search_query, self.log_text_edit.document().findConstants().FindStartIndex, flags)

        if not found_cursor.isNull():
            # Обновляем курсор и прокручиваем
            self.log_text_edit.setTextCursor(found_cursor)
            self.log_text_edit.ensureCursorVisible()
            # Обновляем индекс текущего совпадения
            # Этот способ не идеален, но работает для подсчета в UI
            self._current_match_index = (self._current_match_index + 1) % self._search_results_count
            self._update_search_status()


    def _find_previous(self):
        """Переходит к предыдущему совпадению."""
        if not self._current_search_query or self._search_results_count <= 1:
            # Если 0 или 1 совпадение, нет смысла искать предыдущее
            return

        # Выполняем поиск предыдущего, начиная с текущей позиции курсора
        cursor = self.log_text_edit.textCursor()
        # Устанавливаем начальную позицию поиска до начала текущего выделения
        start_position = cursor.selectionStart()
        if start_position <= 0:
            start_position = self.log_text_edit.document().characterCount() - 1 # Если в начале, переходим в конец для циклического поиска

        flags = QTextDocument.FindFlag.FindBackward # Поиск назад
        found_cursor = self.log_text_edit.document().find(self._current_search_query, start_position, flags)
        found_cursor = self.log_text_edit.document().find(self._current_search_query,
                                                          self.log_text_edit.document().characterCount() - 1, flags)

        if found_cursor.isNull():
            # Если не нашли назад, начинаем поиск с конца документа (циклически)
            found_cursor = self.log_text_edit.document().find(self._current_search_query, self.log_text_edit.document().characterCount() - 1, flags)

        if not found_cursor.isNull():
            # Обновляем курсор и прокручиваем
            self.log_text_edit.setTextCursor(found_cursor)
            self.log_text_edit.ensureCursorVisible()
            # Обновляем индекс текущего совпадения
            self._current_match_index = (self._current_match_index - 1 + self._search_results_count) % self._search_results_count
            self._update_search_status()


    def _update_search_status(self):
        """Обновляет текст статуса поиска."""
        if self._search_results_count > 0:
            self.search_status_label.setText(self.tr("Поиск: {0}/{1}").format(self._current_match_index + 1, self._search_results_count))
        else:
            self.search_status_label.setText(self.tr("Поиск: 0/0"))

    def _scroll_to_match(self, index: int):
        """Прокручивает QTextEdit до указанного совпадения."""
        if not self._current_search_query or index < 0 or index >= self._search_results_count:
            return

        doc = self.log_text_edit.document()

        found_count = 0
        search_cursor = doc.find(self._current_search_query, 0)
        found_count += 1
        search_cursor = doc.find(self._current_search_query, search_cursor)

        while not search_cursor.isNull() and found_count <= index:
            if found_count == index:
                # Выделяем найденный текст
                cursor = self.log_text_edit.textCursor()
                cursor.setPosition(search_cursor.selectionStart())
                cursor.setPosition(search_cursor.selectionEnd(), QTextCursor.MoveMode.KeepAnchor)
                self.log_text_edit.setTextCursor(cursor)
                # Прокручиваем к нему
                self.log_text_edit.ensureCursorVisible()
                break
            found_count += 1
            search_cursor = doc.find(self._current_search_query, search_cursor)


# Предыдущая строка
    def closeEvent(self, event):
        """Перехватывает событие закрытия окна для остановки потока."""
        logger.debug("LogViewerWindow closeEvent triggered.")
        self._stop_log_reading()
        # Принимаем событие, чтобы окно могло быть уничтожено главным приложением.
        # Это позволяет сигналу 'destroyed' сработать корректно.
        event.accept()

    def _stop_log_reading(self):
        """Корректно останавливает поток чтения логов."""
        logger.debug("Запрос на остановку потока чтения логов...")
        if self._thread and self._thread.isRunning():
            logger.debug("Поток активен. Инициируем остановку воркера.")
            # Шаг 1: Просим воркер остановиться. Он, в свою очередь,
            # инициирует очистку и испустит ready_to_quit.
            self._worker.stop()

            # Шаг 2: Ждем, пока поток действительно завершится.
            # Сигнал ready_to_quit вызовет thread.quit(), что позволит wait() завершиться успешно.
            if not self._thread.wait(3000): # Даем 3 секунды на завершение
                logger.warning("Поток чтения логов не завершился штатно за 3 секунды. Принудительное завершение.")
                self._thread.terminate() # Крайняя мера
                self._thread.wait() # Ждем после terminate

            logger.debug("Поток чтения логов успешно остановлен.")
        else:
            logger.debug("Поток не был запущен или уже остановлен, пропуск.")

        # Очищаем ссылки после остановки потока
        # deleteLater() безопасно планирует удаление объектов в цикле событий Qt.
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None
        
        logger.info("LogViewerWindow: Чтение логов остановлено и ресурсы очищены.")
