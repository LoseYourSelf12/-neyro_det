#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import uuid
import signal
import logging
import argparse
import tempfile
from datetime import datetime
from threading import Thread, Event
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from urllib3.util.retry import Retry

# ---------- Конфигурация камер ----------
# Указывайте URL БЕЗ логина/пароля. Логин/пароль — отдельными полями.
# port=8881/8882/8883 — как у вас. path и params можно менять при необходимости.
CAMERAS = [
    {
        "name": "cam1",
        "url": "http://192.168.101.2:8881/ISAPI/Streaming/Channels/101/picture",
        "params": {"snapShotImageType": "JPEG"},
        "user": "admin",
        "password": "1qazxsw2",
        "out_dir": "./snapshots/cam1",
    },
    {
        "name": "cam2",
        "url": "http://192.168.101.2:8882/ISAPI/Streaming/Channels/101/picture",
        "params": {"snapShotImageType": "JPEG"},
        "user": "admin",
        "password": "1qazxsw2",
        "out_dir": "./snapshots/cam2",
    },
    {
        "name": "cam3",
        "url": "http://192.168.101.2:8883/ISAPI/Streaming/Channels/101/picture",
        "params": {"snapShotImageType": "JPEG"},
        "user": "admin",
        "password": "1qazxsw2",
        "out_dir": "./snapshots/cam3",
    },
]
# ----------------------------------------


def build_session(total_retries: int = 3, backoff: float = 0.5, timeout: float = 5.0):
    sess = requests.Session()
    retry = Retry(
        total=total_retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    def _get(url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return sess._orig_get(url, **kwargs)

    def _head(url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return sess._orig_head(url, **kwargs)

    sess._orig_get = sess.get
    sess._orig_head = sess.head
    sess.get = _get  # type: ignore
    sess.head = _head  # type: ignore
    return sess


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def unique_name(cam_name: str, ext: str = "jpeg") -> str:
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S_%f")[:-3]
    u = uuid.uuid4().hex[:8]
    return f"img_{cam_name}_{stamp}_{u}.{ext}"


def save_atomically(target_dir: str, filename: str, content: bytes):
    ensure_dir(target_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=target_dir)
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        final_path = os.path.join(target_dir, filename)
        os.replace(tmp_path, final_path)
        return final_path
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def normalize_url(url: str, extra_params: dict | None) -> str:
    """Убираем логин/пароль из URL и аккуратно добавляем query-параметры."""
    pu = urlparse(url)
    # выбросим креды, если вдруг были
    pu = pu._replace(netloc=pu.hostname if pu.port is None else f"{pu.hostname}:{pu.port}")
    if extra_params:
        qs = dict(parse_qsl(pu.query))
        qs.update(extra_params)
        pu = pu._replace(query=urlencode(qs))
    return urlunparse(pu)


def pick_extension(content_type: str | None) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return "png"
    if "bmp" in ct:
        return "bmp"
    # по умолчанию/чаще всего — jpeg
    return "jpeg"


def fetch_snapshot(session: requests.Session, url: str, user: str, password: str, logger: logging.Logger):
    """
    Пробуем Digest -> если 401 и сервер предлагает Basic — пробуем Basic.
    Возвращает (bytes | None, status_code, www_authenticate_header)
    """
    # 1) Digest
    try:
        resp = session.get(url, auth=HTTPDigestAuth(user, password), allow_redirects=False)
        if resp.status_code == 200 and resp.content:
            return resp.content, 200, resp.headers.get("WWW-Authenticate")
        elif resp.status_code == 401:
            www = resp.headers.get("WWW-Authenticate", "")
            logger.debug("401 после Digest; WWW-Authenticate: %s", www)
            # Если сервер намекает на Basic — пробуем Basic
            if "basic" in www.lower():
                resp2 = session.get(url, auth=HTTPBasicAuth(user, password), allow_redirects=False)
                return (resp2.content if resp2.status_code == 200 else None,
                        resp2.status_code,
                        resp2.headers.get("WWW-Authenticate"))
            else:
                return None, 401, www
        else:
            return (resp.content if resp.content else None), resp.status_code, resp.headers.get("WWW-Authenticate")
    except requests.exceptions.RequestException as e:
        logger.warning("Ошибка запроса: %s", e)
        return None, 0, None


def capture_loop(cam: dict, interval: float, stop_evt: Event, session: requests.Session):
    name = cam["name"]
    url_raw = cam["url"]
    url = normalize_url(url_raw, cam.get("params"))
    out_dir = cam["out_dir"]
    user = cam.get("user")
    password = cam.get("password")

    logger = logging.getLogger(f"grabber.{name}")
    logger.info("Старт захвата: %s -> %s (интервал %.3fs)", url, out_dir, interval)

    while not stop_evt.is_set():
        t0 = time.time()
        content, code, www = fetch_snapshot(session, url, user, password, logger)

        if content and code == 200:
            # определим расширение
            # (тут content-type недоступен, так как мы его вернули из fetch_snapshot только для логов;
            #  большинство камер всё равно отдают image/jpeg)
            ext = "jpeg"
            fname = unique_name(name, ext=ext)
            try:
                save_atomically(out_dir, fname, content)
                logger.debug("Сохранено: %s (%d байт)", os.path.join(out_dir, fname), len(content))
            except Exception as e:
                logger.exception("Не удалось сохранить файл: %s", e)
        else:
            # Больше подсказок в лог
            if code == 401:
                logger.warning("HTTP 401 от %s. Подсказка WWW-Authenticate: %s", name, www or "—")
            elif code == 0:
                # уже было залогировано внутри fetch_snapshot
                pass
            else:
                logger.warning("HTTP %s или пустой ответ от %s. WWW-Authenticate: %s", code, name, www or "—")

        elapsed = time.time() - t0
        remaining = max(0.0, interval - elapsed)
        stop_evt.wait(remaining)

    logger.info("Остановка захвата: %s", name)


def parse_args():
    p = argparse.ArgumentParser(description="Периодический снимок с камер (ISAPI snapshot) в отдельные папки.")
    p.add_argument("-i", "--interval", type=float, default=5.0, help="Интервал между снимками (сек). По умолчанию 1.0")
    p.add_argument("-v", "--verbose", action="store_true", help="Подробные логи (DEBUG). По умолчанию INFO.")
    return p.parse_args()


def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        import requests  # noqa
    except ImportError:
        print("Требуется пакет 'requests': pip install requests", file=sys.stderr)
        sys.exit(1)

    session = build_session()
    stop_evt = Event()

    def _handle_sig(signum, frame):
        logging.getLogger("grabber").info("Получен сигнал %s — завершаемся…", signum)
        stop_evt.set()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    threads = []
    for cam in CAMERAS:
        ensure_dir(cam["out_dir"])
        th = Thread(target=capture_loop, args=(cam, args.interval, stop_evt, session), daemon=True)
        th.start()
        threads.append(th)

    try:
        while not stop_evt.is_set():
            time.sleep(0.3)
    except KeyboardInterrupt:
        stop_evt.set()

    for th in threads:
        th.join()

    logging.getLogger("grabber").info("Готово. Все потоки остановлены.")


if __name__ == "__main__":
    main()
