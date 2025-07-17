import subprocess
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

app = FastAPI()
ffmpeg_procs = {}

class CameraSpec(BaseModel):
    name: str
    filepath: str

@app.post("/add_camera/")
async def add_camera(spec: CameraSpec):
    """
    Стримим файл по циклу в rtsp-simple-server.
    """
    mount = spec.name
    url = f"rtsp://localhost:8554/{mount}"
    cmd = [
        "ffmpeg",
        "-re",
        "-stream_loop", "-1",
        "-i", spec.filepath,
        "-c", "copy",
        "-f", "rtsp",
        url
    ]
    # Перезапускаем, если уже есть
    if mount in ffmpeg_procs:
        ffmpeg_procs[mount].kill()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ffmpeg_procs[mount] = proc
    return {"url": url}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
