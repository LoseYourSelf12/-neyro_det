# src/status_parser.py
from typing import Dict

def parse_status_message(msg: str) -> Dict[str, int]:
    """
    Парсит мониторинговое сообщение. На вход ожидаем строку вида 'x' + <HEX>,
    где <HEX> — плотная hex-строка без пробелов.

    Поддерживаются кадры на 18 и 19 байт. Отсутствующие поля -> 0.

    Формат (индексы в байтах, 0-based):
      0..6  : sec, min, hour, weekday, day, month, year(00..99)
      7     : program (1..16)
      8..9  : phase   (LE)  (берём младший байт)
      10..11: takt    (LE)  (берём младший байт)
      12    : флаги такта (не используем)
      13    : Tmin
      14    : Tosn
      15    : remaining (до конца такта)
      16    : cycle_second
      17    : flags18 (режимы/состояния)
      18    : additional_status (может отсутствовать)
    """
    if not msg:
        raise ValueError("empty message")
    if msg[0] in ("x", "n", "X", "N"):
        hex_data = msg[1:]
    else:
        hex_data = msg

    if len(hex_data) % 2 != 0:
        raise ValueError("invalid hex length: %d" % len(hex_data))

    def have(i):
        return (2 * i + 2) <= len(hex_data)

    def b(i, default=0):
        if not have(i):
            return default
        return int(hex_data[2 * i: 2 * i + 2], 16)

    def u16le(i, default=0):
        if not (have(i) and have(i + 1)):
            return default
        lo = b(i)
        hi = b(i + 1)
        return lo | (hi << 8)

    sec      = b(0)
    minute   = b(1)
    hour     = b(2)
    weekday  = b(3)
    day      = b(4)
    month    = b(5)
    year     = b(6)

    program  = b(7)
    if not (1 <= program <= 16):
        program = 0

    phase_le = u16le(8)
    takt_le  = u16le(10)

    tmin         = b(13)
    tosn         = b(14)
    remaining    = b(15)
    cycle_second = b(16)
    flags18      = b(17)
    additional   = b(18, 0)

    phase = phase_le & 0xFF
    takt  = takt_le  & 0xFF

    return {
        "sec": sec,
        "minute": minute,
        "hour": hour,
        "weekday": weekday,
        "day": day,
        "month": month,
        "year": 2000 + year if year <= 99 else year,

        "program": program,
        "phase": phase,
        "takt": takt,

        "tmin": tmin,
        "tosn": tosn,
        "time_left": remaining,
        "cycle_second": cycle_second,

        "flags18": flags18,
        "additional_status": additional,
    }
