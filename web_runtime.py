from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog

from client_updates import apply_successful_update, fetch_files_manifest, inspect_installation
from launcher import (
    APP_NAME,
    CLIENT_FILES_MANIFEST_URL,
    DEFAULT_CLIENT_EXECUTABLE,
    DEFAULT_LAUNCH_MODE,
    HTTP,
    SERVER_HOST,
    SERVER_PORT,
    SERVER_IP_URL,
    ZIP_URL,
    build_direct_game_command,
    download_archive,
    download_client_folder,
    fetch_release_manifest,
    find_client,
    find_direct_game_client,
    game_root_for,
    load_server_address,
    parse_server_address,
    safe_extract_zip,
    sanitize_nickname,
    write_launcher_log,
)

DATA_HOME = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / "LEDERG CRMP"
WEB_ROOT = Path(os.environ.get("LEDERG_WEB_ROOT", Path(__file__).resolve().parent / "web"))
STATE_FILE = DATA_HOME / "install_location.txt"
NICK_FILE = DATA_HOME / "nickname.txt"
SERVER_FILE = DATA_HOME / "server_address.txt"


class LauncherApi:
    """Narrow JavaScript-to-native bridge. All game/file operations stay native."""

    def __init__(self) -> None:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._busy = False
        self._phase = "idle"
        self._status = "Проверяю папку клиента…"
        self._progress = 0.0
        self._progress_label = "ПРОВЕРКА"
        self._current_file = ""
        self._error = ""
        self._process: subprocess.Popen | None = None
        self._game_root: Path | None = None
        self._endpoint = f"{SERVER_HOST}:{SERVER_PORT}"
        self._nickname = self._read_text(NICK_FILE, "LEDERG")
        self._install_path = self._read_install_path()
        self._refresh_install_root()
        threading.Thread(target=self._refresh_server, daemon=True, name="LEDERG-server-config").start()
        threading.Thread(target=self._startup_check, daemon=True, name="LEDERG-client-check").start()

    @staticmethod
    def _read_text(path: Path, default: str) -> str:
        try:
            return path.read_text(encoding="utf-8-sig").strip() or default
        except OSError:
            return default

    def _read_install_path(self) -> Path | None:
        try:
            path = Path(STATE_FILE.read_text(encoding="utf-8-sig").strip())
            return path if path.is_dir() else None
        except (OSError, ValueError):
            return None

    def _write_install_path(self, path: Path) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(str(path), encoding="utf-8")
        with self._lock:
            self._install_path = path
        self._refresh_install_root()

    def _refresh_install_root(self) -> None:
        with self._lock:
            selected = self._install_path
        if selected is None or not selected.exists():
            with self._lock:
                self._game_root = None
            return
        root = self._find_game_root(selected)
        with self._lock:
            self._game_root = root

    @staticmethod
    def _find_game_root(selected: Path) -> Path | None:
        selected = selected.resolve()
        candidates = [selected, *selected.parents]
        nested = [
            selected / "GTA Criminal Russia",
            selected / "GTA Criminal Russia" / "GTA Criminal Russia",
        ]
        for candidate in nested:
            candidates.append(candidate)
        for candidate in candidates:
            if (candidate / "gta_sa.exe").is_file() and find_direct_game_client(candidate):
                return candidate
        # Folder dialog users often pick a parent folder. Search only to a small
        # depth and return the runtime's real game root, never the selected parent.
        try:
            for runtime in selected.glob("*/multiplayer_c/grand_theft_auto_san_andreas.dll"):
                root = runtime.parents[1]
                if (root / "gta_sa.exe").is_file():
                    return root
            for runtime in selected.glob("*/grand_theft_auto_san_andreas.dll"):
                root = runtime.parent
                if (root / "gta_sa.exe").is_file():
                    return root
        except OSError:
            pass
        return selected if (selected / "gta_sa.exe").is_file() else None

    def _refresh_server(self) -> None:
        try:
            host, port = load_server_address(SERVER_FILE)
            with self._lock:
                self._endpoint = f"{host}:{port}"
        except Exception as exc:
            write_launcher_log(f"Server config refresh failed: {exc!r}")

    def _startup_check(self) -> None:
        try:
            with self._lock:
                root = self._game_root
            if root and find_direct_game_client(root):
                with self._lock:
                    self._status = "Клиент готов к запуску."
                    self._progress = 100.0
                    self._progress_label = "КЛИЕНТ ПРОВЕРЕН"
                    self._phase = "ready"
            else:
                with self._lock:
                    self._status = "Выбери папку с игрой или скачай клиент."
                    self._phase = "idle"
        except Exception as exc:
            with self._lock:
                self._status = f"Проверка клиента: {exc}"
                self._phase = "error"

    def get_state(self) -> dict:
        with self._lock:
            root = self._game_root
            selected = self._install_path
            busy = self._busy
            phase = self._phase
            status = self._status
            progress = self._progress
            progress_label = self._progress_label
            current_file = self._current_file
            error = self._error
            endpoint = self._endpoint
            nickname = self._nickname
        runtime = find_direct_game_client(root) if root else None
        browser = find_client(root) if root else None
        client_ready = bool(root and (root / "gta_sa.exe").is_file() and runtime)
        display_path = str(root or selected) if (root or selected) else ""
        return {
            "client_ready": client_ready,
            "install_path": display_path,
            "endpoint": endpoint,
            "nickname": nickname,
            "busy": busy,
            "phase": phase,
            "status": error or status,
            "progress": progress,
            "progress_label": progress_label,
            "current_file": current_file,
            "browser_found": bool(browser),
        }

    def set_nickname(self, nickname: str) -> dict:
        clean = sanitize_nickname(nickname)
        with self._lock:
            self._nickname = clean
        try:
            NICK_FILE.write_text(clean, encoding="utf-8")
        except OSError as exc:
            write_launcher_log(f"Could not persist nickname: {exc!r}")
        return {"ok": True, "nickname": clean}

    def choose_folder(self) -> dict:
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                initial = str(self._install_path or Path.home() / "Documents")
                selected = filedialog.askdirectory(
                    parent=root,
                    title="Выбери папку игры или место установки LEDERG CR:MP",
                    initialdir=initial if Path(initial).is_dir() else str(Path.home()),
                    mustexist=True,
                )
            finally:
                root.destroy()
        except Exception as exc:
            return {"ok": False, "message": f"Не удалось открыть выбор папки: {exc}", "state": self.get_state()}
        if not selected:
            return {"ok": False, "message": "Выбор папки отменён.", "state": self.get_state()}
        try:
            path = Path(selected).resolve()
            self._write_install_path(path)
            state = self.get_state()
            if not state["client_ready"]:
                with self._lock:
                    self._status = "Папка выбрана. Нажми «Скачать / восстановить файлы»."
                    self._phase = "idle"
                state = self.get_state()
            return {"ok": True, "message": "Папка установки сохранена.", "state": state}
        except Exception as exc:
            return {"ok": False, "message": f"Путь установки не сохранён: {exc}", "state": self.get_state()}

    def open_game_folder(self) -> dict:
        with self._lock:
            root = self._game_root or self._install_path
        if not root or not root.is_dir():
            return {"ok": False, "message": "Сначала выбери папку клиента."}
        try:
            os.startfile(str(root))
            return {"ok": True, "message": "Открыл папку игры."}
        except Exception as exc:
            return {"ok": False, "message": f"Не удалось открыть папку: {exc}"}

    def launch_game(self, nickname: str) -> dict:
        self.set_nickname(nickname)
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "Дождись завершения текущей операции."}
            root = self._game_root
            endpoint = self._endpoint
        if not root:
            return {"ok": False, "message": "Не найдена папка игры. Выбери папку или скачай клиент."}
        runtime = find_direct_game_client(root)
        if not (root / "gta_sa.exe").is_file() or not runtime:
            return {
                "ok": False,
                "message": "Не найден CR:MP runtime. Нажми «Скачать / восстановить файлы» для проверки клиента.",
            }
        try:
            host, port = parse_server_address(endpoint)
            command = build_direct_game_command(runtime, host, port, self._nickname)
            write_launcher_log(
                f"Native runtime launch: executable={runtime}; cwd={root}; "
                f"endpoint={host}:{port}; nickname={self._nickname}"
            )
            process = subprocess.Popen(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            with self._lock:
                self._process = process
                self._status = f"Игровой клиент запущен. PID {process.pid}."
                self._phase = "ready"
                self._error = ""
            write_launcher_log(f"Native runtime process started: pid={process.pid}")
            threading.Thread(target=self._monitor_process, args=(process,), daemon=True).start()
            return {"ok": True, "message": f"Клиент запущен; подключаю к {host}:{port}."}
        except Exception as exc:
            write_launcher_log(f"ERROR launching game: {traceback.format_exc()}")
            with self._lock:
                self._phase = "error"
                self._error = str(exc)
                self._status = "Ошибка запуска клиента."
            return {"ok": False, "message": f"Не удалось запустить CR:MP: {exc}"}

    def _monitor_process(self, process: subprocess.Popen) -> None:
        try:
            code = process.wait()
            with self._lock:
                if self._process is process:
                    self._process = None
                    if code == 0:
                        self._status = "Игровой клиент закрылся."
                        self._phase = "ready" if self.get_state()["client_ready"] else "idle"
                    else:
                        self._status = f"Клиент завершился с кодом {code}. Смотри журнал."
                        self._phase = "error"
                        self._error = self._status
            write_launcher_log(f"Native runtime exited: code={code}")
        except Exception as exc:
            write_launcher_log(f"Process monitor error: {exc!r}")

    def start_install(self) -> dict:
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "Установка уже выполняется."}
            selected = self._install_path
            if selected is None:
                return {"ok": False, "message": "Сначала нажми «Выбрать» и укажи папку установки."}
            self._busy = True
            self._phase = "manifest"
            self._status = "Получаю проверенный манифест клиента…"
            self._progress = 0.0
            self._progress_label = "ПОДГОТОВКА"
            self._error = ""
        threading.Thread(target=self._install_worker, args=(selected,), daemon=True, name="LEDERG-install").start()
        return {"ok": True, "message": "Проверка и установка клиента запущены."}

    def _set_progress(self, phase: str, done: int, total: int, label: str) -> None:
        with self._lock:
            self._phase = phase
            self._status = label or "Обрабатываю файлы клиента…"
            self._current_file = label or ""
            self._progress = (min(100.0, max(0.0, float(done) * 100.0 / float(total))) if total else 0.0)
            if phase == "download":
                self._progress_label = "ЗАГРУЗКА ФАЙЛОВ"
            elif phase == "extract":
                self._progress_label = "РАСПАКОВКА АРХИВА"
            else:
                self._progress_label = "ПРОВЕРКА ФАЙЛОВ"

    def _install_worker(self, selected: Path) -> None:
        archive: Path | None = None
        try:
            with self._lock:
                self._status = "Обновляю конфигурацию сервера…"
                self._phase = "manifest"
            try:
                release = fetch_release_manifest()
            except Exception as exc:
                write_launcher_log(f"Release manifest unavailable; using last-resort client URLs: {exc!r}")
                release = {
                    "version": "fallback",
                    "archive_url": ZIP_URL,
                    "files_manifest_url": CLIENT_FILES_MANIFEST_URL,
                    "archive_size": 0,
                    "sha256": "",
                    "client_executable": DEFAULT_CLIENT_EXECUTABLE,
                    "launch_mode": DEFAULT_LAUNCH_MODE,
                }

            manifest_url = release.get("files_manifest_url") or CLIENT_FILES_MANIFEST_URL
            manifest = None
            manifest_error = None
            try:
                manifest = fetch_files_manifest(manifest_url, http=HTTP)
            except Exception as exc:
                manifest_error = exc
                write_launcher_log(f"Client file manifest unavailable; trying verified ZIP fallback: {exc!r}")

            # The ZIP route is only a fallback for an unavailable manifest. Once
            # a trusted manifest is fetched, its per-file hashes and resumable
            # updates remain the source of truth.
            if manifest is None:
                install_name = "GTA Criminal Russia"
                selected_is_game_root = (selected / "gta_sa.exe").is_file()
                destination = selected.parent if selected_is_game_root or selected.name.casefold() == install_name.casefold() else selected
                archive_url = release.get("archive_url") or ZIP_URL
                with self._lock:
                    self._status = "Манифест недоступен. Скачиваю резервный ZIP-архив…"
                    self._phase = "download"
                    self._progress = 0.0
                    self._progress_label = "ЗАГРУЗКА АРХИВА"
                archive = download_archive(
                    destination,
                    progress=self._set_progress,
                    url=archive_url,
                    expected_sha256=release.get("sha256", ""),
                    expected_size=int(release.get("archive_size", 0) or 0),
                )
                count = safe_extract_zip(
                    archive,
                    destination,
                    progress=lambda done, total, name: self._set_progress("extract", done, total, name),
                )
                client = find_client(destination, release.get("client_executable"))
                if not client:
                    raise RuntimeError(
                        "Архив распакован, но браузер CR:MP не найден. "
                        "Проверь, что ZIP содержит папку multiplayer_c и игровой клиент."
                    )
                root = game_root_for(client)
                runtime = find_direct_game_client(root)
                if not (root / "gta_sa.exe").is_file() or not runtime:
                    raise RuntimeError(
                        "В архиве не найден игровой runtime grand_theft_auto_san_andreas.dll. "
                        "Нужен полный CR:MP-клиент, не только браузер серверов."
                    )
                try:
                    archive.unlink(missing_ok=True)
                except OSError:
                    pass
                self._write_install_path(root)
                with self._lock:
                    self._game_root = root
                    self._install_path = root
                    self._progress = 100.0
                    self._progress_label = "ГОТОВО"
                    self._status = f"Клиент проверен через резервный ZIP ({count} файлов)."
                    self._phase = "ready"
                    self._error = ""
                write_launcher_log(f"Client ZIP fallback completed: root={root}; files={count}")
                return

            install_name = str(manifest.get("install_root", "GTA Criminal Russia"))
            if (selected / "gta_sa.exe").is_file() or selected.name.casefold() == install_name.casefold():
                destination = selected.parent
            else:
                destination = selected
            with self._lock:
                self._status = "Скачиваю и проверяю игровые файлы…"
                self._phase = "download"
            _install_root, count = download_client_folder(
                destination,
                manifest_url,
                progress=self._set_progress,
                http=HTTP,
                manifest_payload=manifest,
            )
            new_install_root = destination / manifest["install_root"]
            if not new_install_root.is_dir():
                client = find_client(destination, release.get("client_executable"))
                new_install_root = game_root_for(client) if client else new_install_root
            client = find_client(new_install_root, release.get("client_executable"))
            if not client:
                raise RuntimeError(
                    "Файлы обновлены, но клиент не найден. Проверь манифест и повтори восстановление."
                )
            root = game_root_for(client)
            runtime = find_direct_game_client(root)
            if not (root / "gta_sa.exe").is_file() or not runtime:
                raise RuntimeError(
                    "Не найден игровой runtime grand_theft_auto_san_andreas.dll. "
                    "Манифест может описывать не полный клиент."
                )
            apply_successful_update(root, manifest)
            self._write_install_path(root)
            with self._lock:
                self._game_root = root
                self._install_path = root
                self._progress = 100.0
                self._progress_label = "ГОТОВО"
                self._status = f"Клиент {manifest.get('version', release.get('version', 'unknown'))} проверен."
                self._phase = "ready"
                self._error = ""
            write_launcher_log(f"Client update completed: root={root}; files={count}")
        except Exception as exc:
            write_launcher_log(f"ERROR updating client: {traceback.format_exc()}")
            with self._lock:
                self._phase = "error"
                self._error = str(exc)
                self._status = f"Ошибка обновления: {exc}"
        finally:
            with self._lock:
                self._busy = False


def start() -> None:
    import webview

    DATA_HOME.mkdir(parents=True, exist_ok=True)
    api = LauncherApi()
    page = WEB_ROOT / "index.html"
    if not page.is_file():
        raise FileNotFoundError(f"Не найден HTML интерфейса лаунчера: {page}")
    webview.create_window(
        APP_NAME,
        url=page.resolve().as_uri(),
        js_api=api,
        width=1160,
        height=760,
        min_size=(900, 620),
        background_color="#080808",
        text_select=True,
    )
    webview.start(debug=False)
