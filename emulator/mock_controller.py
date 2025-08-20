from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from threading import Lock, Thread
import time

app = FastAPI(title="Mock Traffic Controller")

# Определения программ: две фазы (шоссе и примыкающая дорога)
# значения представляют длительность [фаза1, фаза2]
PROGRAM_DEFINITIONS = {
    4: [35, 8],
    5: [40, 8],
    6: [45, 8],
    7: [55, 8],
    8: [35, 20],
    9: [40, 20],
    10: [45, 20],
    11: [55, 20],
}

_state = {
    "program": 4,
    "phase": 0,
    "time_left": PROGRAM_DEFINITIONS[4][0],
}
_state_lock = Lock()


def _build_status_string() -> str:
    """Construct hex status string similar to real controller."""
    with _state_lock:
        program = _state["program"]
        phase = _state["phase"]
        time_left = int(_state["time_left"])
        base_time = PROGRAM_DEFINITIONS[program][phase]

    now = time.localtime()
    fields = [
        now.tm_sec,
        now.tm_min,
        now.tm_hour,
        now.tm_wday + 1,
        now.tm_mday,
        now.tm_mon,
        now.tm_year - 2000,
        program,
        phase,
        0,
        phase,
        0,
        0,
        0,
        base_time,
        time_left,
        base_time - time_left,
        0,
        0,
    ]
    hex_str = ''.join(f"{b:02X}" for b in fields)
    return f"wsO_CMD:electric_coal:x{hex_str}"


class ProgramRequest(BaseModel):
    program: int


@app.get("/api/program")
async def get_program():
    with _state_lock:
        return {"program": _state["program"]}


@app.post("/api/program")
async def set_program(req: ProgramRequest):
    if req.program not in PROGRAM_DEFINITIONS:
        raise HTTPException(status_code=400, detail="Invalid program")
    with _state_lock:
        _state["program"] = req.program
        # сброс фазы на 0 и таймера
        _state["phase"] = 0
        _state["time_left"] = PROGRAM_DEFINITIONS[req.program][0]
    return {"status": "ok", "program": _state["program"]}


@app.get("/api/phase_status", response_class=PlainTextResponse)
async def phase_status():
    """Возвратить строку статуса контроллера в шестнадцатеричном виде."""
    return _build_status_string()


def _phase_timer_loop():
    """
    В отдельном потоке каждую секунду отнимаем time_left,
    и как только доходит до нуля – переключаем фазу.
    """
    while True:
        time.sleep(1)
        with _state_lock:
            _state["time_left"] -= 1
            if _state["time_left"] <= 0:
                prog = _state["program"]
                phases = PROGRAM_DEFINITIONS[prog]
                # следующий индекс фазы
                _state["phase"] = (_state["phase"] + 1) % len(phases)
                _state["time_left"] = phases[_state["phase"]]


if __name__ == "__main__":
    # Запускаем фон-поток таймера
    t = Thread(target=_phase_timer_loop, daemon=True)
    t.start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
