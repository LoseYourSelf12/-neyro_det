import logging
import socket
import time
import select
import re
from typing import Optional, Tuple

from .config import Config
from .status_parser import parse_status_message

ENCODING = "windows-1251"
_HEX2 = re.compile(r"^[0-9A-Fa-f]{2}$")


def _is_complete_packet(buf: bytes) -> bool:
    """Return True if buf already looks like '<...>$<2HEX>' even without CR/LF."""
    try:
        s = buf.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return False
    if "$" not in s:
        return False
    _, tail = s.split("$", 1)
    return len(tail) >= 2 and bool(_HEX2.match(tail[:2]))


class ControllerClient:
    """Synchronous TCP client for interacting with the controller."""

    def __init__(self, config: Config) -> None:
        ctrl_cfg = config.get("controller") or {}
        host = (ctrl_cfg.get("host") or "*").strip()     # expected peer IP for warning; '*' -> no check
        port = int(ctrl_cfg.get("listen_port", 1030))
        timeout = float(ctrl_cfg.get("timeout", 10))
        bind_host = ctrl_cfg.get("bind_host", "")        # "" => bind on all interfaces

        self._log = logging.getLogger(self.__class__.__name__)
        self._expected_peer: Optional[str] = None if host in ("*", "0.0.0.0", "") else host
        self._timeout = timeout

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((bind_host, port))
        self._server.listen(1)
        self._server.settimeout(1.0)  # tick for accept loop

        self._sock: Optional[socket.socket] = None
        self._accept()

    # ---------------- low-level helpers ----------------

    @staticmethod
    def _checksum(payload: str) -> int:
        """Inverted sum of bytes of payload (encoded), masked to one byte."""
        return (~sum(payload.encode(ENCODING))) & 0xFF

    def _build(self, head: str, data: str = "") -> bytes:
        payload = f"{head}{data}"
        cs = self._checksum(payload)
        return f"{payload}${cs:02X}\n".encode(ENCODING)

    def _recv_line(self, timeout: float = 5.0) -> bytes:
        """
        Read one protocol 'line':
          <HEAD><DATA>$<CS>[CR][LF]
        Accept CR or LF (or CRLF). If no terminator but we already have '$<2HEX>',
        return the buffer on deadline.
        """
        end_time = time.time() + timeout
        buf = bytearray()
        self._sock.settimeout(0.2)  # short ticks

        while True:
            # split by CR/LF if present
            pos_n = buf.find(b"\n")
            pos_r = buf.find(b"\r")
            if pos_n != -1 or pos_r != -1:
                idxs = [p for p in (pos_n, pos_r) if p != -1]
                idx = min(idxs)
                line = bytes(buf[:idx])
                # eat CRLF if present
                if idx + 1 < len(buf) and buf[idx] in (10, 13) and buf[idx + 1] in (10, 13) and buf[idx] != buf[idx + 1]:
                    del buf[: idx + 2]
                else:
                    del buf[: idx + 1]
                return line

            now = time.time()
            if now >= end_time:
                # deadline: if looks complete, return, else return what we have
                return bytes(buf) if _is_complete_packet(buf) else bytes(buf)

            # wait until socket readable
            rlist, _, _ = select.select([self._sock], [], [], min(0.2, end_time - now))
            if not rlist:
                continue

            chunk = self._sock.recv(1024)
            if not chunk:
                # remote closed
                return bytes(buf)
            buf.extend(chunk)

    # ---------------- connection / handshake ----------------

    def _accept(self) -> None:
        """Wait for controller connection and perform handshake."""
        port = self._server.getsockname()[1]
        self._log.info("Waiting for controller connection on port %s", port)

        last_note = 0.0
        while True:
            try:
                sock, addr = self._server.accept()
                break
            except socket.timeout:
                now = time.time()
                if now - last_note > 1.0:
                    last_note = now
                    self._log.info("...still waiting for controller")
                continue

        if self._expected_peer and addr[0] != self._expected_peer:
            self._log.warning("Unexpected controller IP %s (expected %s)", addr[0], self._expected_peer)

        self._log.info("Controller connected: %s", addr)
        sock.settimeout(self._timeout)
        self._sock = sock
        self._perform_handshake()
        self._log.info("Controller connected and ready")

    def _perform_handshake(self) -> None:
        """
        Wait for greeting p<name>$CS, reply with !00 and exit.
        Also handle early 'a' echo, 'y'/'z' (ack !00) and ignore 'w'.
        Stop waiting after a short period even if 'p' wasn't seen (some FW variants).
        """
        deadline = time.time() + 10.0

        while time.time() < deadline:
            line = self._recv_line(timeout=2.0)  # short windows
            if not line:
                continue
            s = line.decode("ascii", errors="ignore").strip("\r\n")
            if not s:
                continue

            head = s[0]
            core, cs = (s.split("$", 1) + [""])[:2] if "$" in s else (s, "")

            if head == "p":
                name = core[1:]
                self._log.info("Controller name: %s", name)
                self._sock.sendall(b"!00$7E\n")
                return
            if head == "a":
                self._sock.sendall(b"a$9E\n")
                continue
            if head in ("y", "z"):
                self._sock.sendall(b"!00$7E\n")
                continue
            if head in ("w",):
                # ignore service notification
                continue
            # any data frame means we are effectively ready
            return

        self._log.warning("Handshake: no 'p...' greeting seen, continue anyway")

    # ---------------- protocol I/O ----------------

    def _read_response(self, want_heads: Tuple[str, ...], timeout: float = 5.0) -> Tuple[str, str]:
        """
        Read next meaningful packet and return (head, data) where 'head' is in want_heads.
        Service frames are handled inline (a echo; y/z ack; p ack; w skip).
        """
        deadline = time.time() + timeout
        while True:
            remain = max(0.05, deadline - time.time())
            if remain <= 0:
                raise TimeoutError("No response from controller")

            raw = self._recv_line(timeout=remain)
            if not raw:
                continue

            s = raw.decode("ascii", errors="ignore").strip("\r\n")
            if not s or "$" not in s:
                continue

            head_and_data, cs = s.split("$", 1)
            if not head_and_data:
                continue
            head = head_and_data[0]
            data = head_and_data[1:]

            # checksum verify if possible
            if len(cs) >= 2:
                reported = cs[:2].upper()
                calc = self._checksum(head_and_data)
                if reported != f"{calc:02X}":
                    self._log.warning("Bad checksum: %r (calc %02X, got %s)", s, calc, reported)
                    continue

            # service frames
            if head == "p":
                try:
                    self._sock.sendall(b"!00$7E\n")
                except Exception:
                    pass
                self._log.debug("ACK sent for greeting")
                continue
            if head == "a":
                try:
                    self._sock.sendall(b"a$9E\n")
                except Exception:
                    pass
                self._log.debug("Echo replied")
                continue
            if head in ("y", "z"):
                try:
                    self._sock.sendall(b"!00$7E\n")
                except Exception:
                    pass
                self._log.debug("ACK sent for %s", head)
                continue
            if head == "w":
                self._log.debug("Skip w-packet: %s", s)
                continue

            # wanted data
            if head in want_heads:
                return head, data

            # otherwise skip
            self._log.debug("Skip packet: %s", s)

    def _send(self, cmd: str, arg: str = "", timeout: float = 5.0) -> Tuple[str, str]:
        """
        Send command and read response of expected type.
        For 'b02' expect data frame 'n' (or 'x'); for others expect result '!'.
        """
        pkt = self._build(cmd, arg)
        try:
            self._log.debug("-> %s", pkt.decode(ENCODING, errors="ignore").strip())
            self._sock.sendall(pkt)

            want = ("!",)
            if cmd == "b" and (arg or "").upper() == "02":
                want = ("n", "x")  # some FW replies with monitoring 'x'
            head, data = self._read_response(want_heads=want, timeout=timeout)
            self._log.debug("<- %s%s", head, data)
            return head, data
        except (socket.timeout, ConnectionError, OSError):
            # reconnect once and retry
            self._log.warning("Controller connection lost, waiting for reconnect")
            self._accept()
            self._sock.sendall(pkt)
            want = ("!",)
            if cmd == "b" and (arg or "").upper() == "02":
                want = ("n", "x")
            head, data = self._read_response(want_heads=want, timeout=timeout)
            self._log.info("<- %s%s", head, data)
            return head, data

    # ---------------- public API ----------------

    def get_phase_status(self) -> dict:
        """Request monitoring data (command 'b02')."""
        head, data = self._send("b", "02", timeout=self._timeout)
        if head not in ("n", "x"):
            raise RuntimeError(f"Unexpected response: {head}{data}")
        # status_parser expects leading 'x'
        return parse_status_message("x" + data)

    def get_current_program(self) -> int:
        st = self.get_phase_status()
        return int(st["program"])

    def set_program(self, program_id: int) -> bool:
        arg = f"{program_id:02X}"
        head, data = self._send("g", arg, timeout=self._timeout)
        ok = (head == "!") and (data == "00")
        if ok:
            self._log.info("Program changed to %s", program_id)
        else:
            self._log.error("Failed to set program %s: %s%s", program_id, head, data)
        return ok

    def close(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        finally:
            try:
                self._server.close()
            except Exception:
                pass


__all__ = ["ControllerClient"]
