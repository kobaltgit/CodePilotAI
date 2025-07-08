# --- Файл: network_checker.py ---

import socket
import logging
from PySide6.QtCore import QObject, Signal, QTimer

logger = logging.getLogger(__name__)

class NetworkStatusChecker(QObject):
    """
    Воркер, который периодически проверяет доступность интернета
    и сообщает о смене статуса. Работает в отдельном потоке.
    """
    status_changed = Signal(bool)  # True, если онлайн, False, если оффлайн

    def __init__(self, check_interval_ms=5000, host="8.8.8.8", port=53, timeout=3):
        """
        :param check_interval_ms: Интервал проверки в миллисекундах.
        :param host: Хост для проверки соединения (DNS Google по умолчанию).
        :param port: Порт для проверки.
        :param timeout: Таймаут для попытки соединения.
        """
        super().__init__()
        self._timer = QTimer(self)
        self._timer.setInterval(check_interval_ms)
        self._timer.timeout.connect(self._check_network_status)

        self._host = host
        self._port = port
        self._timeout = timeout

        self._last_status: bool | None = None

    def start_checking(self):
        """Запускает таймер для периодических проверок."""
        logger.info("Запущена периодическая проверка статуса сети.")
        self._check_network_status()  # Первичная проверка сразу при запуске
        self._timer.start()

    def stop_checking(self):
        """Останавливает таймер."""
        logger.info("Остановлена периодическая проверка статуса сети.")
        self._timer.stop()

    def _check_network_status(self):
        """Выполняет проверку и эмитирует сигнал, если статус изменился."""
        is_online = False
        try:
            socket.setdefaulttimeout(self._timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((self._host, self._port))
            is_online = True
        except (socket.error, OSError) as ex:
            is_online = False

        if is_online != self._last_status:
            logger.info(f"Статус сети изменен: {'ОНЛАЙН' if is_online else 'ОФФЛАЙН'}")
            self._last_status = is_online
            self.status_changed.emit(is_online)
