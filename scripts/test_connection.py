import socket
import threading
import traceback
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, List

from PyQt6 import QtWidgets, QtCore


# ==========================
# Протокол: утилиты и парсер
# ==========================

def calc_checksum(bytes_before_dollar: bytes) -> str:
    total = sum(bytes_before_dollar) & 0xFF
    inv = (~total) & 0xFF
    return f"{inv:02X}"


def build_command(cmd: str, arg: Optional[str] = None) -> bytes:
    core = (cmd + (arg or "")).encode('ascii')
    checksum = calc_checksum(core)
    return core + b'$' + checksum.encode('ascii') + b'\n'


def ascii_is_hexpairs(s: str) -> bool:
    if len(s) % 2 != 0:
        return False
    try:
        bytes.fromhex(s); return True
    except ValueError:
        return False


# ============
# Мониторинг
# ============

@dataclass
class MonitoringFrame:
    # индексы 0..6 — дата/время
    sec: int
    minute: int
    hour: int
    weekday: int
    day: int
    month: int
    year: int  # 0..99 (год = 2000+year)

    # индекс 7 — НОМЕР ПЛАНА (1..16)
    plan_number: int

    # индексы 8-9 — номер фазы (LE), 10-11 — номер такта (LE)
    phase: int
    takt: int

    # индексы 13..16 — Tmin, Tosn, Remaining, CycleSecond
    tmin: int
    tosn: int
    time_left: int
    cycle_second: int

    # индекс 17 — флаги режимов/состояний
    flags18: int

    raw_bytes: bytes

    @property
    def date_str(self) -> str:
        return f"{self.day:02d}.{self.month:02d}.20{self.year:02d} {self.hour:02d}:{self.minute:02d}:{self.sec:02d}"


def parse_monitoring(hexdata: str) -> Optional[MonitoringFrame]:
    """
    Формат твоих кадров: 18 байт.
    0..6  — дата/время
    7     — plan (1..16)
    8..9  — phase (LE)
    10..11— takt  (LE)
    12    — (служебный байт тактов/флагов — не используем в UI)
    13    — Tmin
    14    — Tosn
    15    — Remaining
    16    — CycleSecond
    17    — flags18 (режимы)
    """
    # валидность hex-пары
    if len(hexdata) % 2 != 0:
        return None
    try:
        b = bytes.fromhex(hexdata)
    except ValueError:
        return None

    if len(b) < 18:
        return None

    def u8(i): return b[i]
    def u16le(i): return b[i] | (b[i+1] << 8)

    try:
        sec = u8(0); minute = u8(1); hour = u8(2); weekday = u8(3)
        day = u8(4); month = u8(5); year = u8(6)

        plan_number = u8(7)
        if not (1 <= plan_number <= 16):
            plan_number = 0  # если вне диапазона — считаем неизвестным

        phase = u16le(8)
        takt  = u16le(10)

        tmin = u8(13)
        tosn = u8(14)
        time_left = u8(15)
        cycle_second = u8(16)
        flags18 = u8(17)

        return MonitoringFrame(
            sec, minute, hour, weekday, day, month, year,
            plan_number, phase, takt, tmin, tosn, time_left, cycle_second,
            flags18, b
        )
    except Exception:
        return None



# ============
# События y...
# ============

@dataclass
class EventFrame:
    sec: int; minute: int; hour: int; day: int; month: int; year: int
    code: int; data_a: int; data_b: int

    @property
    def date_str(self) -> str:
        return f"{self.day:02d}.{self.month:02d}.20{self.year:02d} {self.hour:02d}:{self.minute:02d}:{self.sec:02d}"


def parse_event(hexdata: str) -> Optional[EventFrame]:
    if not ascii_is_hexpairs(hexdata):
        return None
    b = bytes.fromhex(hexdata)
    if len(b) < 9:
        return None
    try:
        return EventFrame(
            sec=b[0], minute=b[1], hour=b[2], day=b[3], month=b[4], year=b[5],
            code=b[6], data_a=b[7], data_b=b[8]
        )
    except Exception:
        return None


def describe_event(ev: EventFrame) -> str:
    c = ev.code; da, db = ev.data_a, ev.data_b
    if c == 0x0B:
        if 1 <= da <= 16:
            return f"План сменён → {da} (DataB=0x{db:02X})"
        return f"План сменён (A={da}, B=0x{db:02X})"
    if c == 0x0F:
        reason = {0x01: "ручное", 0x02: "центральное", 0x03: "АПП", 0x04: "инженерное"}.get(da, f"неизв.(0x{da:02X})")
        return f"Режим смены плана (причина: {reason})"
    if c == 0x0E:
        reason = {0x01: "ручное", 0x02: "центральное"}.get(da, f"неизв.(0x{da:02X})")
        return f"Режим ВФ (причина: {reason})"
    if c == 0x08: return "Работа ДК восстановлена после сбоя"
    if c == 0x09: return "Успешный запуск ДК"
    if c == 0x10: return "Переведён в ручное управление"
    if c == 0x12: return "Переведён в АПП"
    if c == 0x13: return "Переведён в аварийный режим"
    return f"Событие 0x{c:02X} (A=0x{da:02X}, B=0x{db:02X})"


# ==========================
# Общий разбор строк
# ==========================

def parse_incoming_line(line: bytes) -> Dict[str, Any]:
    if not line:
        return {"type": "invalid", "reason": "empty"}
    if line.endswith(b'\r'): line = line[:-1]
    if line.endswith(b'\n'): line = line[:-1]
    try:
        s = line.decode('ascii', errors='strict')
    except UnicodeDecodeError:
        return {"type": "invalid", "reason": "not ascii"}
    if '$' not in s:
        return {"type": "invalid", "reason": "no dollar"}

    head_and_data, cs = s.split('$', 1)
    if len(cs) < 2:
        return {"type": "invalid", "reason": "checksum too short"}
    reported_cs = cs[:2].upper()
    head = head_and_data[:1]
    data = head_and_data[1:]
    calculated = calc_checksum(head_and_data.encode('ascii'))
    checksum_ok = (reported_cs == calculated)

    if head in ('!',): rtype = "result"
    elif head in ('n', 'x'): rtype = "data"
    else: rtype = "command_or_unsolicited"

    return {"type": rtype, "head": head, "data": data,
            "reported_checksum": reported_cs, "calc_checksum": calculated,
            "checksum_ok": checksum_ok, "raw": s}


# ==========================
# TCP-сервер: приём/отправка
# ==========================

class ControllerSession(threading.Thread):
    """
    Обслуживает одно подключение контроллера.
    Читает строки (CR/LF), валидирует КС, парсит мониторинг/ответы/события, уведомляет UI.
    """
    def __init__(self, sock: socket.socket, addr: Tuple[str, int], app_ref: 'MainWindow'):
        super().__init__(daemon=True)
        self.sock = sock
        self.addr = addr
        self.app = app_ref
        self.keep_running = True
        self.buffer = bytearray()
        self.last_activity = time.monotonic()
        self._send_lock = threading.Lock()

    def log(self, msg: str):
        self.app.append_log(f"[{self.addr[0]}:{self.addr[1]}] {msg}")

    def _touch(self):
        self.last_activity = time.monotonic()

    def send_bytes(self, payload: bytes):
        with self._send_lock:
            self.sock.sendall(payload)
        self._touch()
        self.log(f">> {payload!r}")

    def send_ack_ok(self):
        pkt = build_command('!', '00')
        self.send_bytes(pkt)

    def run(self):
        self.log("Подключено.")
        try:
            while self.keep_running:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                self.buffer.extend(chunk)
                self._touch()

                # Режем по \n или \r (поддержка CRLF и одиночных CR/LF)
                while True:
                    pos_n = self.buffer.find(b'\n')
                    pos_r = self.buffer.find(b'\r')
                    candidates = [p for p in (pos_n, pos_r) if p != -1]
                    if not candidates:
                        break
                    idx = min(candidates)
                    line = bytes(self.buffer[:idx])
                    # Съедаем терминатор(ы): если CRLF — оба; иначе один
                    if idx + 1 < len(self.buffer) and self.buffer[idx] in (10, 13) and self.buffer[idx + 1] in (10, 13) and self.buffer[idx] != self.buffer[idx + 1]:
                        self.buffer = self.buffer[idx + 2:]
                    else:
                        self.buffer = self.buffer[idx + 1:]
                    self.handle_line(line)
        except Exception as e:
            self.log(f"Ошибка сессии: {e}\n{traceback.format_exc()}")
        finally:
            try: self.sock.close()
            except Exception: pass
            self.app.session_ended(self)
            self.log("Отключено.")

    def handle_line(self, line: bytes):
        info = parse_incoming_line(line)
        self.log(f"<< {line!r}  parsed={info}")
        if not info.get("checksum_ok"):
            return

        head = info["head"]; data = info["data"]

        if head in ('x', 'n'):
            mf = parse_monitoring(data)
            if mf: self.app.update_state_from_monitoring(mf)

        elif head == 'y':
            # Событие: подтверждаем !00
            self.send_ack_ok()
            ev = parse_event(data)
            if ev: self.app.handle_event(ev)

        elif head == 'p':
            self.send_ack_ok()
            self.log("Отправлен ACK на приветствие (!00).")

        elif head == 'w':
            pass

        elif head == 'z':
            self.send_ack_ok()

        elif head == 'a':
            # Эхо: ответ тем же a$9E
            echo = build_command('a', None)
            self.send_bytes(echo)

        elif head == '!':
            pass

    def stop(self):
        self.keep_running = False
        try: self.sock.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        try: self.sock.close()
        except Exception: pass


class ControllerServer(threading.Thread):
    """
    Простой однопортовый TCP-сервер, принимает одно или несколько подключений контроллера.
    """
    def __init__(self, host: str, port: int, app_ref: 'MainWindow'):
        super().__init__(daemon=True)
        self.host = host; self.port = port; self.app = app_ref
        self.keep_running = True
        self.sock: Optional[socket.socket] = None
        self.sessions: List[ControllerSession] = []
        self._lock = threading.Lock()

    def run(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(5)
            s.settimeout(1.0)
            with self._lock: self.sock = s

            self.app.append_log(f"Сервер слушает {self.host}:{self.port}")

            while self.keep_running:
                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    if not self.keep_running: break
                    self.app.append_log(f"Ошибка accept(): {e}")
                    break

                session = ControllerSession(conn, addr, self.app)
                with self._lock: self.sessions.append(session)
                session.start()

        except Exception as e:
            self.app.append_log(f"Ошибка сервера: {e}\n{traceback.format_exc()}")
        finally:
            self._close_listener(); self._stop_all_sessions()

    def _close_listener(self):
        with self._lock:
            s, self.sock = self.sock, None
        if s:
            try: s.close()
            except Exception: pass

    def _stop_all_sessions(self):
        with self._lock:
            sessions = list(self.sessions)
            self.sessions.clear()
        for sess in sessions:
            try: sess.stop()
            except Exception: pass

    def stop_all(self):
        self.keep_running = False
        self._close_listener(); self._stop_all_sessions()

    def broadcast(self, payload: bytes):
        with self._lock: sessions = list(self.sessions)
        if not sessions:
            self.app.append_log("Нет активных подключений контроллера — отправить нечего.")
        else:
            self.app.append_log(f"Активных подключений: {len(sessions)}")
        for s in sessions:
            try: s.send_bytes(payload)
            except Exception as e:
                self.app.append_log(f"Ошибка отправки {s.addr}: {e}")

    # Keepalive: отправить echo 'a'
    def keepalive_tick(self, quiet_seconds: int):
        now = time.monotonic()
        echo = build_command('a', None)
        to_ping: List[ControllerSession] = []
        with self._lock:
            for sess in self.sessions:
                if (now - sess.last_activity) >= quiet_seconds:
                    to_ping.append(sess)
        for sess in to_ping:
            try:
                sess.send_bytes(echo)
                self.app.append_log(f"[KEEPALIVE] Эхо отправлено на {sess.addr}")
            except Exception as e:
                self.app.append_log(f"[KEEPALIVE] Ошибка отправки {sess.addr}: {e}")

    def remove_session(self, s: ControllerSession):
        with self._lock:
            try: self.sessions.remove(s)
            except ValueError: pass


# =========
# PyQt6 UI
# ==========

class UiBus(QtCore.QObject):
    sig_log = QtCore.pyqtSignal(str)
    sig_monitoring = QtCore.pyqtSignal(object)
    sig_status = QtCore.pyqtSignal(str)
    sig_plan = QtCore.pyqtSignal(int)
    sig_event = QtCore.pyqtSignal(object)


class MainWindow(QtWidgets.QMainWindow):
    stateChanged = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("КДУ-3 — Центр  •  отладочный UI")
        self.resize(1060, 820)

        self.bus = UiBus()
        self.bus.sig_log.connect(self._ui_append_log)
        self.bus.sig_monitoring.connect(self._ui_update_from_monitoring)
        self.bus.sig_status.connect(lambda s: self.statusBar().showMessage(s, 3000))
        self.bus.sig_plan.connect(self._ui_set_plan)
        self.bus.sig_event.connect(self._ui_append_event)

        self.server: Optional[ControllerServer] = None
        self.lastMon: Optional[MonitoringFrame] = None
        self.pending_plan: Optional[int] = None
        self.pending_since: float = 0.0
        self.current_plan: Optional[int] = None
        self._prev_phase: Optional[int] = None
        self._prev_takt: Optional[int] = None

        # Настройки keepalive
        self.keepalive_enabled = True
        self.keepalive_quiet_sec = 25

        # Настройки авто-проверки плана
        self.planpoll_enabled = True
        self.planpoll_period_sec = 5
        self.plan_pending_timeout_sec = 120  # если за это время нет “цикл-сброса” — снимаем ожидание

        # Виджеты
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Кнопки управления сервером
        hb = QtWidgets.QHBoxLayout()
        self.btnStart = QtWidgets.QPushButton("Старт сервера")
        self.btnStop = QtWidgets.QPushButton("Стоп сервера"); self.btnStop.setEnabled(False)
        hb.addWidget(self.btnStart); hb.addWidget(self.btnStop); hb.addStretch(1)
        layout.addLayout(hb)

        # Текущие значения из мониторинга
        grid = QtWidgets.QGridLayout(); row = 0
        def add_row(label_text):
            nonlocal row
            lbl = QtWidgets.QLabel(label_text); val = QtWidgets.QLabel("-")
            grid.addWidget(lbl, row, 0); grid.addWidget(val, row, 1); row += 1
            return val
        self.valDate = add_row("Дата/время ДК:")
        self.valPhase = add_row("Номер фазы:")
        self.valTakt = add_row("Номер такта:")
        self.valCycleSec = add_row("Секунда цикла:")
        self.valPlan = add_row("Текущий план (1..16):")
        self.valFlags = add_row("Флаги (инд.18):")
        self.valTmin = add_row("Tmin (сек):")
        self.valTosn = add_row("Tосн (сек):")
        self.valTimeLeft = add_row("До конца такта (сек):")
        layout.addLayout(grid)

        # Смена плана
        group = QtWidgets.QGroupBox("Смена плана (команда g)")
        gl = QtWidgets.QHBoxLayout(group)
        self.planInput = QtWidgets.QSpinBox(); self.planInput.setRange(1, 16); self.planInput.setValue(6)
        self.planInput.setMinimumWidth(160); self.planInput.setFixedHeight(44)
        self.planInput.setStyleSheet("QSpinBox { font-size: 20px; padding: 6px 10px; }")
        self.btnSendPlan = QtWidgets.QPushButton("Отправить g<plan>")
        self.btnSendPlan.setMinimumWidth(220); self.btnSendPlan.setFixedHeight(44)
        self.btnSendPlan.setStyleSheet("QPushButton { font-size: 18px; padding: 8px 16px; }")
        gl.addWidget(QtWidgets.QLabel("Номер плана (1..16):")); gl.addWidget(self.planInput, 0); gl.addWidget(self.btnSendPlan, 0); gl.addStretch(1)
        layout.addWidget(group)

        # Мониторинг: b01/b00/b02
        hb2 = QtWidgets.QHBoxLayout()
        self.btnB01 = QtWidgets.QPushButton("b01 — включить посекундный")
        self.btnB00 = QtWidgets.QPushButton("b00 — выключить посекундный")
        self.btnB02 = QtWidgets.QPushButton("b02 — мониторинг сейчас")
        hb2.addWidget(self.btnB01); hb2.addWidget(self.btnB00); hb2.addWidget(self.btnB02); hb2.addStretch(1)
        layout.addLayout(hb2)

        # Keepalive (эха)
        hb3 = QtWidgets.QHBoxLayout()
        self.chkKeepalive = QtWidgets.QCheckBox("Авто-echo (a) при тишине >")
        self.spinQuiet = QtWidgets.QSpinBox(); self.spinQuiet.setRange(5, 300); self.spinQuiet.setValue(self.keepalive_quiet_sec)
        hb3.addWidget(self.chkKeepalive); hb3.addWidget(self.spinQuiet); hb3.addWidget(QtWidgets.QLabel("сек"))
        hb3.addStretch(1)
        layout.addLayout(hb3)
        self.chkKeepalive.setChecked(self.keepalive_enabled)

        # Авто-проверка плана (b02)
        hb4 = QtWidgets.QHBoxLayout()
        self.chkPlanPoll = QtWidgets.QCheckBox("Авто-проверка плана (b02) каждые")
        self.spinPlanPoll = QtWidgets.QSpinBox(); self.spinPlanPoll.setRange(2, 60); self.spinPlanPoll.setValue(self.planpoll_period_sec)
        self.spinPlanTimeout = QtWidgets.QSpinBox(); self.spinPlanTimeout.setRange(10, 600); self.spinPlanTimeout.setValue(self.plan_pending_timeout_sec)
        hb4.addWidget(self.chkPlanPoll); hb4.addWidget(self.spinPlanPoll); hb4.addWidget(QtWidgets.QLabel("сек"))
        hb4.addSpacing(20)
        hb4.addWidget(QtWidgets.QLabel("Таймаут ожидания смены:"))
        hb4.addWidget(self.spinPlanTimeout); hb4.addWidget(QtWidgets.QLabel("сек"))
        hb4.addStretch(1)
        layout.addLayout(hb4)
        self.chkPlanPoll.setChecked(self.planpoll_enabled)

        # Логи
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(5000)
        layout.addWidget(QtWidgets.QLabel("Лог обмена:")); layout.addWidget(self.log, 1)

        # Сигналы
        self.btnStart.clicked.connect(self.start_server)
        self.btnStop.clicked.connect(self.stop_server)
        self.btnSendPlan.clicked.connect(self.send_change_plan)
        self.btnB01.clicked.connect(self.enable_per_second_monitoring)
        self.btnB00.clicked.connect(self.disable_per_second_monitoring)
        self.btnB02.clicked.connect(self.send_request_monitoring_once)
        self.chkKeepalive.stateChanged.connect(self._keepalive_changed)
        self.spinQuiet.valueChanged.connect(self._quiet_changed)
        self.chkPlanPoll.stateChanged.connect(self._planpoll_changed)
        self.spinPlanPoll.valueChanged.connect(self._planpoll_period_changed)
        self.spinPlanTimeout.valueChanged.connect(self._plan_timeout_changed)

        # Таймеры
        self._ka_timer = QtCore.QTimer(self); self._ka_timer.setInterval(5000)
        self._ka_timer.timeout.connect(self._tick_keepalive); self._ka_timer.start()

        self._plan_timer = QtCore.QTimer(self); self._plan_timer.setInterval(self.planpoll_period_sec * 1000)
        self._plan_timer.timeout.connect(self._tick_plan_poll)
        if self.planpoll_enabled: self._plan_timer.start()

    # --------- Потоκобезопасный лог/обновление UI -----------
    def append_log(self, text: str):
        self.bus.sig_log.emit(text)

    @QtCore.pyqtSlot(str)
    def _ui_append_log(self, text: str):
        self.log.appendPlainText(text)

    def update_state_from_monitoring(self, mf: MonitoringFrame):
        self.bus.sig_monitoring.emit(mf)

    @QtCore.pyqtSlot(object)
    def _ui_update_from_monitoring(self, mf: MonitoringFrame):
        # если мониторинг принёс план 1..16 — это истина
        if 1 <= mf.plan_number <= 16:
            if self.current_plan != mf.plan_number:
                self.current_plan = mf.plan_number
                self.pending_plan = None
                self._ui_append_log(f"[PLAN][MON] Обнаружен план из мониторинга = {self.current_plan}")
        # ------------------------------------------

        self.lastMon = mf
        self.valDate.setText(mf.date_str)
        self.valPhase.setText(str(mf.phase))
        self.valTakt.setText(str(mf.takt))
        self.valCycleSec.setText(str(mf.cycle_second))
        self.valFlags.setText(f"0x{mf.flags18:02X}")
        self.valTmin.setText(str(mf.tmin))
        self.valTosn.setText(str(mf.tosn))
        self.valTimeLeft.setText(str(mf.time_left))

        # Отрисовка плана
        if self.current_plan:
            self.valPlan.setText(str(self.current_plan))
        else:
            if self.pending_plan is not None:
                self.valPlan.setText(f"ожидается {self.pending_plan}")
            else:
                self.valPlan.setText("—")

        self._ui_append_log(
            f"[MON] {mf.date_str}  phase={mf.phase} takt={mf.takt} "
            f"cycleSec={mf.cycle_second} flags18=0x{mf.flags18:02X} "
            f"plan={self.current_plan or (mf.plan_number or 'n/a')}"
        )

        self._prev_phase, self._prev_takt = mf.phase, mf.takt

    # --------- События ---------
    def handle_event(self, ev: EventFrame):
        self.bus.sig_event.emit(ev)
        if ev.code == 0x0B and 1 <= ev.data_a <= 16:
            self.bus.sig_plan.emit(ev.data_a)

    @QtCore.pyqtSlot(object)
    def _ui_append_event(self, ev: EventFrame):
        self._ui_append_log(f"[EVT] {ev.date_str}  code=0x{ev.code:02X} A=0x{ev.data_a:02X} B=0x{ev.data_b:02X}  — {describe_event(ev)}")

    @QtCore.pyqtSlot(int)
    def _ui_set_plan(self, plan: int):
        self.current_plan = plan; self.pending_plan = None
        self.valPlan.setText(str(plan))
        self._ui_append_log(f"[PLAN] Текущий план = {plan}")

    def session_ended(self, s: ControllerSession):
        if self.server:
            self.server.remove_session(s)

    # ------ Сеть: старт/стоп сервера

    def start_server(self):
        if self.server is not None:
            self.append_log("Сервер уже запущен."); return
        self.server = ControllerServer('0.0.0.0', 1030, self)
        self.server.start()
        self.btnStart.setEnabled(False); self.btnStop.setEnabled(True)
        self.append_log("Сервер запущен.")

    def stop_server(self):
        if self.server:
            self.server.stop_all(); self.server = None
        self.btnStart.setEnabled(True); self.btnStop.setEnabled(False)
        self.append_log("Сервер остановлен.")

    # ------ Команды

    def send_change_plan(self):
        plan_ui = self.planInput.value()
        arg_hex = f"{plan_ui:02X}"
        pkt = build_command('g', arg_hex)
        self.pending_plan = plan_ui
        self.pending_since = time.monotonic()
        self._prev_phase = None; self._prev_takt = None
        self.append_log(f"Отправка g{arg_hex} ... (ожидаем переход на план {self.pending_plan} после завершения текущего)")
        if self.server: self.server.broadcast(pkt)
        else: self.append_log("Нет активного сервера: запусти сервер и дождись подключения контроллера.")

    def enable_per_second_monitoring(self):
        pkt = build_command('b', "01")
        self.append_log("Включаю посекундный мониторинг (b01).")
        if self.server: self.server.broadcast(pkt)

    def disable_per_second_monitoring(self):
        pkt = build_command('b', "00")
        self.append_log("Выключаю посекундный мониторинг (b00).")
        if self.server: self.server.broadcast(pkt)

    def send_request_monitoring_once(self):
        pkt = build_command('b', "02")
        self.append_log("Запрос мониторинга сейчас (b02).")
        if self.server: self.server.broadcast(pkt)

    # ------ Keepalive echo

    def _keepalive_changed(self, state: int):
        self.keepalive_enabled = (state == QtCore.Qt.CheckState.Checked)

    def _quiet_changed(self, val: int):
        self.keepalive_quiet_sec = val

    def _tick_keepalive(self):
        if self.keepalive_enabled and self.server:
            self.server.keepalive_tick(self.keepalive_quiet_sec)

    # ------ План-поллинг

    def _planpoll_changed(self, state: int):
        self.planpoll_enabled = (state == QtCore.Qt.CheckState.Checked)
        if self.planpoll_enabled:
            self._plan_timer.start()
        else:
            self._plan_timer.stop()

    def _planpoll_period_changed(self, val: int):
        self.planpoll_period_sec = val
        self._plan_timer.setInterval(self.planpoll_period_sec * 1000)

    def _plan_timeout_changed(self, val: int):
        self.plan_pending_timeout_sec = val

    def _tick_plan_poll(self):
        # 1) Попросим свежий кадр
        if self.server:
            self.server.broadcast(build_command('b', '02'))

        # 2) Если долго “ждём план” — снимем ожидание (чтобы не висело вечно)
        if self.pending_plan is not None:
            waited = time.monotonic() - self.pending_since
            if waited >= self.plan_pending_timeout_sec:
                self._ui_append_log(f"[PLAN][TIMEOUT] Не удалось подтвердить смену плана на {self.pending_plan} за {int(waited)} с — снимаю ожидание.")
                self.pending_plan = None
                if not self.current_plan:
                    self.valPlan.setText("—")


# ==========================
# main
# ==========================

def main():
    import sys
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
