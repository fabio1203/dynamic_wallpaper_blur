import ctypes
from ctypes import wintypes
import os
import threading
import time
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

# Custom message for cross-thread reinit signaling
WM_USER_REINIT = win32con.WM_USER + 1

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
_last_known_wallpaper_path: str = ""
_main_thread_id: int = 0
_state_lock = threading.RLock()

# --- FIXED: Explicit ctypes signature using c_longlong for LRESULT to avoid Pylance issues ---
user32 = ctypes.windll.user32
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong


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


def _apply_windows_style(img: Image.Image, width: int, height: int) -> Image.Image:
    """Apply Windows wallpaper scaling styles to match the actual desktop rendering."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HKCU_DESKTOP_KEY) as key:
            style_val, _ = winreg.QueryValueEx(key, WALLPAPER_STYLE_REG_VALUE)
            style = str(style_val)
    except Exception:
        style = "4"  # Default Fill

    if style == "0":  # Center
        bg = Image.new("RGB", (width, height), (0, 0, 0))
        img.thumbnail((width, height), Image.Resampling.LANCZOS)
        x = (width - img.width) // 2
        y = (height - img.height) // 2
        bg.paste(img, (x, y))
        return bg

    elif style == "1":  # Tile
        bg = Image.new("RGB", (width, height))
        for y in range(0, height, img.height):
            for x in range(0, width, img.width):
                bg.paste(img, (x, y))
        return bg

    elif style == "2":  # Stretch
        return img.resize((width, height), Image.Resampling.LANCZOS)

    elif style == "3":  # Fit
        bg = Image.new("RGB", (width, height), (0, 0, 0))
        img.thumbnail((width, height), Image.Resampling.LANCZOS)
        x = (width - img.width) // 2
        y = (height - img.height) // 2
        bg.paste(img, (x, y))
        return bg

    elif style == "4" or style == "10":  # Fill or Span
        crop = _get_windows_fill_crop(img.width, img.height, width, height)
        img_cropped = img.crop(crop)
        return img_cropped.resize((width, height), Image.Resampling.LANCZOS)

    else:
        crop = _get_windows_fill_crop(img.width, img.height, width, height)
        img_cropped = img.crop(crop)
        return img_cropped.resize((width, height), Image.Resampling.LANCZOS)


def _rect_overlap_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    """Return the overlapping area between two rectangles."""
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def _generate_dibs(image_path: str, width: int, height: int) -> tuple[Any, Any]:
    """Generate both sharp and blurred DIBs from an image path, matching Windows wallpaper style."""
    with Image.open(image_path) as img:
        img = img.convert("RGB")
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


def _wallpaper_watcher_thread():
    """Background thread that polls registry for wallpaper changes."""
    global _last_known_wallpaper_path
    while _wallpaper_watcher_running:
        try:
            current_path = _get_current_wallpaper_path_from_registry()
            if current_path and current_path != _last_known_wallpaper_path:
                with _state_lock:
                    _last_known_wallpaper_path = current_path
                print(f"[-] Registry watcher detected wallpaper change: {current_path}")
                # Signal main thread to do a full reinit — avoids Win32 threading issues
                if _main_thread_id:
                    user32.PostThreadMessageW(_main_thread_id, WM_USER_REINIT, 0, 0)
        except Exception as e:
            print(f"[!] Wallpaper watcher error: {e}")
        time.sleep(WALLPAPER_POLL_MS / 1000.0)


def start_wallpaper_watcher():
    """Start the background registry polling thread."""
    global _wallpaper_watcher_running, _last_known_wallpaper_path
    _wallpaper_watcher_running = True
    _last_known_wallpaper_path = _get_current_wallpaper_path_from_registry()
    watcher = threading.Thread(target=_wallpaper_watcher_thread, daemon=True)
    watcher.start()
    print(f"[-] Wallpaper registry watcher started (poll interval: {WALLPAPER_POLL_MS}ms)")


def stop_wallpaper_watcher():
    """Stop the background registry polling thread."""
    global _wallpaper_watcher_running
    _wallpaper_watcher_running = False


def initialize_wallpapers():
    global desktop_wallpaper, vdm, monitor_info

    ctypes.windll.ole32.CoInitialize(0)
    desktop_wallpaper = comtypes.client.CreateObject(GUID('{C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}'), interface=IDesktopWallpaper)
    vdm = comtypes.client.CreateObject(GUID('{AA509086-5CA9-4C25-8F95-589D3C07B48A}'), interface=IVirtualDesktopManager)

    count = desktop_wallpaper.GetMonitorDevicePathCount()  # type: ignore

    print(f"Detected {count} display(s). Generating assets...")

    for i in range(count):
        mon_id = desktop_wallpaper.GetMonitorDevicePathAt(i)  # type: ignore

        try:
            sharp_path = desktop_wallpaper.GetWallpaper(mon_id)  # type: ignore
        except Exception:
            print(f"  [!] Monitor {i} skipped: No native wallpaper file active.")
            continue

        rect = desktop_wallpaper.GetMonitorRECT(mon_id)  # type: ignore
        width = rect.right - rect.left
        height = rect.bottom - rect.top

        sharp_dib = None
        blurred_dib = None
        if os.path.exists(sharp_path):
            try:
                sharp_dib, blurred_dib = _generate_dibs(sharp_path, width, height)
            except Exception as e:
                print(f"  [!] Monitor {i} skipped: Failed to process image: {e}")
                continue

        if sharp_dib and blurred_dib:
            monitor_info.append({
                'id': mon_id,
                'rect': (rect.left, rect.top, rect.right, rect.bottom),
                'sharp_dib': sharp_dib,
                'blurred_dib': blurred_dib,
                'hwnd': None,
                'wallpaper_path': sharp_path
            })
            print(f"  [-] Monitor {i} pre-rendered ({width}x{height}).")


# --- Custom Painting Window Procedure ---
def wndProc(hwnd: int, message: int, wParam: int, lParam: int) -> int:
    if message == win32con.WM_PAINT:
        hdc, ps = win32gui.BeginPaint(hwnd) # type: ignore
        with _state_lock:
            state = window_states.get(hwnd)
            if state and state['dib']:
                state['dib'].expose(hdc)
        win32gui.EndPaint(hwnd, ps)
        return 0
    elif message == win32con.WM_ERASEBKGND:
        return 1
    return user32.DefWindowProcW(hwnd, message, wParam, lParam)


# --- Paradigm II: Layer Injection Setup ---
def setup_layer_injection() -> bool:
    global window_states

    progman = win32gui.FindWindow("Progman", None)
    if not progman:
        print("[!] Critical Error: Cannot find Progman window.")
        return False

    win32gui.SendMessageTimeout(progman, 0x052C, 0, 0, win32con.SMTO_NORMAL, 1000)

    callback_result: dict[str, int | None] = {'shelldll': None, 'parent_window': None}

    def find_shelldll_callback(hwnd: int, ctx: Any) -> bool:
        child = win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None)
        if child:
            callback_result['shelldll'] = child
            callback_result['parent_window'] = hwnd
            return False
        return True

    win32gui.EnumWindows(find_shelldll_callback, None)

    shelldll = callback_result['shelldll']
    parent_window = callback_result['parent_window']

    if not shelldll:
        shelldll = win32gui.FindWindowEx(progman, 0, "SHELLDLL_DefView", None)
        parent_window = progman

    if not shelldll:
        def find_workerw_callback(hwnd: int, ctx: Any) -> bool:
            if win32gui.GetClassName(hwnd) == "WorkerW":
                child = win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None)
                if child:
                    callback_result['shelldll'] = child
                    callback_result['parent_window'] = hwnd
                    return False
            return True
        win32gui.EnumWindows(find_workerw_callback, None)
        shelldll = callback_result['shelldll']
        parent_window = callback_result['parent_window']

    if not shelldll or not parent_window:
        print("[!] Critical Error: Desktop hierarchy layering unavailable.")
        print("    Attempting alternative approach...")
        if progman:
            parent_window = progman
        else:
            return False

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

        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_NOACTIVATE,
            "DesktopBlurOverlay",
            f"BlurOverlay_{minfo['id']}",
            win32con.WS_POPUP,
            ml, mt, width, height,
            0, 0, h_inst, None
        )

        if not hwnd:
            print(f"[!] Failed to create overlay window for monitor {minfo['id']}")
            continue

        if parent_window and parent_window != 0:
            try:
                win32gui.SetParent(hwnd, parent_window)
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                style &= ~win32con.WS_POPUP
                style |= win32con.WS_CHILD
                win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
            except Exception as e:
                print(f"[!] SetParent failed for monitor {minfo['id']}: {e}")
                print("    Continuing without parent window attachment...")

        win32gui.SetLayeredWindowAttributes(hwnd, 0, 0, win32con.LWA_ALPHA)

        if shelldll and shelldll != 0:
            try:
                win32gui.SetWindowPos(
                    hwnd,
                    shelldll,
                    ml, mt, width, height,
                    win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW
                )
            except Exception as e:
                print(f"[!] SetWindowPos failed: {e}")
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)

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

    # Kill any running fade timer — we want immediate snap
    if active_timer_id is not None:
        ctypes.windll.user32.KillTimer(0, active_timer_id)
        active_timer_id = None

    # Re-count windows
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

            # Re-read current wallpaper from COM (most reliable per-monitor path)
            current_path = ""
            try:
                current_path = desktop_wallpaper.GetWallpaper(mon_id)  # type: ignore
            except Exception:
                pass

            if current_path and os.path.exists(current_path) and current_path != minfo['wallpaper_path']:
                ml, mt, mr, mb = minfo['rect']
                width = mr - ml
                height = mb - mt
                try:
                    sharp_dib, blurred_dib = _generate_dibs(current_path, width, height)
                    minfo['sharp_dib'] = sharp_dib
                    minfo['blurred_dib'] = blurred_dib
                    minfo['wallpaper_path'] = current_path
                    print(f"[-] Reinit asset loaded for monitor {minfo['id']}: {current_path}")
                except Exception as e:
                    print(f"[!] Reinit failed for monitor {minfo['id']}: {e}")

            # Snap to correct state immediately (no fade)
            has_windows = active_counts[mon_id] > 0
            state = window_states[hwnd]
            state['is_blurred'] = has_windows
            state['pending_sharp_swap'] = False
            state['dib'] = minfo['blurred_dib'] if has_windows else minfo['sharp_dib']
            state['target_alpha'] = 255 if has_windows else 0
            state['current_alpha'] = state['target_alpha']

            win32gui.SetLayeredWindowAttributes(hwnd, 0, state['current_alpha'], win32con.LWA_ALPHA)

    # Force full repaint outside lock
    for minfo in monitor_info:
        hwnd = minfo['hwnd']
        if hwnd:
            win32gui.InvalidateRect(hwnd, None, True)  # type: ignore
            win32gui.UpdateWindow(hwnd)

    print("[-] Overlay reinitialization complete.")


# --- Native Win32 Timer Animation Hook Handler ---
TIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD)

@TIMERPROC
def timer_callback(hwnd: int, msg: int, idEvent: int, dwTime: int) -> None:
    animating = False
    updates: list[tuple[int, int]] = []
    swap_to_sharp: list[int] = []
    with _state_lock:
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
            ctypes.windll.user32.KillTimer(0, idEvent)
            global active_timer_id
            active_timer_id = None

    # Apply alpha updates outside the lock to prevent reentrancy deadlocks
    for hw, alpha in updates:
        win32gui.SetLayeredWindowAttributes(hw, 0, alpha, win32con.LWA_ALPHA)

    for hw in swap_to_sharp:
        try:
            win32gui.InvalidateRect(hw, None, True) # pyright: ignore[reportArgumentType]
            win32gui.UpdateWindow(hw)
        except Exception:
            pass


def _hotswap_wallpaper(minfo: dict[str, Any], current_path: str) -> bool:
    """Regenerate both sharp and blurred DIBs for a new wallpaper path."""
    ml, mt, mr, mb = minfo['rect']
    width = mr - ml
    height = mb - mt

    if not os.path.exists(current_path):
        print(f"[!] Hot-swap path does not exist: {current_path}")
        return False

    try:
        sharp_dib, blurred_dib = _generate_dibs(current_path, width, height)
    except Exception as e:
        print(f"[!] Hot-swap failed for monitor {minfo['id']}: {e}")
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

    print(f"[-] Hot-swap complete. Monitor {minfo['id']}: {current_path}")
    return True


def evaluate_and_swap() -> None:
    global active_timer_id
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


# --- Event Hooks ---
WinEventProcType = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
)

def event_callback(hWinEventHook: int, event: int, hwnd: int, idObject: int, idChild: int, dwEventThread: int, dwmsEventTime: int) -> None:
    # Ignore child-object noise; we only care about top-level window changes.
    if hwnd and idObject not in (0, win32con.OBJID_WINDOW):
        return
    evaluate_and_swap()


if __name__ == "__main__":
    initialize_wallpapers()
    if setup_layer_injection():
        evaluate_and_swap()
        start_wallpaper_watcher()

        user32_local = ctypes.windll.user32
        win_event_proc = WinEventProcType(event_callback)

        hook_ids = [
            user32_local.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT),
            user32_local.SetWinEventHook(EVENT_OBJECT_UNCLOAKED, EVENT_OBJECT_CLOAKED, 0, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        ]

        print("\nReal-Time Layered Injection service running. Press Ctrl+C to exit.")

        try:
            _main_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            msg = wintypes.MSG()
            while user32_local.GetMessageW(ctypes.byref(msg), 0, 0, 0) != 0:
                if msg.message == WM_USER_REINIT:
                    reinitialize_overlays()
                else:
                    user32_local.TranslateMessage(ctypes.byref(msg))
                    user32_local.DispatchMessageW(ctypes.byref(msg))
        except KeyboardInterrupt:
            pass
        finally:
            stop_wallpaper_watcher()
            print("[-] Shutting down wallpaper watcher.")
