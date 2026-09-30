"""WPS GUI action primitives used by the 90-event runner.

The formal measurement path uses xdotool keyboard input and window-relative
coordinates. Screenshots/image recognition are intentionally not used inside
the 5-second measurement window.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path


# GUI session settings come from the caller's environment.  Do not assume a
# display number, UID, or display manager-specific Xauthority path.
DISPLAY_ENV = dict(os.environ)


def require_gui_environment() -> None:
    """Fail early with a useful message when optional WPS automation is used."""
    if not os.environ.get("DISPLAY"):
        raise RuntimeError(
            "WPS automation needs DISPLAY. Export the GUI session variables "
            "(DISPLAY, and when applicable XAUTHORITY/XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS).")
    if shutil.which("xdotool") is None:
        raise RuntimeError("WPS automation needs xdotool in PATH.")


def xdo(*args, check=True, capture=False):
    return subprocess.run(["xdotool", *map(str, args)], env=DISPLAY_ENV,
                          check=check, text=True, capture_output=capture)


def active_title() -> str:
    return xdo("getactivewindow", "getwindowname", check=False, capture=True).stdout.strip()


def _shell_values(text: str):
    values = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip()
    return values


def active_window_snapshot() -> dict:
    active = xdo("getactivewindow", check=False, capture=True).stdout.strip()
    geometry = {}
    if active.isdigit():
        geometry = _shell_values(
            xdo("getwindowgeometry", "--shell", active, check=False, capture=True).stdout)
    pointer = _shell_values(xdo("getmouselocation", "--shell", check=False,
                                capture=True).stdout)
    return {
        "active_window_id": active,
        "window_x": geometry.get("X", ""),
        "window_y": geometry.get("Y", ""),
        "window_width": geometry.get("WIDTH", ""),
        "window_height": geometry.get("HEIGHT", ""),
        "pointer_x": pointer.get("X", ""),
        "pointer_y": pointer.get("Y", ""),
        "pointer_window_id": pointer.get("WINDOW", ""),
    }


def visible_windows(token: str):
    result = xdo("search", "--onlyvisible", "--name", token, check=False, capture=True)
    return [value for value in result.stdout.split() if value.isdigit()]


def focus_token(token: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    last = active_title()
    while time.monotonic() < deadline:
        ids = visible_windows(token)
        if ids:
            xdo("windowactivate", "--sync", ids[-1])
            time.sleep(0.2)
            last = active_title()
            if token.lower() in last.lower():
                return last
        time.sleep(0.25)
    raise RuntimeError(f"window token not ready: token={token}; title={last}")


def require_active(token: str) -> str:
    title = active_title()
    if token.lower() not in title.lower():
        raise RuntimeError(f"active title mismatch: token={token}; title={title}")
    return title


def wait_title(token: str, timeout: float = 60.0) -> str:
    return focus_token(token, timeout)


def wait_active_title(token: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    last = active_title()
    while time.monotonic() < deadline:
        last = active_title()
        if token.lower() in last.lower():
            return last
        time.sleep(0.25)
    raise RuntimeError(f"active title not ready: token={token}; title={last}")


def wait_active_title_exact(expected: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    last = active_title()
    while time.monotonic() < deadline:
        last = active_title()
        if last.lower() == expected.lower():
            return last
        time.sleep(0.25)
    raise RuntimeError(f"active title not ready: expected={expected}; title={last}")


def wait_document_ready(token: str, timeout: float = 300.0,
                        stable_seconds: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    stable_since = None
    last = active_title()
    while time.monotonic() < deadline:
        ids = visible_windows(token)
        if ids:
            xdo("windowactivate", "--sync", ids[-1])
            last = active_title()
            if token.lower() in last.lower():
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= stable_seconds:
                    return last
            else:
                stable_since = None
        else:
            stable_since = None
        time.sleep(0.25)
    raise RuntimeError(f"document readiness timeout: token={token}; title={last}")


def begin_open_document(path: str) -> str:
    """Submit a path from the verified WPS open dialog without waiting for load."""
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    focus_token("WPS Office", 30.0)
    xdo("key", "ctrl+o")
    wait_active_title_exact("wpsoffice", 10.0)
    xdo("type", "--delay", "8", path)
    xdo("key", "Return")
    return active_title()


def open_document(path: str, token: str, timeout: float = 300.0) -> str:
    """Open a sample and wait until WPS exposes the expected document title.

    Large reference documents can take over a minute to finish loading after
    the file dialog accepts the path.  The readiness wait happens before the
    operation-specific input, so later actions never target the previous tab.
    """
    begin_open_document(path)
    return wait_document_ready(token, timeout)


def key_sequence(keys, token=None):
    if token:
        require_active(token)
    for key in keys:
        xdo("key", "--delay", "80", key)


def relative_move(x_ratio: float, y_ratio: float, token=None):
    if token:
        require_active(token)
    ids = visible_windows(token) if token else []
    if not ids:
        active = xdo("getactivewindow", capture=True).stdout.strip()
        if active.isdigit():
            ids = [active]
    if not ids:
        raise RuntimeError("relative click needs a visible token window")
    geometry = xdo("getwindowgeometry", "--shell", ids[-1], capture=True).stdout
    values = _shell_values(geometry)
    x, y = int(values.get("X", 0)), int(values.get("Y", 0))
    width, height = int(values.get("WIDTH", 1)), int(values.get("HEIGHT", 1))
    target_x = x + int(width * x_ratio)
    target_y = y + int(height * y_ratio)
    xdo("mousemove", str(target_x), str(target_y))
    pointer = active_window_snapshot()
    pointer_x = int(pointer["pointer_x"] or -1)
    pointer_y = int(pointer["pointer_y"] or -1)
    inside = x <= pointer_x < x + width and y <= pointer_y < y + height
    pointer_window = pointer["pointer_window_id"]
    if not inside or pointer_window not in ("", "0", pointer["active_window_id"]):
        raise RuntimeError(
            "pointer is outside active target window: "
            f"pointer=({pointer_x},{pointer_y}); pointer_window={pointer_window}; "
            f"active_window={pointer['active_window_id']}; "
            f"geometry=({x},{y},{width},{height})")
    return values


def relative_click(x_ratio: float, y_ratio: float, token=None):
    relative_move(x_ratio, y_ratio, token)
    xdo("click", "1")


def scroll_wheel(direction: str, clicks: int, token: str, x_ratio: float = 0.60):
    require_active(token)
    relative_move(x_ratio, 0.55, token)
    button = "5" if direction == "down" else "4"
    xdo("click", "--repeat", str(clicks), "--delay", "80", button)
    return require_active(token)


def safe_output_path(value: str) -> Path:
    path = Path(value).resolve()
    if path.suffix.lower() != ".docx":
        raise RuntimeError(f"save output must be a .docx: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def wait_saved_copy(path: Path, timeout: float = 180.0) -> str:
    deadline = time.monotonic() + timeout
    previous_size = -1
    stable_polls = 0
    while time.monotonic() < deadline:
        title = active_title()
        if path.is_file() and path.stat().st_size > 0 and "wps" != title.lower():
            size = path.stat().st_size
            stable_polls = stable_polls + 1 if size == previous_size else 0
            previous_size = size
            if stable_polls >= 2:
                return title
        time.sleep(0.5)
    raise RuntimeError(f"saved copy not ready: path={path}; title={active_title()}")


def dispatch(handler: str, args: dict, testdata: dict) -> str:
    word = testdata["word"]
    ppt = testdata["ppt"]
    excel = testdata["excel"]
    if handler == "open_word":
        return open_document(word, Path(word).stem)
    if handler == "open_ppt":
        return open_document(ppt, Path(ppt).stem)
    if handler == "start_open_ppt":
        return begin_open_document(ppt)
    if handler == "verify_ppt_open":
        return wait_document_ready(Path(ppt).stem)
    if handler in {"word_scroll_down", "excel_scroll_down"}:
        token = Path(word if handler.startswith("word") else excel if handler.startswith("excel") else ppt).stem
        return scroll_wheel("down", int(args.get("scroll_clicks_per_event", "3")), token)
    if handler == "scroll_down":
        return scroll_wheel("down", int(args.get("scroll_clicks_per_event", "3")),
                            Path(ppt).stem, x_ratio=0.08)
    if handler in {"word_scroll_up", "excel_scroll_up"}:
        token = Path(word if handler.startswith("word") else excel if handler.startswith("excel") else ppt).stem
        return scroll_wheel("up", int(args.get("scroll_clicks_per_event", "3")), token)
    if handler == "scroll_up":
        return scroll_wheel("up", int(args.get("scroll_clicks_per_event", "3")),
                            Path(ppt).stem, x_ratio=0.08)
    if handler == "click_slideshow":
        require_active(Path(ppt).stem)
        xdo("key", "F5")
        time.sleep(0.5)
        return active_title()
    if handler == "click_slideshow_next":
        relative_click(0.50, 0.50)
        return active_title()
    if handler == "escape_slideshow":
        xdo("key", "Escape")
        return wait_active_title(Path(ppt).stem, 20.0)
    if handler == "click_insert":
        require_active(Path(ppt).stem)
        xdo("key", "alt+i")
        return active_title()
    if handler == "click_textbox":
        require_active(Path(ppt).stem)
        key_sequence(["x", "h"])
        return require_active(Path(ppt).stem)
    if handler == "place_textbox":
        relative_click(float(args.get("x_ratio", "0.82")), float(args.get("y_ratio", "0.11")))
        return active_title()
    if handler == "save_ctrl_s":
        xdo("key", "ctrl+s")
        return active_title()
    if handler == "open_task_center":
        # The WPS home/task-center tab is in the top tab strip. The old
        # 0.06/0.035 point lands on the PPT export toolbar in current WPS.
        relative_click(0.03, 0.02)
        return focus_token("WPS Office", 20.0)
    if handler == "open_excel":
        return open_document(excel, Path(excel).stem)
    if handler in {"excel_bold", "excel_unbold"}:
        require_active(Path(excel).stem)
        xdo("key", "ctrl+b")
        return active_title()
    if handler == "excel_filter":
        require_active(Path(excel).stem)
        xdo("key", "ctrl+shift+l")
        return active_title()
    if handler == "file_menu":
        require_active(Path(word).stem)
        output = safe_output_path(testdata["save_path"])
        if output.exists():
            output.unlink()
        xdo("key", "alt+f")
        time.sleep(0.3)
        return active_title()
    if handler == "save_as":
        require_active(Path(word).stem)
        xdo("key", "a")
        time.sleep(0.3)
        return active_title()
    if handler == "download":
        require_active(Path(word).stem)
        key_sequence(["Down"] * 4 + ["Return"])
        wait_active_title_exact("wps")
        save_path = str(safe_output_path(testdata["save_path"]))
        xdo("key", "ctrl+a")
        xdo("type", "--delay", "8", save_path)
        return wait_active_title_exact("wps")
    if handler == "save_as_dropdown":
        wait_active_title_exact("wps")
        key_sequence(["Tab", "alt+Down", "Down", "Up", "Escape"])
        return wait_active_title_exact("wps")
    if handler == "save":
        output = safe_output_path(testdata["save_path"])
        wait_active_title_exact("wps")
        xdo("key", "alt+s")
        return wait_saved_copy(output)
    raise ValueError(f"unknown action handler: {handler}")
