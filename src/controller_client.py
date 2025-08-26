import logging
from typing import Tuple, Optional
import socket

from .config import Config
from .status_parser import parse_status_message


ENCODING = "windows-1251"


class ControllerClient:
    """Коммуникация с контроллером по TCP/IP.

    Обмен ведётся согласно протоколу КДУ-3:

    `<COM>[<ARG>]<$><CHECKSUM><0x0A>`

    Где ``CHECKSUM`` — инвертированная сумма всех байтов до символа ``$``.
    Ответ имеет вид ``<RESULT><DATA>$<CHECKSUM><0x0A>``. ``RESULT`` может быть
    ``!`` (код исполнения) или ``n`` (данные).

    Клиент предоставляет три публичных метода:

    ``get_current_program()`` – вернуть номер текущего плана;
    ``set_program(id)`` – переключить план на ``id``;
    ``get_phase_status()`` – получить структуру статуса, содержащую план,
    фазу, оставшееся время и т.п.
    """

    def __init__(self, config: Config) -> None:
        ctrl_cfg = config.get("controller") or {}
        host = ctrl_cfg.get("host", "192.168.1.33")
        port = ctrl_cfg.get("listen_port", 1030)
        timeout = ctrl_cfg.get("timeout", 1)

        self._log = logging.getLogger(self.__class__.__name__)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("", port))
        server.listen(1)
        server.settimeout(timeout)
        self._log.info("Waiting for controller connection on port %s", port)
        try:
            self._sock, addr = server.accept()
        except socket.timeout:
            server.close()
            raise TimeoutError("Controller connection timeout")
        if addr[0] != host:
            self._log.warning("Unexpected controller IP %s", addr[0])
        self._log.info("Controller connected: %s", addr)
        self._sock.settimeout(timeout)
        self._server = server

        # После установления соединения модем отправляет своё имя и ожидает
        # подтверждения от центра, а затем сообщение о переходе в рабочий
        # режим. Обрабатываем этот обмен, чтобы дальнейшие запросы не
        # натолкнулись на "Unexpected response: p...".
        self._perform_handshake()


    # ------------------------------------------------------------------
    # low level helpers
    @staticmethod
    def _checksum(payload: str) -> int:
        """Calculate inverted sum checksum for payload encoded in Windows-1251."""
        return (~sum(payload.encode(ENCODING))) & 0xFF

    def _build(self, com: str, arg: str = "") -> bytes:
        payload = f"{com}{arg}"
        cs = self._checksum(payload)
        return f"{payload}${cs:02X}\n".encode(ENCODING)

    def _build_reply(self, result: str, data: str = "") -> bytes:
        payload = f"{result}{data}"
        cs = self._checksum(payload)
        return f"{payload}${cs:02X}\n".encode(ENCODING)

    def _recv_line(self, timeout: Optional[float] = None) -> str:
        """Receive one line from socket and decode it."""
        prev_timeout = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        data = b""
        while not data.endswith(b"\n"):
            chunk = self._sock.recv(1024)
            if not chunk:
                break
            data += chunk
        if timeout is not None:
            self._sock.settimeout(prev_timeout)
        return data.decode(ENCODING, errors="replace").strip()

    def _perform_handshake(self) -> None:
        """Handle initial modem handshake (name and work mode message)."""
        try:
            while True:
                line = self._recv_line(timeout=10)
                if not line:
                    break
                if "$" not in line:
                    continue
                data_part, cs_part = line.split("$", 1)
                recv_cs = int(cs_part[:2], 16)
                if self._checksum(data_part) != recv_cs:
                    self._log.warning("Checksum mismatch in handshake message: %s", line)
                    continue
                if data_part.startswith("p"):
                    name = data_part[1:]
                    self._log.info("Controller name: %s", name)
                    self._sock.sendall(self._build_reply("!", "00"))
                    continue
                if data_part.startswith("W"):
                    self._log.info("Controller ready for work")
                    break
                if data_part.startswith("a"):
                    # echo during handshake
                    self._sock.sendall(self._build("a"))
        except socket.timeout:
            self._log.warning("Handshake with controller timed out")

    def _read_response(self) -> Tuple[str, str]:
        """Read next meaningful message from controller."""
        while True:
            line = self._recv_line()
            if not line:
                raise TimeoutError("No response from controller")
            if "$" not in line:
                self._log.warning("Invalid message: %s", line)
                continue
            data_part, cs_part = line.split("$", 1)
            recv_cs = int(cs_part[:2], 16)
            if self._checksum(data_part) != recv_cs:
                self._log.warning("Checksum mismatch in message: %s", line)
                continue
            if data_part.startswith("a"):
                self._log.debug("Echo request")
                self._sock.sendall(self._build("a"))
                continue
            if data_part.startswith("p"):
                self._log.debug("Name message: %s", data_part[1:])
                self._sock.sendall(self._build_reply("!", "00"))
                continue
            if data_part.startswith("W"):
                self._log.debug("Work mode notification")
                continue
            result = data_part[:1]
            data = data_part[1:]
            return result, data

    def _send(self, com: str, arg: str = "") -> Tuple[str, str]:
        """Send command and return tuple (result, data)."""
        msg = self._build(com, arg)
        self._log.info("-> %s", msg.decode(ENCODING, errors="replace"))
        self._sock.sendall(msg)
        try:
            result, data = self._read_response()
        except socket.timeout:
            raise TimeoutError("No response from controller")
        self._log.info("<- %s%s", result, data)
        return result, data

    # ------------------------------------------------------------------
    # public API
    def get_phase_status(self) -> dict:
        """Запросить мониторинг (b02) и распарсить статус контроллера."""
        res, data = self._send("b", "02")
        if res != "n":
            raise RuntimeError(f"Unexpected response: {res}{data}")
        # parse_status_message ожидает строку с префиксом 'x'
        parsed = parse_status_message("x" + data)
        self._log.debug("Parsed status: %s", parsed)
        return parsed

    def get_current_program(self) -> int:
        """Вернуть ID текущей программы, используя мониторинг."""
        status = self.get_phase_status()
        return int(status["program"])

    def set_program(self, program_id: int) -> bool:
        """Сменить план работы контроллера командой 'g'."""
        arg = f"{program_id:02X}"
        res, data = self._send("g", arg)
        success = res == "!" and data == "00"
        if success:
            self._log.info("Program changed to %s", program_id)
        else:
            self._log.error("Failed to set program %s: %s%s", program_id, res, data)
        return success

    # ------------------------------------------------------------------
    def close(self) -> None:
        if getattr(self, "_sock", None):
            self._sock.close()
        if getattr(self, "_server", None):
            self._server.close()


__all__ = ["ControllerClient"]

