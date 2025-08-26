"""TCP client for communicating with traffic light controller.

The controller (modem) connects to the centre via TCP and speaks a simple
ASCII based protocol.  Messages are encoded in Windows‑1251 and have the
following format::

    <CMD>[<ARG>]<$><CS><\n>

where ``CS`` is the inverted sum of all bytes before the ``$`` symbol.  The
controller responds with messages of the same form where ``CMD`` is either
``!`` (status code) or ``n`` (requested data).

This module implements only the minimum required subset of the protocol:

* handshake on first connection (receive controller name and confirm);
* query current programme using monitoring command ``b02``;
* change programme using command ``g``.

The centre acts as a TCP server.  Upon connection the controller immediately
sends its name using command ``p``.  The centre must acknowledge it with
``!00`` and wait for the ``W`` message that indicates that the controller has
entered working mode.  Afterwards regular commands may be exchanged.

The implementation below is intentionally small and synchronous which makes
it suitable for the existing service.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional, Tuple

from .config import Config
from .status_parser import parse_status_message


ENCODING = "windows-1251"


class ControllerClient:
    """Synchronous TCP client for interacting with the controller."""

    def __init__(self, config: Config) -> None:
        ctrl_cfg = config.get("controller") or {}
        host = ctrl_cfg.get("host", "192.168.1.33")
        port = ctrl_cfg.get("listen_port", 1030)
        timeout = ctrl_cfg.get("timeout", 1)

        self._log = logging.getLogger(self.__class__.__name__)
        self._host = host
        self._timeout = timeout

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("", port))
        self._server.listen(1)
        self._server.settimeout(timeout)

        self._accept()

    # ------------------------------------------------------------------
    # low level helpers
    def _accept(self) -> None:
        """Wait for controller connection and perform handshake."""
        self._log.info(
            "Waiting for controller connection on port %s",
            self._server.getsockname()[1],
        )
        try:
            sock, addr = self._server.accept()
        except socket.timeout as exc:
            raise TimeoutError("Controller connection timeout") from exc

        if addr[0] != self._host:
            self._log.warning("Unexpected controller IP %s", addr[0])
        self._log.info("Controller connected: %s", addr)

        sock.settimeout(self._timeout)
        self._sock = sock

        self._perform_handshake()

    @staticmethod
    def _checksum(payload: str) -> int:
        """Return inverted sum checksum for ``payload``."""
        return (~sum(payload.encode(ENCODING))) & 0xFF

    def _build(self, cmd: str, arg: str = "") -> bytes:
        payload = f"{cmd}{arg}"
        cs = self._checksum(payload)
        return f"{payload}${cs:02X}\n".encode(ENCODING)

    def _build_reply(self, result: str, data: str = "") -> bytes:
        payload = f"{result}{data}"
        cs = self._checksum(payload)
        return f"{payload}${cs:02X}\n".encode(ENCODING)

    def _recv_line(self, timeout: Optional[float] = None) -> str:
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

    # ------------------------------------------------------------------
    # protocol handling
    def _perform_handshake(self) -> None:
        """Handle initial handshake with the controller."""
        try:
            got_name = False
            while True:
                line = self._recv_line(timeout=10)
                if not line:
                    raise TimeoutError("Empty handshake message")
                if "$" not in line:
                    continue
                data, cs_part = line.split("$", 1)
                if self._checksum(data) != int(cs_part[:2], 16):
                    self._log.warning(
                        "Checksum mismatch in handshake message: %s", line
                    )
                    continue
                if data.startswith("p"):
                    self._controller_name = data[1:]
                    self._log.info(
                        "Controller name: %s", self._controller_name
                    )
                    self._sock.sendall(self._build_reply("!", "00"))
                    got_name = True
                    continue
                if data.startswith("W") and got_name:
                    self._log.info("Controller ready for work")
                    break
                if data.startswith("a"):
                    # echo during handshake
                    self._sock.sendall(self._build("a"))
                    continue
        except socket.timeout as exc:
            raise TimeoutError("Handshake with controller timed out") from exc

    def _read_response(self) -> Tuple[str, str]:
        while True:
            line = self._recv_line()
            if not line:
                raise TimeoutError("No response from controller")
            if "$" not in line:
                self._log.warning("Invalid message: %s", line)
                continue
            data, cs_part = line.split("$", 1)
            if self._checksum(data) != int(cs_part[:2], 16):
                self._log.warning("Checksum mismatch in message: %s", line)
                continue
            if data.startswith("a"):
                self._sock.sendall(self._build("a"))
                continue
            if data.startswith("p"):
                # controller can resend its name – acknowledge it
                self._sock.sendall(self._build_reply("!", "00"))
                continue
            if data.startswith("W"):
                # work mode notification – ignore
                continue
            return data[:1], data[1:]

    def _send(self, cmd: str, arg: str = "") -> Tuple[str, str]:
        msg = self._build(cmd, arg)
        self._log.info("-> %s", msg.decode(ENCODING, errors="replace"))
        try:
            self._sock.sendall(msg)
            res, data = self._read_response()
            self._log.info("<- %s%s", res, data)
            return res, data
        except (socket.timeout, ConnectionError, OSError):
            # if something goes wrong, wait for reconnection and retry once
            self._log.warning("Controller connection lost, waiting for reconnect")
            self._accept()
            self._sock.sendall(msg)
            res, data = self._read_response()
            self._log.info("<- %s%s", res, data)
            return res, data

    # ------------------------------------------------------------------
    # public API
    def get_phase_status(self) -> dict:
        """Request monitoring data (command ``b02``)."""
        res, data = self._send("b", "02")
        if res != "n":
            raise RuntimeError(f"Unexpected response: {res}{data}")
        # ``parse_status_message`` expects prefix ``x`` for monitoring frames
        return parse_status_message("x" + data)

    def get_current_program(self) -> int:
        status = self.get_phase_status()
        return int(status["program"])

    def set_program(self, program_id: int) -> bool:
        arg = f"{program_id:02X}"
        res, data = self._send("g", arg)
        ok = res == "!" and data == "00"
        if ok:
            self._log.info("Program changed to %s", program_id)
        else:
            self._log.error(
                "Failed to set program %s: %s%s", program_id, res, data
            )
        return ok

    # ------------------------------------------------------------------
    def close(self) -> None:
        if getattr(self, "_sock", None):
            self._sock.close()
        if getattr(self, "_server", None):
            self._server.close()


__all__ = ["ControllerClient"]

