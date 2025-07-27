import datetime


def _byte(hex_data: str, index: int) -> int:
    return int(hex_data[2 * index: 2 * index + 2], 16)


def parse_status_message(message: str) -> dict:
    """Parse controller status message string.

    The message is expected in format ``wsO_CMD:electric_coal:x<HEX>`` where
    ``<HEX>`` is a sequence of hexadecimal byte values.
    Returns a dictionary with at least keys ``program``, ``phase`` and
    ``time_left``. Extra fields are also provided for completeness.
    """
    if 'x' not in message:
        raise ValueError('Invalid status message')
    hex_data = message.split('x', 1)[1].strip()
    if len(hex_data) % 2 != 0:
        raise ValueError('Odd length of hex payload')

    def b(idx: int) -> int:
        return int(hex_data[2 * idx: 2 * idx + 2], 16)

    seconds = b(0)
    minutes = b(1)
    hours = b(2)
    week_day = b(3)
    day = b(4)
    month = b(5)
    year = 2000 + b(6)

    plan_number = b(7)

    phase_low = b(8)
    phase_high = b(9)
    tact_low = b(10)
    tact_high = b(11)

    tact_flags = b(12)
    min_time = b(13)
    base_time = b(14)
    remaining_time = b(15)
    current_cycle_second = b(16)

    tvp_status = b(17)
    additional_status = b(18)

    result = {
        'datetime': datetime.datetime(year, month, day, hours, minutes, seconds),
        'week_day': week_day,
        'program': plan_number,
        'phase': phase_low,
        'tact': tact_low,
        'tact_flags': tact_flags,
        'min_time': min_time,
        'base_time': base_time,
        'time_left': remaining_time,
        'cycle_second': current_cycle_second,
        'tvp_status': tvp_status,
        'additional_status': additional_status,
    }

    # convenience boolean flags
    result.update({
        'main_tact': bool(tact_flags & 0x01),
        'prom_tact': bool(tact_flags & 0x02),
        'tvp1_tact': bool(tact_flags & 0x04),
        'tvp2_tact': bool(tact_flags & 0x08),
        'is_last_tact': bool(tact_flags & 0x10),
        'door_open': bool(tact_flags & 0x40),
        'power_signal': bool(tact_flags & 0x80),
        'tvp1_call': bool(tvp_status & 0x01),
        'tvp2_call': bool(tvp_status & 0x02),
        'tvp1_phase': bool(tvp_status & 0x04),
        'tvp2_phase': bool(tvp_status & 0x08),
        'manual_mode': bool(tvp_status & 0x10),
        'app_mode': bool(tvp_status & 0x20),
        'tvp1_inactive': bool(tvp_status & 0x40),
        'tvp2_inactive': bool(tvp_status & 0x80),
        'fast_plan_change': bool(additional_status & 0x01),
        'fast_plan_change_mode': bool(additional_status & 0x02),
        'center_plan_change': bool(additional_status & 0x04),
        'center_plan_active': bool(additional_status & 0x08),
        'vf_mode_activated': bool(additional_status & 0x10),
        'vf_mode': bool(additional_status & 0x20),
        'engineer_mode': bool(additional_status & 0x40),
        'emergency_mode': bool(additional_status & 0x80),
    })

    return result
