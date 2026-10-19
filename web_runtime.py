from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from ctypes import wintypes
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
    query_samp_server,
    format_client_exit_code,
)

DATA_HOME = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / "LEDERG CRMP"
WEB_ROOT = Path(os.environ.get("LEDERG_WEB_ROOT", Path(__file__).resolve().parent / "web"))
STATE_FILE = DATA_HOME / "install_location.txt"
NICK_FILE = DATA_HOME / "nickname.txt"

SERVER_FILE = DATA_HOME / "server_address.txt"


def parse_windows_crash_event_payload(
    payload_text: str, executable_names: list[str] | tuple[str, ...]
) -> list[str]:
    """Return Windows Application Error events that mention one of our game binaries."""
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(payload, dict):
        events = [payload]
    elif isinstance(payload, list):
        events = payload
    else:
        return []

    names = {
        str(name).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        for name in executable_names
        if str(name).strip()
    }
    if not names:
        return []

    matches = []
    for event in events:
        if not isinstance(event, dict):
            continue
        message = str(event.get("Message") or "")
        folded_message = message.casefold()
        if not message or not any(name in folded_message for name in names):
            continue
        timestamp = str(event.get("TimeCreated") or "unknown time")
        event_id = str(event.get("Id") if event.get("Id") is not None else "?")
        provider = str(event.get("ProviderName") or "unknown provider")
        matches.append(
            f"Windows Application event: time={timestamp}; id={event_id}; "
            f"provider={provider}\n{message}"
        )
    return matches[:10]


def collect_windows_crash_events(
    executable_names: list[str] | tuple[str, ...],
    *,
    started_at: float | None = None,
    runner=None,
    sleeper=None,
    attempts: int = 3,
) -> list[str]:
    """Query recent Application Error/WER records after a game crash, without elevation."""
    if os.name != "nt":
        return []
    normalized_names = [
        str(name).replace("\\", "/").rsplit("/", 1)[-1]
        for name in executable_names
        if str(name).strip()
    ]
    if not normalized_names:
        return []

    runner = runner or subprocess.run
    sleeper = sleeper or time.sleep
    launch_epoch = float(started_at if started_at is not None else time.time() - 600.0)
    from_epoch_ms = max(0, int((launch_epoch - 3.0) * 1000))
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$from=[DateTimeOffset]::FromUnixTimeMilliseconds({from_epoch_ms}).LocalDateTime; "
        "$events = @(Get-WinEvent -FilterHashtable "
        "@{LogName='Application'; Id=1000,1001; StartTime=$from} "
        "-MaxEvents 50 -ErrorAction SilentlyContinue | "
        "Select-Object @{Name='TimeCreated';Expression={$_.TimeCreated.ToString('o')}}, "
        "Id, ProviderName, Message); "
        "if ($events.Count -gt 0) { $events | ConvertTo-Json -Depth 3 -Compress }"
    )
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    for attempt in range(max(1, int(attempts))):
        try:
            result = runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                matches = parse_windows_crash_event_payload(
                    result.stdout or "", normalized_names
                )
                if matches:
                    return matches
            elif getattr(result, "stderr", ""):
                write_launcher_log(
                    "Windows crash-event query returned an error: "
                    + str(result.stderr).strip()[:1200]
                )
        except Exception as exc:
            write_launcher_log(f"Could not query Windows Application crash events: {exc!r}")
        if attempt + 1 < max(1, int(attempts)):
            sleeper(0.9)
    return []

VK_X = 0x58
VK_T = 0x54
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
X_PANEL_COMMAND = "/xpanel"


def should_trigger_xpanel_hotkey(
    *, key_down: bool, was_down: bool, foreground_pid: int, game_pid: int | None
) -> bool:
    """Trigger only on a fresh X press while the exact launched game is foreground."""
    return bool(
        key_down
        and not was_down
        and game_pid
        and foreground_pid
        and foreground_pid == game_pid
    )


def _foreground_process_id() -> tuple[int, int]:
    """Return (foreground HWND, owning PID), safely returning zero on failure."""
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return 0, 0
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return hwnd, int(pid.value)
    except Exception:
        return 0, 0


def is_game_window_process(image_path: str, game_root: Path) -> bool:
    """Accept only the two expected game executables inside the selected client root."""
    if not image_path:
        return False
    try:
        root = os.path.normcase(os.path.abspath(os.fspath(game_root)))
        image = os.path.normcase(os.path.abspath(image_path))
        if os.path.commonpath((root, image)) != root:
            return False
        return os.path.basename(image).lower() in {
            "gta_sa.exe",
            "grand_theft_auto_san_andreas.dll",
        }
    except (OSError, ValueError):
        return False


def _process_image_path(process_id: int) -> str:
    """Read an image path without importing psutil or querying unrelated processes."""
    if not process_id:
        return ""
    kernel32 = ctypes.windll.kernel32
    process = kernel32.OpenProcess(0x1000, False, int(process_id))  # QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(capacity)
        ):
            return ""
        return buffer.value
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(process)


def _foreground_game_process_id(game_root: Path, launched_pid: int) -> int:
    """Return the game PID only for the launcher process or its known client executable."""
    _, foreground_pid = _foreground_process_id()
    if not foreground_pid:
        return 0
    if foreground_pid == launched_pid:
        return foreground_pid
    image_path = _process_image_path(foreground_pid)
    return foreground_pid if is_game_window_process(image_path, game_root) else 0


def _send_vk(user32, virtual_key: int, *, key_up: bool = False) -> None:
    flags = KEYEVENTF_KEYUP if key_up else 0
    user32.keybd_event(virtual_key, 0, flags, 0)


def _send_layout_character(user32, character: str, keyboard_layout) -> bool:
    """Type one ASCII command character using the game's active keyboard layout."""
    mapped = int(user32.VkKeyScanExW(character, keyboard_layout))
    if mapped == -1:
        return False
    virtual_key = mapped & 0xFF
    modifiers = (mapped >> 8) & 0xFF
    modifier_keys = []
    if modifiers & 1:
        modifier_keys.append(VK_SHIFT)
    if modifiers & 2:
        modifier_keys.append(VK_CONTROL)
    if modifiers & 4:
        modifier_keys.append(VK_MENU)
    for modifier in modifier_keys:
        _send_vk(user32, modifier)
    _send_vk(user32, virtual_key)
    _send_vk(user32, virtual_key, key_up=True)
    for modifier in reversed(modifier_keys):
        _send_vk(user32, modifier, key_up=True)
    return True


def _type_xpanel_command(game_pid: int) -> bool:
    """Open SA:MP chat and send /xpanel only while our game remains foreground."""
    hwnd, foreground_pid = _foreground_process_id()
    if not hwnd or foreground_pid != game_pid:
        return False
    user32 = ctypes.windll.user32
    thread_id = int(user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None) or 0)
    keyboard_layout = user32.GetKeyboardLayout(thread_id)
    # T is a game action key. Allow the game a moment to open its chat input.
    _send_vk(user32, VK_T)
    _send_vk(user32, VK_T, key_up=True)
    time.sleep(0.14)
    if _foreground_process_id()[1] != game_pid:
        return False
    for character in X_PANEL_COMMAND:
        if not _send_layout_character(user32, character, keyboard_layout):
            write_launcher_log(
                f"X hotkey could not map character {character!r} under the game keyboard layout."
            )
            return False
        time.sleep(0.018)
    _send_vk(user32, VK_RETURN)
    _send_vk(user32, VK_RETURN, key_up=True)
    return True


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
        self._xpanel_stop: threading.Event | None = None
        self._game_root: Path | None = None
        self._endpoint = f"{SERVER_HOST}:{SERVER_PORT}"
        self._server_online: bool | None = None
        self._server_ping_ms: int | None = None
        self._server_method = "none"
        self._server_detail = "Проверяю доступность сервера…"
        self._server_checked_at = ""
        self._last_exit_code: int | None = None
        self._nickname = self._read_text(NICK_FILE, "LEDERG")
        self._install_path = self._read_install_path()
        self._refresh_install_root()
        threading.Thread(target=self._server_monitor, daemon=True, name="LEDERG-server-monitor").start()
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
            if path.is_dir():
                return path
        except (OSError, ValueError):
            pass
        # One-click installation works on a fresh machine; users can still choose
        # another drive before downloading the multi-gigabyte client.
        return Path.home() / "Games"

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
        candidates.extend([
            selected / "GTA Criminal Russia",
            selected / "GTA Criminal Russia" / "GTA Criminal Russia",
        ])
        for candidate in candidates:
            if (candidate / "gta_sa.exe").is_file() and find_direct_game_client(candidate):
                return candidate
        # The folder picker can point at a parent folder. Locate the actual game
        # root by its game executable and the DLL the direct-connect helper loads.
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
        return selected if (
            (selected / "gta_sa.exe").is_file()
            and find_direct_game_client(selected)
        ) else None

    def _refresh_server(self) -> None:
        try:
            host, port = load_server_address(SERVER_FILE)
            endpoint = f"{host}:{port}"
            with self._lock:
                previous = self._endpoint
                self._endpoint = endpoint
            if endpoint != previous:
                write_launcher_log(f"Server endpoint refreshed from GitHub config: {endpoint}")
        except Exception as exc:
            write_launcher_log(f"Server config refresh failed: {exc!r}")

    def _server_monitor(self) -> None:
        """Refresh the public endpoint and poll the SA:MP query protocol in background."""
        next_config_refresh = 0.0
        last_online: bool | None = None
        while True:
            now = time.monotonic()
            if now >= next_config_refresh:
                self._refresh_server()
                next_config_refresh = now + 60.0
            with self._lock:
                endpoint = self._endpoint
            try:
                host, port = parse_server_address(endpoint)
                result = query_samp_server(host, port, timeout=0.8)
            except Exception as exc:
                result = {
                    "online": False,
                    "ping_ms": None,
                    "method": "none",
                    "detail": f"Не удалось проверить сервер: {exc}",
                }
            online = bool(result.get("online"))
            checked_at = time.strftime("%H:%M:%S")
            with self._lock:
                self._server_online = online
                self._server_ping_ms = result.get("ping_ms") if online else None
                self._server_method = str(result.get("method") or "none")
                self._server_detail = str(result.get("detail") or "")
                self._server_checked_at = checked_at
            if last_online is None or online != last_online:
                write_launcher_log(
                    f"Server query status changed: endpoint={endpoint}; online={online}; "
                    f"ping_ms={result.get('ping_ms')}; method={result.get('method')}; "
                    f"detail={result.get('detail')!r}"
                )
                last_online = online
            time.sleep(12.0)

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
            server_online = self._server_online
            server_ping_ms = self._server_ping_ms
            server_method = self._server_method
            server_detail = self._server_detail
            server_checked_at = self._server_checked_at
            last_exit_code = self._last_exit_code
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
            "server_online": server_online,
            "server_ping_ms": server_ping_ms,
            "server_method": server_method,
            "server_detail": server_detail,
            "server_checked_at": server_checked_at,
            "last_exit_code": last_exit_code,
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

    def open_log(self) -> dict:
        """Open the launcher diagnostics so users can share the exact crash context."""
        path = DATA_HOME / "launcher.log"
        try:
            DATA_HOME.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                write_launcher_log("Launcher log opened before any diagnostics were recorded.")
            os.startfile(str(path))
            return {"ok": True, "message": "Открыл журнал LEDERG CR:MP."}
        except Exception as exc:
            write_launcher_log(f"Could not open launcher log: {exc!r}")
            return {"ok": False, "message": f"Не удалось открыть журнал: {exc}"}

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
        """Launch the CR:MP runtime itself; do not inject an adaptation DLL into vanilla GTA:SA."""
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
                "message": (
                    "Не найден CR:MP runtime grand_theft_auto_san_andreas.dll. "
                    "Нажми «Скачать / восстановить файлы» для проверки клиента."
                ),
            }
        try:
            host, port = parse_server_address(endpoint)
            command = build_direct_game_command(runtime, host, port, self._nickname)
            # The native CR:MP browser lives in multiplayer_c and starts this
            # client runtime directly. Launching gta_sa.exe and injecting
            # san_andreas_multiplayer.dll produces the wrong adaptation context.
            working_directory = runtime.parent
            try:
                runtime_stat = runtime.stat()
                runtime_details = (
                    f"size={runtime_stat.st_size}; modified={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(runtime_stat.st_mtime))}"
                )
            except OSError as stat_error:
                runtime_details = f"stat_error={stat_error!r}"
            write_launcher_log(
                f"Native CR:MP runtime launch: executable={runtime}; {runtime_details}; "
                f"cwd={working_directory}; endpoint={host}:{port}; nickname={self._nickname}; "
                f"command={subprocess.list2cmdline(command)!r}"
            )
            launch_started_at = time.time()
            process = subprocess.Popen(
                command,
                cwd=str(working_directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            with self._lock:
                self._process = process
                self._status = f"CR:MP runtime запущен. PID {process.pid}; сервер {host}:{port}."
                self._phase = "ready"
                self._error = ""
            write_launcher_log(f"Native CR:MP runtime process started: pid={process.pid}")
            self._start_xpanel_hotkey(process, root)
            threading.Thread(
                target=self._monitor_process,
                args=(process, launch_started_at),
                daemon=True,
                name="LEDERG-game-process-monitor",
            ).start()
            return {"ok": True, "message": f"Запускаю CR:MP и подключаю к {host}:{port}."}
        except Exception as exc:
            write_launcher_log(f"ERROR launching native CR:MP runtime: {traceback.format_exc()}")
            with self._lock:
                self._phase = "error"
                self._error = str(exc)
                self._status = "Ошибка запуска CR:MP."
            return {"ok": False, "message": f"Не удалось запустить CR:MP: {exc}"}

    def _start_xpanel_hotkey(self, process: subprocess.Popen, root: Path) -> None:
        """Start an X-key watcher tied to this game process; never global-type elsewhere."""
        previous = self._xpanel_stop
        if previous is not None:
            previous.set()
        stop_event = threading.Event()
        self._xpanel_stop = stop_event
        threading.Thread(
            target=self._watch_xpanel_hotkey,
            args=(process, root, stop_event),
            daemon=True,
            name="LEDERG-X-panel-hotkey",
        ).start()
        write_launcher_log(
            f"X hotkey watcher started for CR:MP PID {process.pid}; "
            "only sends /xpanel when that exact game process is foreground."
        )

    def _watch_xpanel_hotkey(
        self, process: subprocess.Popen, root: Path, stop_event: threading.Event
    ) -> None:
        was_down = False
        try:
            user32 = ctypes.windll.user32
            while not stop_event.wait(0.025):
                if process.poll() is not None:
                    break
                key_down = bool(int(user32.GetAsyncKeyState(VK_X)) & 0x8000)
                foreground_game_pid = _foreground_game_process_id(root, process.pid)
                if should_trigger_xpanel_hotkey(
                    key_down=key_down,
                    was_down=was_down,
                    foreground_pid=foreground_game_pid,
                    game_pid=foreground_game_pid,
                ):
                    write_launcher_log(
                        f"Physical X pressed in foreground CR:MP window (pid={foreground_game_pid}); "
                        f"launched runtime pid={process.pid}; sending /xpanel."
                    )
                    if not _type_xpanel_command(foreground_game_pid):
                        write_launcher_log(
                            f"Could not send /xpanel for game pid {process.pid}; "
                            "use the /xpanel chat command as a fallback."
                        )
                was_down = key_down
        except Exception:
            write_launcher_log("X hotkey watcher failed: " + traceback.format_exc())
        finally:
            if self._xpanel_stop is stop_event:
                self._xpanel_stop = None
            write_launcher_log(f"X hotkey watcher stopped for CR:MP PID {process.pid}.")

    def _monitor_process(
        self, process: subprocess.Popen, started_at: float | None = None
    ) -> None:
        try:
            code = process.wait()
            if self._xpanel_stop is not None and self._process is process:
                self._xpanel_stop.set()
            code_hex = f"0x{(int(code) & 0xFFFFFFFF):08X}"
            with self._lock:
                self._last_exit_code = int(code)
                if self._process is process:
                    self._process = None
                    if code == 0:
                        self._status = "Игровой клиент закрылся."
                        self._phase = "ready" if self.get_state()["client_ready"] else "idle"
                    else:
                        self._status = format_client_exit_code(code)
                        self._phase = "error"
                        self._error = self._status
            write_launcher_log(
                f"Native runtime exited: pid={process.pid}; code={code}; hex={code_hex}; "
                f"command={process.args!r}"
            )

            if (int(code) & 0xFFFFFFFF) == 0xC0000005:
                command = process.args
                command_items = [command] if isinstance(command, str) else list(command or [])
                executable_names = [
                    str(item).replace("\\", "/").rsplit("/", 1)[-1]
                    for item in command_items
                    if str(item).strip()
                ]
                executable_names.extend([
                    "LEDERG-DirectLaunch.exe",
                    "multiplayer_browser.cr.exe",
                    "gta_sa.exe",
                    "grand_theft_auto_san_andreas.dll",
                ])
                events = collect_windows_crash_events(
                    executable_names, started_at=started_at
                )
                if events:
                    for event in events:
                        write_launcher_log(event)
                    with self._lock:
                        original = self._error or self._status
                        self._error = original.replace(
                            "Если повторится, нужен журнал Windows «Приложение» "
                            "с именем сбойного модуля.",
                            "Событие Windows записано в «ЖУРНАЛ» лаунчера.",
                        )
                        self._status = self._error
                    write_launcher_log(
                        "Windows crash-event diagnostics collected automatically; "
                        "see launcher.log for the faulting application/module."
                    )
                else:
                    write_launcher_log(
                        "No matching Windows Application Error/WER event was found automatically; "
                        "check Windows Event Viewer > Windows Logs > Application."
                    )
        except Exception as exc:
            write_launcher_log(f"Process monitor error: {exc!r}")

    def start_install(self) -> dict:
        return self._begin_install(reinstall=False)

    def reinstall_client(self) -> dict:
        """Re-download the official Drive archive and restore files over the installation."""
        return self._begin_install(reinstall=True)

    def _begin_install(self, *, reinstall: bool) -> dict:
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "Установка уже выполняется."}
            if self._process is not None and self._process.poll() is None:
                return {
                    "ok": False,
                    "message": "Сначала полностью закрой CR:MP, затем запускай восстановление.",
                }
            selected = (self._game_root or self._install_path) if reinstall else self._install_path
            if selected is None:
                return {"ok": False, "message": "Сначала выбери папку установки."}
            self._busy = True
            self._phase = "manifest"
            self._status = (
                "Готовлю переустановку и проверку файлов…"
                if reinstall else "Готовлю установку игрового клиента…"
            )
            self._progress = 0.0
            self._progress_label = "ПЕРЕУСТАНОВКА" if reinstall else "ПОДГОТОВКА"
            self._error = ""
        write_launcher_log(
            f"Client {'reinstall/repair' if reinstall else 'install'} requested: selected={selected}"
        )
        threading.Thread(
            target=self._install_worker,
            args=(selected, reinstall),
            daemon=True,
            name="LEDERG-reinstall" if reinstall else "LEDERG-install",
        ).start()
        return {
            "ok": True,
            "message": "Переустановка и восстановление файлов запущены."
            if reinstall else "Установка клиента запущена.",
        }

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

    def _install_worker(self, selected: Path, reinstall: bool = False) -> None:
        archive: Path | None = None
        try:
            with self._lock:
                self._status = (
                    "Загружаю архив заново для восстановления клиента…"
                    if reinstall else "Обновляю конфигурацию сервера…"
                )
                self._phase = "manifest"
            if reinstall:
                write_launcher_log(
                    f"Reinstall mode active: existing game files will be overwritten from Drive; root={selected}"
                )
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

            # The full client payload is intentionally sourced only from the
            # pinned Google Drive archive. Do not contact Cloudpub or a per-file manifest.
            manifest_url = ""
            manifest = None
            write_launcher_log(
                f"Drive-only client source selected: {ZIP_URL}; "
                f"release metadata version={release.get('version', 'unknown')}."
            )
            if manifest is None:
                install_name = "GTA Criminal Russia"
                selected_is_game_root = (selected / "gta_sa.exe").is_file()
                destination = selected.parent if selected_is_game_root or selected.name.casefold() == install_name.casefold() else selected
                archive_url = release.get("archive_url") or ZIP_URL
                with self._lock:
                    self._status = "Скачиваю игровой клиент из Google Drive…"
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
                    self._status = f"Клиент установлен из Google Drive ({count} файлов)."
                    self._phase = "ready"
                    self._error = ""
                write_launcher_log(f"Google Drive client installation completed: root={root}; files={count}")
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


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _fetch_live_web_page() -> tuple[bytes, str]:
    """Fetch the GitHub-published cover and verify it against the signed-by-HTTPS manifest."""
    base = "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-luncher-crmp/main/"
    request = urllib.request.Request(
        base + "runtime-manifest.json",
        headers={"User-Agent": "LEDERG-CRMP-Launcher/2"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    if payload.get("schema") != 1 or not isinstance(payload.get("files"), list):
        raise ValueError("Invalid live UI manifest")
    entry = next(
        (item for item in payload["files"] if item.get("path") == "web/index.html"),
        None,
    )
    if not isinstance(entry, dict):
        raise ValueError("Live UI is missing from the manifest")
    expected_size = entry.get("size")
    expected_hash = entry.get("sha256")
    if not isinstance(expected_size, int) or not 0 <= expected_size <= 20 * 1024 * 1024:
        raise ValueError("Invalid live UI size")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("Invalid live UI SHA-256")
    request = urllib.request.Request(
        base + "web/index.html",
        headers={"User-Agent": "LEDERG-CRMP-Launcher/2"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) != expected_size or _sha256_bytes(data) != expected_hash:
        raise ValueError("Live UI failed size/SHA-256 verification")
    return data, expected_hash


def _live_ui_updater(window, initial_page: Path) -> None:
    """Poll GitHub while the launcher is open and hot-reload only a verified HTML cover."""
    current_hash = ""
    try:
        current_hash = _sha256_bytes(initial_page.read_bytes())
    except OSError:
        pass
    live_root = DATA_HOME / "live-web"
    while True:
        time.sleep(60)
        try:
            data, digest = _fetch_live_web_page()
            if digest == current_hash:
                continue
            version_dir = live_root / digest
            version_dir.mkdir(parents=True, exist_ok=True)
            page = version_dir / "index.html"
            if not page.is_file() or _sha256_bytes(page.read_bytes()) != digest:
                temporary = version_dir / "index.html.tmp"
                temporary.write_bytes(data)
                temporary.replace(page)
            # Reload only after the replacement has passed integrity checks.
            window.load_url(page.resolve().as_uri())
            current_hash = digest
            write_launcher_log(f"Live launcher cover updated from GitHub: sha256={digest}")
        except Exception as exc:
            write_launcher_log(f"Live cover check failed; keeping current UI: {exc!r}")


def start() -> None:
    import webview

    DATA_HOME.mkdir(parents=True, exist_ok=True)
    api = LauncherApi()
    page = WEB_ROOT / "index.html"
    if not page.is_file():
        raise FileNotFoundError(f"Не найден HTML интерфейса лаунчера: {page}")
    window = webview.create_window(
        APP_NAME,
        url=page.resolve().as_uri(),
        js_api=api,
        width=1160,
        height=760,
        min_size=(900, 620),
        background_color="#080808",
        text_select=True,
    )
    threading.Thread(
        target=_live_ui_updater,
        args=(window, page),
        daemon=True,
        name="LEDERG-live-cover-updater",
    ).start()
    webview.start(debug=False)
