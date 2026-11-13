"""Desktop application.

How professionals ship this kind of tool: a real window with its own chrome, a
tray icon, an app that keeps working when the window is closed, configuration on
disk, and one distributable binary. Discord, Slack, VS Code and Notion are all a
native window over web technology — that is the professional pattern, not a
compromise, and it is what this is.

What it adds over `tgfarm serve`:

* a native window with a menu, instead of a browser tab;
* close-to-tray, so shutting the window does not stop publishing;
* the scheduler owned by this process, started with the window;
* a stable per-install port on loopback, so OAuth callback URLs survive restarts;
* a token handed to the page in-process, never written to disk by the app.

The heavy dependencies (pywebview, pystray) are imported lazily so the rest of
the farm — and the test suite — runs without a display.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from tgfarm.core.apiauth import ensure_token
from tgfarm.core.logging import get_logger, setup_logging
from tgfarm.core.settings import get_settings
from tgfarm.db.bootstrap import ensure_bootstrapped

log = get_logger(__name__)

APP_NAME = "TgFarm"
WINDOW_TITLE = "TgFarm — автономная ферма Telegram-каналов"


# --------------------------------------------------------------------- single instance
class SingleInstance:
    """Hold an OS-level lock so two starts cannot pass the same check.

    The lock file is intentionally persistent. The kernel releases the advisory
    lock on normal exit or crash; a PID in the file is diagnostic only, never the
    synchronization primitive.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False
        self._lock_handle = None

    def _running_pid(self) -> int | None:
        if not self.path.exists():
            return None
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return None
        if pid <= 0:
            return None
        return pid if _process_alive(pid) else None

    def acquire(self) -> bool:
        if self._held:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                existing = self._running_pid()
                if existing is not None:
                    log.warning("another TgFarm instance is already running (pid %d)", existing)
                else:
                    log.warning("another TgFarm instance currently holds the desktop lock")
                handle.close()
                return False

            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
            self._lock_handle = handle
            self._held = True
            return True
        except BaseException:
            handle.close()
            raise

    def release(self) -> None:
        handle = self._lock_handle
        if not self._held or handle is None:
            return
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(b"0")
            handle.flush()
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lock_handle = None
            self._held = False


def _process_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        code = ctypes.c_ulong()
        alive = bool(ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code)))
        ctypes.windll.kernel32.CloseHandle(process)
        # STILL_ACTIVE
        return alive and code.value == 259
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------------- the server
def free_port() -> int:
    """Ask the OS for a port nobody is using, then let it go."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_available(port: int) -> bool:
    """Return whether the loopback port can be bound right now."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False


def _store_desktop_port(data_root: Path, port: int) -> None:
    """Persist the actual listener port with a unique atomic temporary file."""
    import tempfile

    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "desktop.port"
    fd, temp_name = tempfile.mkstemp(prefix=".desktop.port.", suffix=".tmp", dir=root)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(str(int(port)))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def stable_desktop_port(data_root: Path) -> int:
    """Reuse the per-install callback port, even if another process occupies it.

    Availability is enforced at bind time. Replacing a registered redirect port
    automatically would make the OAuth provider reject the callback.
    """
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "desktop.port"
    try:
        saved = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        saved = 0
    if 1024 < saved < 65536:
        return saved
    selected = free_port()
    _store_desktop_port(root, selected)
    return selected


def _bind_desktop_socket(data_root: Path, preferred_port: int | None) -> socket.socket:
    """Reserve the exact configured callback port before starting Uvicorn."""
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)

    def bind(port: int) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", int(port)))
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            return listener
        except OSError:
            listener.close()
            raise

    try:
        listener = bind(int(preferred_port)) if preferred_port else bind(0)
    except OSError as exc:
        log.error("configured Desktop OAuth callback port %s is unavailable", preferred_port)
        raise RuntimeError(
            f"Порт OAuth callback TgFarm {preferred_port} занят. "
            "Закрой другое приложение, использующее этот порт, и перезапусти TgFarm. "
            "Порт не будет изменён автоматически, чтобы не сломать Redirect URI у OAuth-провайдеров."
        ) from exc

    actual_port = int(listener.getsockname()[1])
    _store_desktop_port(root, actual_port)
    return listener


@dataclass
class RunningServer:
    port: int
    token: str
    thread: threading.Thread

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


def start_server(port: int | None = None, with_scheduler: bool = True) -> RunningServer:
    """Run the admin API on a loopback port in a background thread."""
    import uvicorn

    from tgfarm.api import create_app

    data_root = get_settings().data_root()
    preferred_port = port if port is not None else stable_desktop_port(data_root)
    listener = _bind_desktop_socket(data_root, preferred_port)
    port = int(listener.getsockname()[1])

    # The token is generated once and handed straight to the page in the URL, so
    # a desktop install never has to read a secret off disk.
    token = ensure_token()

    config = uvicorn.Config(
        create_app(with_scheduler=with_scheduler),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="tgfarm-api",
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        if not thread.is_alive():
            listener.close()
            raise RuntimeError("admin API stopped before the listener became ready")
        time.sleep(0.1)
    else:
        listener.close()
        raise RuntimeError("admin API did not start within 30s")

    log.info("admin API on %s", f"http://127.0.0.1:{port}")
    return RunningServer(port=port, token=token, thread=thread)


# ------------------------------------------------------------------ python bridge
class DesktopApi:
    """What the dashboard may ask of the host application.

    Exposed to JavaScript as `window.pywebview.api`. Deliberately small: the
    page can already do everything over HTTP, so this exists for the things only
    a real application can do — opening a folder, showing a native dialog,
    keeping publishing alive independently of the window.
    """

    def __init__(self, app: DesktopApp) -> None:
        self._app = app

    # -- introspection ------------------------------------------------------
    def info(self) -> dict:
        return {
            "app": APP_NAME,
            "version": _version(),
            "desktop": True,
            "platform": sys.platform,
            "data_dir": str(get_settings().data_root()),
            "mode": get_settings().mode,
        }

    # -- native affordances -------------------------------------------------
    def open_data_dir(self) -> str:
        target = get_settings().data_root()
        return _reveal(target)

    def open_log_dir(self) -> str:
        logs = get_settings().data_root() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return _reveal(logs)

    def open_config_file(self) -> str:
        env = Path.cwd() / ".env"
        if env.exists():
            return _reveal(env)
        example = Path.cwd() / ".env.example"
        return _reveal(example) if example.exists() else "нет .env — скопируйте .env.example"

    def open_url(self, url: str) -> str:
        """Open a validated HTTP(S) destination in the user's system browser."""
        from urllib.parse import urlsplit
        import webbrowser

        value = str(url or "").strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Разрешены только HTTP(S) ссылки без встроенных учётных данных.")
        if not webbrowser.open(value, new=2, autoraise=True):
            raise RuntimeError("Операционная система не подтвердила открытие браузера.")
        return "ok"

    def notify(self, title: str, message: str) -> str:
        self._app.notify(title, message)
        return "ok"

    # -- farm actions -------------------------------------------------------
    def launch_cycle(self) -> str:
        """Run one full cycle in the background and report the outcome."""
        return self._app.run_cycle_async()

    def set_minimize_to_tray(self, enabled: bool) -> str:
        self._app.minimize_to_tray = bool(enabled)
        return "ok"

    def quit(self) -> str:
        self._app.request_exit()
        return "ok"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("tgfarm")
    except Exception:  # noqa: BLE001 - running from a source checkout
        return "1.2.0"


def _reveal(path: Path) -> str:
    """Open a path in the OS file manager — Explorer on Windows."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - the documented Windows opener
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
    except Exception as exc:  # noqa: BLE001 - a missing file manager is not fatal
        log.warning("could not open %s: %s", path, exc)
        return f"не удалось открыть {path}: {exc}"
    return str(path)


# ------------------------------------------------------------------------ the app
class DesktopApp:
    def __init__(
        self,
        *,
        port: int | None = None,
        minimize_to_tray: bool = True,
        with_scheduler: bool = True,
    ) -> None:
        self.port = port
        self.minimize_to_tray = minimize_to_tray
        self.with_scheduler = with_scheduler
        self.window = None
        self.tray = None
        self._exit = threading.Event()
        self.server: RunningServer | None = None
        self.lock: SingleInstance | None = None
        self._supervisor_stop = threading.Event()

    # -- lifecycle ----------------------------------------------------------
    def request_exit(self) -> None:
        self._supervisor_stop.set()
        self._exit.set()
        try:
            if self.window is not None:
                self.window.destroy()
        finally:
            if self.tray is not None:
                self.tray.stop()

    def notify(self, title: str, message: str) -> None:
        if self.tray is not None:
            try:
                self.tray.notify(title, message)
                return
            except Exception as exc:  # noqa: BLE001 - notifications are optional
                log.debug("tray notify failed: %s", exc)
        print(f"{title}: {message}")

    def _build_menu(self) -> list:
        """Расширенное нативное меню: все вкладки, очередь публикации, диагностика."""
        from webview import Menu
        from webview.menu import MenuAction, MenuSeparator

        return [
            Menu(
                "Файл",
                [
                    MenuAction("Открыть папку данных", self._menu_open_data),
                    MenuAction("Открыть папку журналов", self._menu_open_logs),
                    MenuAction("Открыть .env", self._menu_open_env),
                    MenuAction("Открыть конфиг", self._menu_open_config),
                    MenuSeparator(),
                    MenuAction("Резервная копия БД", self._menu_backup_db),
                    MenuSeparator(),
                    MenuAction("Выход", self.request_exit),
                ],
            ),
            Menu(
                "Ферма",
                [
                    MenuAction("Запустить цикл сейчас", self._menu_cycle),
                    MenuSeparator(),
                    MenuAction("Обзор", lambda: self._goto("overview")),
                    MenuAction("Тренды", lambda: self._goto("trends")),
                    MenuAction("Посты", lambda: self._goto("posts")),
                    MenuAction("Очередь публикации", lambda: self._goto_filtered("posts", "gated")),
                    MenuAction("Каналы (включая VK)", lambda: self._goto("channels")),
                    MenuAction("Источники", lambda: self._goto("sources")),
                    MenuAction("Прогоны", lambda: self._goto("runs")),
                    MenuSeparator(),
                    MenuAction("Синхронизировать каналы", self._menu_sync_channels),
                    MenuAction("Статистика Telegram", self._menu_telegram_stats),
                    MenuAction("Статус кросспостов", self._menu_crosspost_status),
                ],
            ),
            Menu(
                "Вид",
                [
                    MenuAction("Обновить", lambda: self._js("window.tgfarmRefresh && tgfarmRefresh()")),
                    MenuAction("Открыть панель в браузере", self._menu_open_browser),
                    MenuSeparator(),
                    MenuAction("Переключить тему", lambda: self._js("window.tgfarmToggleTheme && tgfarmToggleTheme()")),
                    MenuAction("Полный экран", lambda: self._js("window.tgfarmToggleFullscreen && tgfarmToggleFullscreen()")),
                    MenuAction("Открыть журналы", self._menu_open_logs),
                ],
            ),
            Menu(
                "Инструменты",
                [
                    MenuAction("Диагностика (doctor)", self._menu_doctor),
                    MenuAction("Чек-лист запуска", self._menu_checklist),
                    MenuAction("Статус системы", self._menu_status),
                    MenuSeparator(),
                    MenuAction("Резервная копия БД", self._menu_backup_db),
                ],
            ),
            Menu(
                "Справка",
                [
                    MenuAction("Документация", self._menu_docs),
                    MenuSeparator(),
                    MenuAction("О программе", self._menu_about),
                ],
            ),
        ]

    # -- menu actions -------------------------------------------------------
    def _js(self, script: str) -> None:
        if self.window is not None:
            try:
                self.window.evaluate_js(script)
            except Exception as exc:  # noqa: BLE001 - menu must never crash the app
                log.debug("js failed: %s", exc)

    def _goto(self, view: str) -> None:
        self._js(f"window.tgfarmGoto && tgfarmGoto({view!r})")

    def _goto_filtered(self, view: str, status: str) -> None:
        self._goto(view)
        self._js(f"window.tgfarmSetPostFilter && tgfarmSetPostFilter({status!r})")

    def _menu_backup_db(self) -> None:
        import shutil
        from datetime import datetime

        settings = get_settings()
        src = settings.data_root() / "farm.db"
        backup_dir = settings.data_root() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"farm-{stamp}.db"
        try:
            if src.exists():
                shutil.copy2(src, target)
                self.notify(APP_NAME, f"Резервная копия: {target}")
            else:
                self.notify(APP_NAME, "База данных не найдена")
        except Exception as exc:  # noqa: BLE001
            log.warning("backup failed: %s", exc)
            self.notify(APP_NAME, f"Ошибка резервной копии: {exc}")

    def _menu_sync_channels(self) -> None:
        self._js("window.tgfarmSyncChannels && tgfarmSyncChannels()")

    def _menu_telegram_stats(self) -> None:
        self._js("window.tgfarmGoto && tgfarmGoto('channels')")
        self._js("window.tgfarmRefresh && tgfarmRefresh()")

    def _menu_checklist(self) -> None:
        self._js("window.tgfarmGoto && tgfarmGoto('overview')")
        self.notify(APP_NAME, "Проверьте: .env, каналы, источники, режим LIVE/DRY.")

    def _menu_docs(self) -> None:
        self._menu_open_browser()

    def _menu_cycle(self) -> None:
        self.run_cycle_async()

    def _menu_open_data(self) -> None:
        _reveal(get_settings().data_root())

    def _menu_open_logs(self) -> None:
        logs = get_settings().data_root() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        _reveal(logs)

    def _menu_open_env(self) -> None:
        for candidate in (Path.cwd() / ".env", Path.cwd() / ".env.example"):
            if candidate.exists():
                _reveal(candidate)
                return
        self.notify(APP_NAME, "Файл .env не найден — скопируйте .env.example")

    def _menu_open_browser(self) -> None:
        if self.server is not None:
            import webbrowser

            webbrowser.open(f"{self.server.url}?token={self.server.token}")

    def _menu_doctor(self) -> None:
        from tgfarm.diagnostics import run_doctor, summary

        ok, total, problems = summary(run_doctor())
        message = f"Проверок пройдено: {ok}/{total}"
        if problems:
            message += "\n" + "\n".join(problems)
        self.notify("Диагностика", message)

    def _menu_about(self) -> None:
        settings = get_settings()
        self.notify(
            f"{APP_NAME} {_version()}",
            f"Режим: {settings.mode}\nДанные: {settings.data_root()}",
        )

    def _menu_open_config(self) -> None:
        from tgfarm.core.settings import get_settings

        settings = get_settings()
        target = settings.data_root() / "config"
        if not target.exists():
            target = settings.data_root()
        _reveal(target)

    def _menu_crosspost_status(self) -> None:
        self._goto("channels")
        self._js("window.tgfarmRefresh && tgfarmRefresh()")
        try:
            from tgfarm.core.settings import get_settings

            s = get_settings()
            vk = "VK: подключён" if (s.vk_access_token and s.vk_group_id) else "VK: не настроен"
            self.notify(APP_NAME, vk)
        except Exception:  # noqa: BLE001
            pass

    def _menu_status(self) -> None:
        from tgfarm.core.settings import get_settings

        s = get_settings()
        lines = [
            f"Режим: {s.mode}",
            f"Изображения: {'включены' if s.image_enabled else 'выключены'} ({s.image_provider})",
            f"VK: {'подключён' if (s.vk_access_token and s.vk_group_id) else 'не настроен'}",
            f"Каналов TG: {len(s.channel_ids)}",
        ]
        self.notify("Статус TgFarm", "\n".join(lines))

    # -- always-on helpers --------------------------------------------------
    def _supervise(self) -> None:
        """Restart the API/scheduler thread if it dies, so the farm stays alive."""
        while not self._supervisor_stop.wait(10):
            if self._exit.is_set():
                break
            server = self.server
            if server is None or not server.thread.is_alive():
                log.warning("API thread is not running; restarting")
                self.notify(APP_NAME, "Перезапуск сервера фермы…")
                time.sleep(2)
                try:
                    self.server = start_server(
                        port=self.port, with_scheduler=self.with_scheduler
                    )
                    self.port = self.server.port
                except Exception as exc:  # noqa: BLE001
                    log.exception("server restart failed: %s", exc)
                    self.notify(APP_NAME, f"Не удалось перезапустить сервер: {exc}")

    def run_cycle_async(self) -> str:
        from tgfarm import pipeline

        def _worker() -> None:
            try:
                pipeline.run_cycle(collect=True)
                self.notify("TgFarm", "Цикл завершён")
            except Exception as exc:  # noqa: BLE001 - surface it, never crash the app
                log.warning("cycle failed: %s", exc)
                self.notify("TgFarm", f"Цикл не удался: {exc}")

        threading.Thread(target=_worker, name="tgfarm-cycle", daemon=True).start()
        return "запущен"

    def run(self) -> int:
        import webview

        ensure_bootstrapped()

        lock_path = get_settings().data_root() / "desktop.lock"
        self.lock = SingleInstance(lock_path)
        if not self.lock.acquire():
            print(f"{APP_NAME} уже запущен. Откройте окно из трея или закройте "
                  f"работающий процесс (pid в {lock_path}).")
            return 1

        if self.port is None:
            self.port = stable_desktop_port(get_settings().data_root())
        self.server = start_server(port=self.port, with_scheduler=self.with_scheduler)
        self.port = self.server.port
        threading.Thread(target=self._supervise, name="tgfarm-supervisor", daemon=True).start()
        url = f"{self.server.url}?token={self.server.token}"

        self.window = webview.create_window(
            APP_NAME,
            url,
            js_api=DesktopApi(self),
            width=1440,
            height=900,
            min_size=(1080, 680),
            frameless=False,
            easy_drag=False,
            on_top=False,
        )
        self.window.events.closing += self._on_closing

        self._start_tray()
        webview.start(
            self._on_start,
            self._on_stop,
            menu=self._build_menu(),
            icon=app_icon_path(),
            debug=False,
        )
        return 0

    # -- window events ------------------------------------------------------
    def _on_closing(self) -> bool:
        """Close means "hide" while the tray is available.

        Publishing must not stop because someone tidied their taskbar — that is
        the whole reason the tray exists.
        """
        if self.minimize_to_tray and self.tray is not None:
            if self.window is not None:
                self.window.hide()
            return False
        self.request_exit()
        return False

    # pywebview calls these with arguments that differ between versions, and a
    # signature mismatch here kills the UI thread — so accept anything.
    def _on_start(self, *_args: object) -> None:
        log.info("%s %s ready", APP_NAME, _version())

    def _on_stop(self, *_args: object) -> None:
        self._supervisor_stop.set()
        if self.lock is not None:
            self.lock.release()
        log.info("%s stopped", APP_NAME)

    # -- tray ---------------------------------------------------------------
    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            log.info("pystray not installed — running without a tray icon")
            return

        icon = _tray_icon(Image, ImageDraw)
        menu = pystray.Menu(
            pystray.MenuItem("Показать", lambda: self._show(), default=True),
            pystray.MenuItem("Обзор", lambda: (self._show(), self._goto("overview"))),
            pystray.MenuItem("Посты", lambda: (self._show(), self._goto("posts"))),
            pystray.MenuItem("Очередь", lambda: (self._show(), self._goto_filtered("posts", "gated"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Запустить цикл", lambda: self.run_cycle_async()),
            pystray.MenuItem("Открыть в браузере", lambda: self._menu_open_browser()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda: self.request_exit()),
        )
        self.tray = pystray.Icon("tgfarm", icon, APP_NAME, menu)
        threading.Thread(target=self.tray.run, name="tgfarm-tray", daemon=True).start()

    def _show(self) -> None:
        if self.window is not None:
            self.window.show()


def _app_mark(Image, ImageDraw, size: int = 256) -> object:
    """The app mark: a rising chart line. Generated, so no binary asset ships."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = max(2, size // 32)
    box = [pad, pad, size - pad - 1, size - pad - 1]
    draw.rounded_rectangle(box, radius=size // 6, fill=(11, 16, 32, 255))
    draw.rounded_rectangle(box, radius=size // 6, outline=(91, 140, 255, 255), width=max(2, size // 32))

    points = [(0.28, 0.68), (0.46, 0.46), (0.62, 0.56), (0.76, 0.28)]
    scaled = [(int(x * size), int(y * size)) for x, y in points]
    draw.line(scaled, fill=(255, 255, 255, 255), width=max(3, size // 24), joint="curve")
    dot = max(3, size // 28)
    draw.ellipse(
        [scaled[-1][0] - dot, scaled[-1][1] - dot, scaled[-1][0] + dot, scaled[-1][1] + dot],
        fill=(52, 211, 153, 255),
    )
    return image


def _tray_icon(Image, ImageDraw) -> object:
    return _app_mark(Image, ImageDraw, size=64)


def app_icon_path() -> Path | None:
    """Write an .ico into the data directory and return it, or None on failure."""
    try:
        from PIL import Image, ImageDraw

        target = get_settings().data_root() / "app.ico"
        if not target.exists():
            _app_mark(Image, ImageDraw).save(
                target, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)]
            )
        return target
    except Exception as exc:  # noqa: BLE001 - an icon is cosmetic, never fatal
        log.debug("could not build app icon: %s", exc)
        return None


# ------------------------------------------------------------------------- entrypoint
def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    setup_logging()
    log.info("%s starting", APP_NAME)

    port: int | None = None
    minimize = True
    for arg in argv:
        if arg in ("--no-tray", "--no-tray-on-close"):
            minimize = False
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])

    app = DesktopApp(port=port, minimize_to_tray=minimize)
    try:
        return app.run()
    except KeyboardInterrupt:
        app.request_exit()
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DesktopApi",
    "DesktopApp",
    "RunningServer",
    "SingleInstance",
    "free_port",
    "main",
    "start_server",
]