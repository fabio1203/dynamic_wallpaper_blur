import ctypes
from ctypes import wintypes
import os
import sys
import threading
import time
import traceback
from typing import Any
from PIL import Image, ImageFilter, ImageWin
import win32gui
import win32con
import win32api
import winreg
import comtypes.client
from comtypes import IUnknown, GUID, COMMETHOD, HRESULT
from ctypes import c_wchar_p, c_uint, POINTER, c_bool

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
BLUR_STRENGTH = 4  # Increase for heavier blur, decrease for lighter blur
FADE_SPEED = 15     # Opacity shift per frame. Higher = faster fade. (25 ≈ 160ms total fade)
TIMER_INTERVAL = 15 # Animation frame step interval in milliseconds (~60 FPS)
WALLPAPER_POLL_MS = 500  # Registry poll interval for wallpaper changes
HEALTH_CHECK_INTERVAL_MS = 2000  # Interval for the parent-hierarchy health probe
LOG_PATH = os.path.join(os.environ.get("TEMP", os.getcwd()), "dynamic_wallpaper_blur.log")


def _log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass
    try:
        print(msg)
    except OSError:
        pass


def _log_exc(ctx: str) -> None:
    _log(f"[!] Exception in {ctx}:\n{traceback.format_exc()}")

# --- Windows API Constants ---
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_UNCLOAKED = 0x8018
EVENT_OBJECT_CLOAKED = 0x8019
WINEVENT_OUTOFCONTEXT = 0x0000
DWMWA_CLOAKED = 14
GA_ROOTOWNER = 3

# Registry paths
HKCU_DESKTOP_KEY = r"Control Panel\Desktop"
WALLPAPER_REG_VALUE = "Wallpaper"
WALLPAPER_STYLE_REG_VALUE = "WallpaperStyle"
WALLPAPER_TILE_REG_VALUE = "TileWallpaper"
TRANSCODED_WALLPAPER_PATH = os.path.join(
    os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Themes", "TranscodedWallpaper"
)

# Custom messages for cross-thread reinit / rebuild signaling
WM_USER_REINIT = win32con.WM_USER + 1
WM_USER_REBUILD = win32con.WM_USER + 2

# Power / session / hosting constants for sleep-wake and mixed-DPI handling
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
HWND_MESSAGE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_HOSTING_BEHAVIOR_MIXED = 1

# --- COM Interface Definitions ---
class IDesktopWallpaper(IUnknown):
    _iid_ = GUID('{B92B56A9-8B55-4E14-9A89-0199BBB6F93B}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'SetWallpaper', (['in'], c_wchar_p, 'monitorID'), (['in'], c_wchar_p, 'wallpaper')),
        COMMETHOD([], HRESULT, 'GetWallpaper', (['in'], c_wchar_p, 'monitorID'), (['out'], POINTER(c_wchar_p), 'wallpaper')),
        COMMETHOD([], HRESULT, 'GetMonitorDevicePathAt', (['in'], c_uint, 'monitorIndex'), (['out'], POINTER(c_wchar_p), 'monitorID')),
        COMMETHOD([], HRESULT, 'GetMonitorDevicePathCount', (['out'], POINTER(c_uint), 'count')),
        COMMETHOD([], HRESULT, 'GetMonitorRECT', (['in'], c_wchar_p, 'monitorID'), (['out'], POINTER(wintypes.RECT), 'displayRect')),
    ]

class IVirtualDesktopManager(IUnknown):
    _iid_ = GUID('{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'IsWindowOnCurrentVirtualDesktop', (['in'], wintypes.HWND, 'topLevelWindow'), (['out'], POINTER(c_bool), 'onCurrentDesktop')),
        COMMETHOD([], HRESULT, 'GetWindowDesktopId', (['in'], wintypes.HWND, 'topLevelWindow'), (['out'], POINTER(GUID), 'desktopId')),
        COMMETHOD([], HRESULT, 'MoveWindowToDesktop', (['in'], wintypes.HWND, 'topLevelWindow'), (['in'], GUID, 'desktopId'))
    ]

# --- Global State ---
desktop_wallpaper: IDesktopWallpaper = None  # type: ignore
vdm: IVirtualDesktopManager = None  # type: ignore
monitor_info: list[dict[str, Any]] = []
window_states: dict[int, dict[str, Any]] = {}
active_timer_id: int | None = None
_wallpaper_watcher_running = False
_last_known_wallpaper_signature: tuple = ()
_main_thread_id: int = 0
_state_lock = threading.RLock()
_hierarchy_generation: int = 0
_control_hwnd: int = 0
_shelldll_ref: int = 0  # SHELLDLL_DefView we parented to on the last setup, for health check
_health_timer_id: int | None = None

# --- FIXED: Explicit ctypes signature using c_longlong for LRESULT to avoid Pylance issues ---
user32 = ctypes.windll.user32
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong

# Per-monitor DPI context / mixed hosting so PMv2 top-level DPI does not get
# overridden by Progman's DPI context when we SetParent our overlay.
try:
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
except AttributeError:
    pass
try:
    user32.SetThreadDpiHostingBehavior.argtypes = [ctypes.c_int]
    user32.SetThreadDpiHostingBehavior.restype = ctypes.c_int
except AttributeError:
    pass
try:
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ScreenToClient.restype = wintypes.BOOL
except AttributeError:
    pass


def _set_thread_dpi_hosting_mixed() -> None:
    # Mixed hosting lets a child window keep its own DPI context instead of
    # inheriting the parent's (Progman is one DPI; our per-monitor overlays
    # need their own). Requires Windows 10 1803+.
    try:
        user32.SetThreadDpiHostingBehavior(DPI_HOSTING_BEHAVIOR_MIXED)
    except (AttributeError, OSError):
        pass


def _push_thread_pmv2() -> Any:
    # Pin the calling thread to PMv2 for the duration of a window creation
    # so the resulting HWND takes PMv2 context regardless of caller DPI leakage.
    try:
        return user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except (AttributeError, OSError):
        return None


def _pop_thread_dpi(prev: Any) -> None:
    if prev is None:
        return
    try:
        user32.SetThreadDpiAwarenessContext(prev)
    except (AttributeError, OSError):
        pass


def _screen_to_client(parent_hwnd: int, x: int, y: int) -> tuple[int, int]:
    # Convert screen coords to parent-client coords. After SetParent our
    # SetWindowPos coordinates are interpreted in the parent's client space;
    # passing raw screen coords produces the primary-not-at-(0,0) offset bug.
    pt = wintypes.POINT()
    pt.x, pt.y = int(x), int(y)
    try:
        user32.ScreenToClient(parent_hwnd, ctypes.byref(pt))
        return pt.x, pt.y
    except (AttributeError, OSError):
        return x, y


def _enable_dpi_awareness() -> None:
    # PMv2 makes GetMonitorRECT return physical pixels so the overlay DIB
    # shares the same pixel grid as Explorer's wallpaper draw — otherwise
    # DWM upscales the overlay and fades reveal a subpixel halo.
    user32_local = ctypes.windll.user32
    try:
        if user32_local.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32_local.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def _get_windows_fill_crop(img_width: int, img_height: int, target_width: int, target_height: int) -> tuple[int, int, int, int]:
    """
    Calculate crop box to match Windows 'Fill' wallpaper mode.
    Fill preserves aspect ratio by cropping the larger dimension to match target ratio.
    Returns (left, top, right, bottom) crop box in source image coordinates.
    """
    img_ratio = img_width / img_height
    target_ratio = target_width / target_height

    if img_ratio > target_ratio:
        new_width = int(img_height * target_ratio)
        left = (img_width - new_width) // 2
        return (left, 0, left + new_width, img_height)
    else:
        new_height = int(img_width / target_ratio)
        top = (img_height - new_height) // 2
        return (0, top, img_width, top + new_height)


def _read_wallpaper_style() -> tuple[int, int]:
    # Real Windows values: 0=Center/Tile, 2=Stretch, 6=Fit, 10=Fill, 22=Span.
    # Tile is distinguished from Center by TileWallpaper=1.
    style = 10
    tile = 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HKCU_DESKTOP_KEY) as key:
            try:
                v, _ = winreg.QueryValueEx(key, WALLPAPER_STYLE_REG_VALUE)
                style = int(v)
            except (OSError, ValueError):
                pass
            try:
                v, _ = winreg.QueryValueEx(key, WALLPAPER_TILE_REG_VALUE)
                tile = int(v)
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return style, tile


def _apply_windows_style(img: Image.Image, width: int, height: int,
                         style_override: tuple[int, int] | None = None) -> Image.Image:
    """Apply Windows wallpaper scaling styles to match the actual desktop rendering."""
    style, tile = style_override if style_override is not None else _read_wallpaper_style()

    if style == 0 and tile == 1:
        bg = Image.new("RGB", (width, height))
        for y in range(0, height, img.height):
            for x in range(0, width, img.width):
                bg.paste(img, (x, y))
        return bg

    if style == 0:
        bg = Image.new("RGB", (width, height), (0, 0, 0))
        img_copy = img.copy()
        img_copy.thumbnail((width, height), Image.Resampling.LANCZOS)
        x = (width - img_copy.width) // 2
        y = (height - img_copy.height) // 2
        bg.paste(img_copy, (x, y))
        return bg

    if style == 2:
        return img.resize((width, height), Image.Resampling.LANCZOS)

    if style == 6:
        bg = Image.new("RGB", (width, height), (0, 0, 0))
        img_copy = img.copy()
        img_copy.thumbnail((width, height), Image.Resampling.LANCZOS)
        x = (width - img_copy.width) // 2
        y = (height - img_copy.height) // 2
        bg.paste(img_copy, (x, y))
        return bg

    # Fill (10) and any unknown value fall through to the modern default.
    # Span (22) is handled upstream in _generate_dibs and never reaches here.
    crop = _get_windows_fill_crop(img.width, img.height, width, height)
    return img.crop(crop).resize((width, height), Image.Resampling.LANCZOS)


def _virtual_desktop_rect() -> tuple[int, int, int, int]:
    """Union rect of every monitor. Handles negative-coordinate secondaries via min/max."""
    if not monitor_info:
        return (0, 0, 0, 0)
    lefts = [m['rect'][0] for m in monitor_info]
    tops = [m['rect'][1] for m in monitor_info]
    rights = [m['rect'][2] for m in monitor_info]
    bottoms = [m['rect'][3] for m in monitor_info]
    return (min(lefts), min(tops), max(rights), max(bottoms))


def _rect_overlap_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    """Return the overlapping area between two rectangles."""
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def _generate_dibs(image_path: str, width: int, height: int,
                   span_ctx: tuple[tuple[int, int, int, int],
                                   tuple[int, int, int, int]] | None = None) -> tuple[Any, Any]:
    """Generate both sharp and blurred DIBs from an image path, matching Windows wallpaper style."""
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        if span_ctx is not None:
            # Span (WallpaperStyle=22): Windows fill-crops the source against the
            # virtual desktop rect and shows each monitor a slice of that composite.
            monitor_rect, virtual_rect = span_ctx
            vw = virtual_rect[2] - virtual_rect[0]
            vh = virtual_rect[3] - virtual_rect[1]
            crop = _get_windows_fill_crop(img.width, img.height, vw, vh)
            composite = img.crop(crop).resize((vw, vh), Image.Resampling.LANCZOS)
            sx = monitor_rect[0] - virtual_rect[0]
            sy = monitor_rect[1] - virtual_rect[1]
            styled_img = composite.crop((sx, sy, sx + width, sy + height))
        else:
            styled_img = _apply_windows_style(img, width, height)
        sharp_dib = ImageWin.Dib(styled_img)
        blurred_img = styled_img.filter(ImageFilter.GaussianBlur(radius=BLUR_STRENGTH))
        blurred_dib = ImageWin.Dib(blurred_img)
    return sharp_dib, blurred_dib


def _get_current_wallpaper_path_from_registry() -> str:
    """Read the active wallpaper path from the registry."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HKCU_DESKTOP_KEY) as key:
            path, _ = winreg.QueryValueEx(key, WALLPAPER_REG_VALUE)
            return str(path)
    except Exception:
        return ""


def _compute_per_monitor_paths(dw: Any) -> tuple[tuple[str, str], ...]:
    """Return sorted ((monitor_id, wallpaper_path), ...) from a thread-local IDesktopWallpaper.
    Returns () on any failure so a broken COM call degrades gracefully."""
    if dw is None:
        return ()
    try:
        count = dw.GetMonitorDevicePathCount()
    except Exception:
        return ()
    pairs: list[tuple[str, str]] = []
    for i in range(count):
        try:
            mid = dw.GetMonitorDevicePathAt(i)
            path = dw.GetWallpaper(mid)
        except Exception:
            continue
        pairs.append((str(mid), str(path or "")))
    return tuple(sorted(pairs))


def _compute_transcoded_mtimes() -> tuple[tuple[str, float], ...]:
    """mtimes for every TranscodedWallpaper* file in %APPDATA%\\Microsoft\\Windows\\Themes.
    Windows 11 uses TranscodedWallpaper_0/_1/_2 for per-monitor cached copies."""
    if not TRANSCODED_WALLPAPER_PATH:
        return ()
    directory = os.path.dirname(TRANSCODED_WALLPAPER_PATH)
    if not directory:
        return ()
    entries: list[tuple[str, float]] = []
    try:
        with os.scandir(directory) as it:
            for e in it:
                if not e.name.startswith("TranscodedWallpaper"):
                    continue
                try:
                    entries.append((e.name, e.stat().st_mtime))
                except OSError:
                    continue
    except OSError:
        return ()
    return tuple(sorted(entries))


def _compute_wallpaper_signature(dw: Any = None) -> tuple:
    # Signature parts:
    #   - registry Wallpaper path (global fallback)
    #   - WallpaperStyle + TileWallpaper (catches Fit/Fill/Center/Tile/Span flips)
    #   - per-monitor COM paths (catches Win11 per-monitor set + per-monitor slideshow)
    #   - every TranscodedWallpaper* mtime (catches slideshow rotation on any monitor)
    path = _get_current_wallpaper_path_from_registry()
    style, tile = _read_wallpaper_style()
    per_mon = _compute_per_monitor_paths(dw) if dw is not None else ()
    mtimes = _compute_transcoded_mtimes()
    return (path, style, tile, per_mon, mtimes)


def _wallpaper_watcher_thread():
    """Background thread that polls the wallpaper signature and signals reinit on change.

    Owns its own COM apartment + IDesktopWallpaper. Cross-apartment interface pointers
    are not safe, so we do NOT reuse the main-thread `desktop_wallpaper` global.
    """
    global _last_known_wallpaper_signature
    dw_local = None
    com_inited = False
    try:
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
            com_inited = True
            dw_local = comtypes.client.CreateObject(
                GUID('{C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}'), interface=IDesktopWallpaper)
        except Exception as e:
            _log(f"[!] Watcher COM init failed, degrading to registry-only signature: {e}")
            dw_local = None

        # Overwrite the initial signature with one that includes per-monitor paths,
        # so the very first tick does not fire a spurious reinit.
        try:
            with _state_lock:
                _last_known_wallpaper_signature = _compute_wallpaper_signature(dw_local)
        except Exception:
            _log_exc("wallpaper_watcher_bootstrap")

        while _wallpaper_watcher_running:
            try:
                sig = _compute_wallpaper_signature(dw_local)
                with _state_lock:
                    changed = sig != _last_known_wallpaper_signature
                    if changed:
                        _last_known_wallpaper_signature = sig
                if changed:
                    _log("[-] Watcher detected wallpaper/style change; signalling reinit.")
                    if _main_thread_id:
                        user32.PostThreadMessageW(_main_thread_id, WM_USER_REINIT, 0, 0)
            except Exception:
                _log_exc("wallpaper_watcher")
            time.sleep(WALLPAPER_POLL_MS / 1000.0)
    finally:
        if com_inited:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def start_wallpaper_watcher():
    """Start the background wallpaper-signal polling thread."""
    global _wallpaper_watcher_running, _last_known_wallpaper_signature
    _wallpaper_watcher_running = True
    # Initial signature without COM (main thread has COM but we avoid coupling);
    # the watcher will overwrite this on its first tick with a per-monitor signature.
    _last_known_wallpaper_signature = _compute_wallpaper_signature(None)
    watcher = threading.Thread(target=_wallpaper_watcher_thread, daemon=True)
    watcher.start()
    _log(f"[-] Wallpaper watcher started (poll {WALLPAPER_POLL_MS}ms; tracks path + style + tile + per-monitor + mtimes)")


def stop_wallpaper_watcher():
    """Stop the background registry polling thread."""
    global _wallpaper_watcher_running
    _wallpaper_watcher_running = False


def _ensure_com_objects() -> None:
    global desktop_wallpaper, vdm
    if desktop_wallpaper is None:
        desktop_wallpaper = comtypes.client.CreateObject(
            GUID('{C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}'), interface=IDesktopWallpaper)
    if vdm is None:
        vdm = comtypes.client.CreateObject(
            GUID('{AA509086-5CA9-4C25-8F95-589D3C07B48A}'), interface=IVirtualDesktopManager)


def _enumerate_monitors() -> list[dict[str, Any]]:
    """Fresh enumeration of active monitors. Reused on rebuild after sleep/wake."""
    _ensure_com_objects()
    count = desktop_wallpaper.GetMonitorDevicePathCount()  # type: ignore
    _log(f"Detected {count} display(s). Generating assets...")

    pending: list[dict[str, Any]] = []
    for i in range(count):
        try:
            mon_id = desktop_wallpaper.GetMonitorDevicePathAt(i)  # type: ignore
        except Exception:
            _log(f"  [!] Monitor {i} skipped: cannot query device path.")
            continue

        try:
            sharp_path = desktop_wallpaper.GetWallpaper(mon_id)  # type: ignore
        except Exception:
            _log(f"  [!] Monitor {i} skipped: No native wallpaper file active.")
            continue

        try:
            rect = desktop_wallpaper.GetMonitorRECT(mon_id)  # type: ignore
        except Exception:
            _log(f"  [!] Monitor {i} skipped: GetMonitorRECT failed.")
            continue

        pending.append({
            'index': i,
            'id': mon_id,
            'rect': (rect.left, rect.top, rect.right, rect.bottom),
            'sharp_dib': None,
            'blurred_dib': None,
            'hwnd': None,
            'wallpaper_path': sharp_path,
        })
    return pending


def _pregenerate_all_dibs(mons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate sharp+blurred DIBs for every monitor. Drops monitors with unreadable images."""
    if not mons:
        return []
    # Span (WallpaperStyle=22) needs the full virtual desktop rect *before* any
    # per-monitor DIB can be generated. Temporarily point monitor_info at the
    # candidate list so _virtual_desktop_rect sees the fresh topology.
    global monitor_info
    style, _ = _read_wallpaper_style()
    saved = monitor_info
    monitor_info = mons
    try:
        virt_rect = _virtual_desktop_rect() if style == 22 else None
    finally:
        monitor_info = saved

    kept: list[dict[str, Any]] = []
    for p in mons:
        rect = p['rect']
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        sharp_path = p['wallpaper_path']

        if not sharp_path or not os.path.exists(sharp_path):
            _log(f"  [!] Monitor {p.get('index', '?')} skipped: wallpaper path missing ({sharp_path!r}).")
            continue

        try:
            span_ctx = (rect, virt_rect) if virt_rect is not None else None
            sharp_dib, blurred_dib = _generate_dibs(sharp_path, width, height, span_ctx=span_ctx)
        except Exception as e:
            _log(f"  [!] Monitor {p.get('index', '?')} skipped: Failed to process image: {e}")
            continue

        p['sharp_dib'] = sharp_dib
        p['blurred_dib'] = blurred_dib
        kept.append(p)
        _log(f"  [-] Monitor {p.get('index', '?')} pre-rendered ({width}x{height}).")

    return kept


def initialize_wallpapers():
    """First-time COM init + monitor enumeration + DIB generation."""
    global monitor_info
    ctypes.windll.ole32.CoInitialize(0)
    _ensure_com_objects()
    monitor_info[:] = _pregenerate_all_dibs(_enumerate_monitors())


# --- Custom Painting Window Procedure ---
def wndProc(hwnd: int, message: int, wParam: int, lParam: int) -> int:
    try:
        if message == win32con.WM_PAINT:
            hdc, ps = win32gui.BeginPaint(hwnd)  # type: ignore
            try:
                with _state_lock:
                    state = window_states.get(hwnd)
                    if state and state['dib']:
                        state['dib'].expose(hdc)
            finally:
                win32gui.EndPaint(hwnd, ps)
            return 0
        elif message == win32con.WM_ERASEBKGND:
            return 1
    except Exception:
        _log_exc("wndProc")
    return user32.DefWindowProcW(hwnd, message, wParam, lParam)


# --- Paradigm II: Layer Injection Setup ---
def _locate_desktop_hierarchy() -> tuple[int, int, int]:
    """Return (progman, shelldll, parent_of_shelldll). shelldll/parent may be 0 on failure."""
    progman = win32gui.FindWindow("Progman", None)
    if not progman:
        return 0, 0, 0

    # Nudge Explorer to spawn a WorkerW behind the icons; harmless if already spawned.
    win32gui.SendMessageTimeout(progman, 0x052C, 0, 0, win32con.SMTO_NORMAL, 1000)

    result: dict[str, int | None] = {'shelldll': None, 'parent': None}

    def find_shelldll_cb(hwnd: int, ctx: Any) -> bool:
        child = win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None)
        if child:
            result['shelldll'] = child
            result['parent'] = hwnd
            return False
        return True

    win32gui.EnumWindows(find_shelldll_cb, None)

    shelldll = result['shelldll']
    parent_window = result['parent']

    if not shelldll:
        shelldll = win32gui.FindWindowEx(progman, 0, "SHELLDLL_DefView", None)
        parent_window = progman

    if not shelldll:
        def find_workerw_cb(hwnd: int, ctx: Any) -> bool:
            if win32gui.GetClassName(hwnd) == "WorkerW":
                child = win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None)
                if child:
                    result['shelldll'] = child
                    result['parent'] = hwnd
                    return False
            return True
        win32gui.EnumWindows(find_workerw_cb, None)
        shelldll = result['shelldll']
        parent_window = result['parent']

    return progman, int(shelldll or 0), int(parent_window or 0)


def setup_layer_injection() -> bool:
    global window_states, _shelldll_ref

    progman, shelldll, parent_window = _locate_desktop_hierarchy()
    if not progman:
        _log("[!] Critical Error: Cannot find Progman window.")
        return False

    if not shelldll or not parent_window:
        _log("[!] Desktop hierarchy layering unavailable; attempting Progman-only fallback.")
        parent_window = progman

    _shelldll_ref = shelldll  # cached for health check

    wc = win32gui.WNDCLASS()  # type: ignore
    wc.lpfnWndProc = wndProc  # type: ignore
    wc.lpszClassName = "DesktopBlurOverlay"  # type: ignore
    wc.hInstance = win32api.GetModuleHandle(None)  # type: ignore
    wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)  # type: ignore

    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass

    h_inst = win32api.GetModuleHandle(None)
    for minfo in monitor_info:
        ml, mt, mr, mb = minfo['rect']
        width = mr - ml
        height = mb - mt

        # Pin PMv2 around window creation so this HWND gets its monitor's DPI
        # context even after SetParent, instead of inheriting Progman's DPI.
        prev_ctx = _push_thread_pmv2()
        try:
            hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_NOACTIVATE,
                "DesktopBlurOverlay",
                f"BlurOverlay_{minfo['id']}",
                win32con.WS_POPUP,
                ml, mt, width, height,
                0, 0, h_inst, None
            )

            if not hwnd:
                _log(f"[!] Failed to create overlay window for monitor {minfo['id']}")
                continue

            if parent_window and parent_window != 0:
                try:
                    win32gui.SetParent(hwnd, parent_window)
                    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                    style &= ~win32con.WS_POPUP
                    style |= win32con.WS_CHILD
                    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
                except Exception as e:
                    _log(f"[!] SetParent failed for monitor {minfo['id']}: {e}")

            win32gui.SetLayeredWindowAttributes(hwnd, 0, 0, win32con.LWA_ALPHA)

            # After SetParent, SetWindowPos coords are interpreted in the parent's
            # client space, not screen space. Convert to client coords so a
            # primary-not-at-(0,0) layout does not shift the overlay by the parent
            # origin (the "overlay lands on wrong monitor" bug).
            if shelldll and shelldll != 0:
                target_parent = parent_window if parent_window else shelldll
                cx, cy = _screen_to_client(int(target_parent), ml, mt)
                try:
                    win32gui.SetWindowPos(
                        hwnd,
                        shelldll,
                        cx, cy, width, height,
                        win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW
                    )
                except Exception as e:
                    _log(f"[!] SetWindowPos failed: {e}")
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
        finally:
            _pop_thread_dpi(prev_ctx)

        with _state_lock:
            window_states[hwnd] = {
                'current_alpha': 0,
                'target_alpha': 0,
                'dib': minfo['sharp_dib'],
                'monitor_id': minfo['id'],
                'is_blurred': False,
                'pending_sharp_swap': False
            }
        minfo['hwnd'] = hwnd

    return True


# --- Teardown / rebuild for sleep-wake and Explorer restart ---
def teardown_overlays() -> None:
    """Destroy all overlay HWNDs, clear state, kill any active fade timer.

    Called from the main thread before rebuild_overlays(). Safe if already torn down.
    """
    global active_timer_id
    if active_timer_id is not None:
        try:
            ctypes.windll.user32.KillTimer(0, active_timer_id)
        except Exception:
            pass
        active_timer_id = None

    with _state_lock:
        for m in monitor_info:
            hwnd = m.get('hwnd')
            if hwnd:
                try:
                    win32gui.DestroyWindow(hwnd)
                except Exception:
                    pass
            m['hwnd'] = None
        window_states.clear()


def rebuild_overlays() -> None:
    """Full teardown+re-enumerate+re-create. Used after sleep/wake, Explorer restart,
    or any time the parent hierarchy became invalid."""
    global _hierarchy_generation
    _hierarchy_generation += 1
    _log(f"[-] Rebuilding overlay hierarchy (generation {_hierarchy_generation}).")
    teardown_overlays()
    # Fresh topology — monitor add/remove during sleep is common (lid close, dock).
    monitor_info[:] = _pregenerate_all_dibs(_enumerate_monitors())
    if not monitor_info:
        _log("[!] Rebuild aborted: no monitors resolved.")
        return
    if setup_layer_injection():
        evaluate_and_swap()
        _log("[-] Rebuild complete.")


# --- Hidden control window: receives power / session broadcasts ---
_control_wndproc_ref: Any = None  # keep the WNDPROC callable alive


def _control_wndProc(hwnd: int, msg: int, wp: int, lp: int) -> int:
    try:
        if msg == WM_POWERBROADCAST:
            if wp in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                _log(f"[-] Power broadcast wParam={wp:#x}; scheduling rebuild.")
                if _main_thread_id:
                    user32.PostThreadMessageW(_main_thread_id, WM_USER_REBUILD, 0, 0)
            return 1
        if msg == WM_WTSSESSION_CHANGE:
            if wp == WTS_SESSION_UNLOCK:
                _log("[-] Session unlock; scheduling rebuild.")
                if _main_thread_id:
                    user32.PostThreadMessageW(_main_thread_id, WM_USER_REBUILD, 0, 0)
            return 0
    except Exception:
        _log_exc("_control_wndProc")
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def create_control_window() -> int:
    """Register + create a message-only window that receives power and session events."""
    global _control_hwnd, _control_wndproc_ref
    wc = win32gui.WNDCLASS()  # type: ignore
    wc.lpfnWndProc = _control_wndProc  # type: ignore
    wc.lpszClassName = "DesktopBlurControl"  # type: ignore
    wc.hInstance = win32api.GetModuleHandle(None)  # type: ignore
    _control_wndproc_ref = wc.lpfnWndProc  # anchor against GC
    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass
    try:
        hwnd = win32gui.CreateWindowEx(
            0, "DesktopBlurControl", "", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, 0, win32api.GetModuleHandle(None), None
        )
    except Exception as e:
        _log(f"[!] Failed to create control window: {e}")
        return 0
    _control_hwnd = hwnd or 0
    # Session-change notifications are best-effort; not all Windows editions expose wtsapi32.
    try:
        wtsapi32 = ctypes.windll.wtsapi32
        wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
    except (AttributeError, OSError) as e:
        _log(f"[!] WTSRegisterSessionNotification unavailable: {e}")
    _log(f"[-] Control window created (hwnd={_control_hwnd}).")
    return _control_hwnd


def destroy_control_window() -> None:
    global _control_hwnd
    if _control_hwnd:
        try:
            ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(_control_hwnd)
        except (AttributeError, OSError):
            pass
        try:
            win32gui.DestroyWindow(_control_hwnd)
        except Exception:
            pass
        _control_hwnd = 0


# --- Health check: detects Explorer restart and orphaned overlays ---
HEALTHTIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD)


@HEALTHTIMERPROC
def health_timer_callback(hwnd: int, msg: int, idEvent: int, dwTime: int) -> None:
    try:
        # Don't touch HWNDs mid-fade — the animation timer holds them.
        if active_timer_id is not None:
            return
        if not monitor_info:
            return
        for m in monitor_info:
            h = m.get('hwnd')
            if not h or not user32.IsWindow(h):
                _log("[-] Health check: overlay HWND missing/invalid; scheduling rebuild.")
                if _main_thread_id:
                    user32.PostThreadMessageW(_main_thread_id, WM_USER_REBUILD, 0, 0)
                return
        # Detect Explorer restart: the SHELLDLL_DefView we parented to is gone
        # or replaced by a new one.
        _, current_shelldll, _ = _locate_desktop_hierarchy()
        if _shelldll_ref and current_shelldll and current_shelldll != _shelldll_ref:
            _log(f"[-] Health check: SHELLDLL_DefView changed ({_shelldll_ref} -> {current_shelldll}); rebuild.")
            if _main_thread_id:
                user32.PostThreadMessageW(_main_thread_id, WM_USER_REBUILD, 0, 0)
    except Exception:
        _log_exc("health_timer_callback")


def start_health_check() -> None:
    global _health_timer_id
    if _health_timer_id is not None:
        return
    _health_timer_id = ctypes.windll.user32.SetTimer(
        0, 0, HEALTH_CHECK_INTERVAL_MS, health_timer_callback
    )


def stop_health_check() -> None:
    global _health_timer_id
    if _health_timer_id is not None:
        try:
            ctypes.windll.user32.KillTimer(0, _health_timer_id)
        except Exception:
            pass
        _health_timer_id = None


# --- Window State Checking Logic ---
def get_window_monitor_id(hwnd: int) -> str | None:
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        return monitor_info[0]['id'] if monitor_info else None

    best_id: str | None = None
    best_area = 0
    for minfo in monitor_info:
        area = _rect_overlap_area(rect, minfo['rect'])
        if area > best_area:
            best_area = area
            best_id = minfo['id']

    if best_id is not None and best_area > 0:
        return best_id

    center_x = (rect[0] + rect[2]) // 2
    center_y = (rect[1] + rect[3]) // 2
    for minfo in monitor_info:
        ml, mt, mr, mb = minfo['rect']
        if ml <= center_x <= mr and mt <= center_y <= mb:
            return minfo['id']
    return monitor_info[0]['id'] if monitor_info else None


def is_valid_application_window(hwnd: int) -> bool:
    if not win32gui.IsWindowVisible(hwnd): return False  # type: ignore
    if win32gui.IsIconic(hwnd): return False

    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if (ex_style & win32con.WS_EX_TOOLWINDOW): return False  # type: ignore

    user32_local = ctypes.windll.user32
    if user32_local.GetLastActivePopup(user32_local.GetAncestor(hwnd, GA_ROOTOWNER)) != hwnd: return False  # type: ignore

    is_cloaked = ctypes.c_int(0)
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(is_cloaked), ctypes.sizeof(is_cloaked)) == 0:
        if is_cloaked.value != 0: return False  # type: ignore

    if win32gui.GetClassName(hwnd) in ["WorkerW", "Progman", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow", "DesktopBlurOverlay"]: return False  # type: ignore

    try:
        if not vdm.IsWindowOnCurrentVirtualDesktop(hwnd):  # type: ignore
            return False
    except Exception:
        pass 

    return True


def reinitialize_overlays():
    """
    Full reinitialization of overlay assets and state.
    Called from main thread when wallpaper change is detected.
    Regenerates DIBs from current wallpaper and snaps alpha to correct state.
    """
    global active_timer_id
    try:
        if active_timer_id is not None:
            try:
                ctypes.windll.user32.KillTimer(0, active_timer_id)
            except Exception:
                pass
            active_timer_id = None

        active_counts: dict[str, int] = {minfo['id']: 0 for minfo in monitor_info}

        def enum_handler(hwnd: int, ctx: Any) -> None:
            if is_valid_application_window(hwnd):
                mon_id = get_window_monitor_id(hwnd)
                if mon_id in active_counts:
                    active_counts[mon_id] += 1

        win32gui.EnumWindows(enum_handler, None)

        with _state_lock:
            for minfo in monitor_info:
                mon_id = minfo['id']
                hwnd = minfo['hwnd']
                if not hwnd:
                    continue

                current_path = ""
                try:
                    current_path = desktop_wallpaper.GetWallpaper(mon_id)  # type: ignore
                except Exception:
                    pass

                if current_path and os.path.exists(current_path):
                    ml, mt, mr, mb = minfo['rect']
                    width = mr - ml
                    height = mb - mt
                    try:
                        style, _ = _read_wallpaper_style()
                        span_ctx = ((ml, mt, mr, mb), _virtual_desktop_rect()) if style == 22 else None
                        sharp_dib, blurred_dib = _generate_dibs(current_path, width, height, span_ctx=span_ctx)
                        minfo['sharp_dib'] = sharp_dib
                        minfo['blurred_dib'] = blurred_dib
                        minfo['wallpaper_path'] = current_path
                        _log(f"[-] Reinit asset loaded for monitor {minfo['id']}: {current_path}")
                    except Exception as e:
                        _log(f"[!] Reinit failed for monitor {minfo['id']}: {e}")

                has_windows = active_counts[mon_id] > 0
                state = window_states.get(hwnd)
                if state is None:
                    continue
                state['is_blurred'] = has_windows
                state['pending_sharp_swap'] = False
                state['dib'] = minfo['blurred_dib'] if has_windows else minfo['sharp_dib']
                state['target_alpha'] = 255 if has_windows else 0
                state['current_alpha'] = state['target_alpha']

                try:
                    win32gui.SetLayeredWindowAttributes(hwnd, 0, state['current_alpha'], win32con.LWA_ALPHA)
                except Exception:
                    pass

        for minfo in monitor_info:
            hwnd = minfo['hwnd']
            if hwnd:
                try:
                    win32gui.InvalidateRect(hwnd, None, True)  # type: ignore
                    win32gui.UpdateWindow(hwnd)
                except Exception:
                    pass

        _log("[-] Overlay reinitialization complete.")
    except Exception:
        _log_exc("reinitialize_overlays")


# --- Native Win32 Timer Animation Hook Handler ---
TIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD)

@TIMERPROC
def timer_callback(hwnd: int, msg: int, idEvent: int, dwTime: int) -> None:
    global active_timer_id
    try:
        gen_at_entry = _hierarchy_generation
        animating = False
        updates: list[tuple[int, int]] = []
        swap_to_sharp: list[int] = []
        with _state_lock:
            # If a rebuild happened during this callback dispatch, our HWNDs may
            # already be destroyed. Kill the timer and no-op the rest.
            if gen_at_entry != _hierarchy_generation:
                try:
                    ctypes.windll.user32.KillTimer(0, idEvent)
                except Exception:
                    pass
                active_timer_id = None
                return

            for hw, state in window_states.items():
                curr = state['current_alpha']
                target = state['target_alpha']

                if curr != target:
                    animating = True
                    if curr < target:
                        curr = min(target, curr + FADE_SPEED)
                    else:
                        curr = max(target, curr - FADE_SPEED)
                    state['current_alpha'] = curr
                    updates.append((hw, curr))

                    if curr == 0 and target == 0 and state.get('pending_sharp_swap'):
                        swap_to_sharp.append(hw)
                elif target == 0 and state.get('pending_sharp_swap') and curr == 0:
                    swap_to_sharp.append(hw)

            for hw in swap_to_sharp:
                state = window_states.get(hw)
                if not state:
                    continue
                monitor_id = state['monitor_id']
                minfo = next((m for m in monitor_info if m['id'] == monitor_id), None)
                if minfo:
                    state['dib'] = minfo['sharp_dib']
                state['is_blurred'] = False
                state['pending_sharp_swap'] = False

            if not animating:
                try:
                    ctypes.windll.user32.KillTimer(0, idEvent)
                except Exception:
                    pass
                active_timer_id = None

        # Apply alpha updates outside the lock to prevent reentrancy deadlocks
        for hw, alpha in updates:
            try:
                win32gui.SetLayeredWindowAttributes(hw, 0, alpha, win32con.LWA_ALPHA)
            except Exception:
                pass

        for hw in swap_to_sharp:
            try:
                win32gui.InvalidateRect(hw, None, True)  # pyright: ignore[reportArgumentType]
                win32gui.UpdateWindow(hw)
            except Exception:
                pass
    except Exception:
        _log_exc("timer_callback")


def _hotswap_wallpaper(minfo: dict[str, Any], current_path: str) -> bool:
    """Regenerate both sharp and blurred DIBs for a new wallpaper path."""
    ml, mt, mr, mb = minfo['rect']
    width = mr - ml
    height = mb - mt

    if not os.path.exists(current_path):
        _log(f"[!] Hot-swap path does not exist: {current_path}")
        return False

    try:
        style, _ = _read_wallpaper_style()
        span_ctx = ((ml, mt, mr, mb), _virtual_desktop_rect()) if style == 22 else None
        sharp_dib, blurred_dib = _generate_dibs(current_path, width, height, span_ctx=span_ctx)
    except Exception as e:
        _log(f"[!] Hot-swap failed for monitor {minfo['id']}: {e}")
        return False

    minfo['sharp_dib'] = sharp_dib
    minfo['blurred_dib'] = blurred_dib
    minfo['wallpaper_path'] = current_path

    hwnd = minfo['hwnd']
    if hwnd and hwnd in window_states:
        with _state_lock:
            state = window_states[hwnd]
            # Keep the currently displayed mode consistent with the fade state.
            if state['is_blurred'] or state['target_alpha'] > 0 or state['current_alpha'] > 0:
                state['dib'] = blurred_dib
            else:
                state['dib'] = sharp_dib

        win32gui.InvalidateRect(hwnd, None, True)  # type: ignore  # type: ignore
        win32gui.UpdateWindow(hwnd)

    _log(f"[-] Hot-swap complete. Monitor {minfo['id']}: {current_path}")
    return True


def evaluate_and_swap() -> None:
    global active_timer_id
    try:
        active_counts: dict[str, int] = {minfo['id']: 0 for minfo in monitor_info}

        def enum_handler(hwnd: int, ctx: Any) -> None:
            if is_valid_application_window(hwnd):
                mon_id = get_window_monitor_id(hwnd)
                if mon_id in active_counts:
                    active_counts[mon_id] += 1

        win32gui.EnumWindows(enum_handler, None)

        changed = False
        repaint_hwnds: list[int] = []
        with _state_lock:
            for minfo in monitor_info:
                mon_id = minfo['id']
                hwnd = minfo['hwnd']
                if not hwnd or hwnd not in window_states:
                    continue

                target = 255 if active_counts[mon_id] > 0 else 0
                state = window_states[hwnd]
                should_be_blurred = target == 255

                # Prefer the per-monitor COM wallpaper path. The registry path is only
                # a coarse global signal and should not be used as the active wallpaper
                # source for individual monitors unless COM cannot resolve anything.
                current_path = ""
                try:
                    com_path = desktop_wallpaper.GetWallpaper(mon_id)  # type: ignore
                    if com_path and os.path.exists(com_path) and com_path != minfo['wallpaper_path']:
                        current_path = com_path
                except Exception:
                    pass

                if not current_path and not minfo['wallpaper_path']:
                    registry_path = _get_current_wallpaper_path_from_registry()
                    if registry_path and os.path.exists(registry_path):
                        current_path = registry_path

                if current_path:
                    if _hotswap_wallpaper(minfo, current_path):
                        repaint_hwnds.append(hwnd)

                # Fade-in: ensure blurred art is shown before raising alpha.
                if should_be_blurred and not state['is_blurred']:
                    state['dib'] = minfo['blurred_dib']
                    state['is_blurred'] = True
                    state['pending_sharp_swap'] = False
                    repaint_hwnds.append(hwnd)

                # Fade-out: keep blurred art until alpha reaches 0, then swap to sharp.
                if not should_be_blurred and state['is_blurred']:
                    state['pending_sharp_swap'] = True
                elif should_be_blurred:
                    state['pending_sharp_swap'] = False

                if state['target_alpha'] != target:
                    state['target_alpha'] = target
                    changed = True

        for hwnd in repaint_hwnds:
            try:
                win32gui.InvalidateRect(hwnd, None, True)  # type: ignore
                win32gui.UpdateWindow(hwnd)
            except Exception:
                pass

        if changed and active_timer_id is None:
            active_timer_id = ctypes.windll.user32.SetTimer(0, 0, TIMER_INTERVAL, timer_callback)
    except Exception:
        _log_exc("evaluate_and_swap")


# --- Event Hooks ---
WinEventProcType = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
)

def event_callback(hWinEventHook: int, event: int, hwnd: int, idObject: int, idChild: int, dwEventThread: int, dwmsEventTime: int) -> None:
    try:
        # Ignore child-object noise; we only care about top-level window changes.
        if hwnd and idObject not in (0, win32con.OBJID_WINDOW):
            return
        evaluate_and_swap()
    except Exception:
        _log_exc("event_callback")


def _uncaught_excepthook(t, v, tb) -> None:
    _log(f"[!] Uncaught: {''.join(traceback.format_exception(t, v, tb))}")


if __name__ == "__main__":
    sys.excepthook = _uncaught_excepthook
    _log(f"[-] Startup pid={os.getpid()} python={sys.version.split()[0]} script={__file__}")

    # DPI awareness must be set before any HWND is created or GetMonitorRECT is
    # called, otherwise Explorer's wallpaper and our overlay end up on different
    # pixel grids on scaled displays.
    _enable_dpi_awareness()
    # Mixed hosting lets each per-monitor overlay keep its own DPI context after
    # SetParent — required for mixed-DPI multi-monitor layouts.
    _set_thread_dpi_hosting_mixed()
    # Set the main thread id before starting the watcher so its first
    # PostThreadMessageW cannot miss the target.
    _main_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

    try:
        initialize_wallpapers()
    except Exception:
        _log_exc("initialize_wallpapers")

    create_control_window()

    if setup_layer_injection():
        evaluate_and_swap()
        start_wallpaper_watcher()
        start_health_check()

        user32_local = ctypes.windll.user32
        win_event_proc = WinEventProcType(event_callback)

        hook_ids = [
            user32_local.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_OBJECT_UNCLOAKED, EVENT_OBJECT_CLOAKED, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        ]

        _log(f"[-] Service running. {len(monitor_info)} overlay(s) active. Log: {LOG_PATH}")

        try:
            msg = wintypes.MSG()
            while user32_local.GetMessageW(ctypes.byref(msg), 0, 0, 0) != 0:
                try:
                    if msg.message == WM_USER_REINIT:
                        reinitialize_overlays()
                    elif msg.message == WM_USER_REBUILD:
                        rebuild_overlays()
                    else:
                        user32_local.TranslateMessage(ctypes.byref(msg))
                        user32_local.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    _log_exc("message_loop")
        except KeyboardInterrupt:
            pass
        finally:
            stop_wallpaper_watcher()
            stop_health_check()
            destroy_control_window()
            _log("[-] Shutting down.")
    else:
        _log("[!] setup_layer_injection failed; exiting.")
        destroy_control_window()
