"""샵마인(WinForms 데스크톱 앱) 화면을 조작하기 위한 최소 도구.

이 모듈은 '클릭할 수 있게' 하는 것보다 '엉뚱한 데 클릭하지 않게' 하는 데
초점이 있다. 실제로 겪은 사고를 그대로 방어한다:

  1. 다른 창이 위를 덮고 있으면 그 창이 클릭을 먹는다
     -> window_at()/is_descendant()로 클릭 직전에 확인
  2. 다중 모니터에서 SendInput 절대좌표는 MOUSEEVENTF_VIRTUALDESK 없이는
     주 모니터 기준으로 접혀서 엉뚱한 좌표를 클릭한다 (x가 절반이 됐다)
     -> move_to()가 항상 VIRTUALDESK를 붙이고, 이동 후 실제 커서 위치를 확인
  3. 버튼 중심점이 하필 버튼 안 '흰 글자' 위일 수 있다
     -> 색 검증은 한 점이 아니라 주변 25점 다수결

샵마인 화면 자동화 자체의 배경은 excel_io.py 상단 주석 참고. 요약하면
UI Automation으로 DataGridView를 '읽는' 것만 앱을 크래시시켰고, 마우스/키보드
입력 자체는 안전하다.

표준 Win32 대화상자(#32770)는 좌표가 아니라 컨트롤 ID로 다룰 수 있으므로
가능하면 dlg_button()을 쓴다 - 좌표 클릭보다 훨씬 정확하다.
"""

import ctypes, ctypes.wintypes as wt, struct, sys, time

u = ctypes.windll.user32
g = ctypes.windll.gdi32
ctypes.windll.shcore.SetProcessDpiAwareness(2)
ENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _text(h):
    n = u.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(h, b, n + 1)
    return b.value


def _cls(h):
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, b, 256)
    return b.value


def find_windows(title_startswith=None, title_equals=None, min_w=80, min_h=40):
    """샵마인 프로세스가 소유한 보이는 창을 조건에 맞게 찾는다."""
    pid_box = []
    def find_pid(h, l):
        if u.IsWindowVisible(h) and _text(h).startswith("ShopMine::"):
            p = wt.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(p))
            pid_box.append(p.value)
        return True
    u.EnumWindows(ENUM(find_pid), 0)
    if not pid_box:
        return []
    pid = pid_box[0]
    out = []
    def collect(h, l):
        if not u.IsWindowVisible(h):
            return True
        p = wt.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value != pid:
            return True
        r = wt.RECT(); u.GetWindowRect(h, ctypes.byref(r))
        if r.right - r.left < min_w or r.bottom - r.top < min_h:
            return True
        t = _text(h)
        if title_equals is not None and t != title_equals:
            return True
        if title_startswith is not None and not t.startswith(title_startswith):
            return True
        out.append((h, t, (r.left, r.top, r.right, r.bottom)))
        return True
    u.EnumWindows(ENUM(collect), 0)
    return out


# --- 자식 컨트롤 다루기 --------------------------------------------
def children(parent):
    """자식 컨트롤 핸들 전부 (깊이 제한 없음).

    샵마인 메인 창은 자식이 700개가 넘고 필요한 컨트롤이 깊이 들어 있다.
    한 단계만 훑으면 안 보인다.
    """
    out = []
    u.EnumChildWindows(parent, ENUM(lambda h, l: (out.append(h), True)[1]), 0)
    return out


def class_of(hwnd):
    """WinForms 창클래스에서 실제 종류만 뽑는다 (WindowsForms10.EDIT.app... -> EDIT)."""
    c = _cls(hwnd)
    if c.startswith("WindowsForms10.") and "." in c:
        return c.split(".")[1]
    return c


def rect(hwnd):
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def find_child(parent, cls_name, predicate):
    """조건에 맞는 첫 자식 컨트롤. 없으면 None."""
    for k in children(parent):
        if class_of(k) == cls_name and predicate(k):
            return k
    return None


def pixel(x, y):
    """화면 절대좌표의 색을 (R, G, B)로 읽는다."""
    hdc = u.GetDC(0)
    v = g.GetPixel(hdc, x, y)
    u.ReleaseDC(0, hdc)
    if v == 0xFFFFFFFF:
        return None
    return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def close_to(c1, c2, tol=28):
    return c1 is not None and all(abs(a - b) <= tol for a, b in zip(c1, c2))


# --- 마우스 입력 (SendInput) ---
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]
class _IU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _IU)]

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_VIRTUALDESK = 0x4000  # 다중 모니터: 가상 데스크톱 전체 기준으로 해석
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    u.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def move_to(x, y):
    """가상 데스크톱 절대좌표로 커서 이동 (다중 모니터 대응)."""
    vx = u.GetSystemMetrics(76); vy = u.GetSystemMetrics(77)
    vw = u.GetSystemMetrics(78); vh = u.GetSystemMetrics(79)
    ax = int(round((x - vx) * 65535.0 / (vw - 1)))
    ay = int(round((y - vy) * 65535.0 / (vh - 1)))
    mv = INPUT(type=INPUT_MOUSE)
    mv.u.mi = MOUSEINPUT(ax, ay, 0,
                         MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                         0, None)
    _send(mv)


def move_click(x, y, verify=True, tol=3, dwell=0.45):
    """좌표로 이동한 뒤, 커서가 정말 그 자리에 갔는지 확인하고 클릭한다.

    다중 모니터/DPI 문제로 커서가 엉뚱한 곳에 놓이면 클릭하지 않는다.

    dwell: 누르기 직전에 그 자리에 머무는 시간. WinForms ToolStrip 버튼은
    마우스가 들어온 것을 인식한 뒤에야 클릭을 받는다. 0.25초로는 [송장수정모드
    켜기] 클릭이 조용히 무시됐다 (창은 활성화되는데 버튼은 안 눌림).
    """
    move_to(x, y)
    time.sleep(0.15)
    if verify:
        cx, cy = cursor_pos()
        if abs(cx - x) > tol or abs(cy - y) > tol:
            move_to(x, y)          # 한 번 더 시도
            time.sleep(0.2)
            cx, cy = cursor_pos()
            if abs(cx - x) > tol or abs(cy - y) > tol:
                raise RuntimeError(
                    f"커서가 목표에 도달하지 못함: 목표=({x},{y}) 실제=({cx},{cy}) - 클릭 취소")
    time.sleep(dwell)
    dn = INPUT(type=INPUT_MOUSE); dn.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None)
    up = INPUT(type=INPUT_MOUSE); up.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None)
    _send(dn); time.sleep(0.06); _send(up)


def cursor_pos():
    p = wt.POINT(); u.GetCursorPos(ctypes.byref(p)); return p.x, p.y


MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


def wheel(x, y, notches):
    """(x, y)로 커서를 옮긴 뒤 마우스 휠을 굴린다. 양수=아래로, 음수=위로.

    휠은 '커서 아래 컨트롤'로 가므로 좌표를 반드시 그리드 안으로 줘야 한다.
    """
    move_to(x, y)
    time.sleep(0.2)
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(0, 0, (-WHEEL_DELTA * notches) & 0xFFFFFFFF,
                          MOUSEEVENTF_WHEEL, 0, None)
    _send(inp)


# --- 스크롤바 읽기 -------------------------------------------------
# GetScrollInfo 는 다른 프로세스의 표준 SCROLLBAR 컨트롤에도 동작한다
# (WM_GETTEXT 와 달리 이건 윈도우가 대신 마샬링해준다). 단위는 픽셀이라
# 행 높이로 나누면 전체 행 수를 알 수 있다.
class SCROLLINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("fMask", wt.UINT), ("nMin", ctypes.c_int),
                ("nMax", ctypes.c_int), ("nPage", wt.UINT), ("nPos", ctypes.c_int),
                ("nTrackPos", ctypes.c_int)]


SIF_ALL = 0x17
SB_CTL = 2


def scroll_info(hwnd):
    """스크롤바 컨트롤의 (현재위치, 최대위치, 한 페이지) - 실패하면 None."""
    si = SCROLLINFO()
    si.cbSize = ctypes.sizeof(SCROLLINFO)
    si.fMask = SIF_ALL
    if not u.GetScrollInfo(hwnd, SB_CTL, ctypes.byref(si)):
        return None
    return si.nPos, max(0, si.nMax - si.nPage + 1), si.nPage


def key(vk, alt=False):
    seq = []
    if alt:
        a = INPUT(type=INPUT_KEYBOARD); a.u.ki = KEYBDINPUT(0x12, 0, 0, 0, None); seq.append(a)
    d = INPUT(type=INPUT_KEYBOARD); d.u.ki = KEYBDINPUT(vk, 0, 0, 0, None); seq.append(d)
    up = INPUT(type=INPUT_KEYBOARD); up.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None); seq.append(up)
    if alt:
        au = INPUT(type=INPUT_KEYBOARD); au.u.ki = KEYBDINPUT(0x12, 0, KEYEVENTF_KEYUP, 0, None); seq.append(au)
    _send(*seq)


def type_text(s):
    for ch in s:
        d = INPUT(type=INPUT_KEYBOARD); d.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, None)
        up = INPUT(type=INPUT_KEYBOARD); up.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
        _send(d, up)
        time.sleep(0.008)


# --- 스크린샷 ---
class BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def shot(rect, path):
    x, y, w, h = rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
    hdc = u.GetDC(0); mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h); g.SelectObject(mdc, bmp)
    g.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)
    buf = ctypes.create_string_buffer(w * h * 4)
    bi = BIH(); bi.biSize = ctypes.sizeof(BIH); bi.biWidth = w; bi.biHeight = -h
    bi.biPlanes = 1; bi.biBitCount = 32
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    g.DeleteObject(bmp); g.DeleteDC(mdc); u.ReleaseDC(0, hdc)
    with open(path, "wb") as f:
        f.write(b"BM" + struct.pack("<IHHI", 14 + 40 + len(buf), 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, len(buf), 0, 0, 0, 0))
        f.write(buf.raw)
    return path


# --- 클릭 안전장치 -------------------------------------------------
def window_at(x, y):
    """해당 화면 좌표에서 실제로 '보이는' 창 핸들 (가장 위에 있는 것)."""
    p = wt.POINT(x, y)
    return u.WindowFromPoint(p)


def is_descendant(child, ancestor):
    h = child
    for _ in range(30):
        if h == ancestor:
            return True
        h = u.GetAncestor(h, 1)  # GA_PARENT
        if not h:
            return False
    return False


def bring_to_front(hwnd):
    """대상 창을 최상단으로. 성공 여부를 bool로 반환.

    주의: SW_RESTORE(9)를 무조건 호출하면 '최대화된 창'까지 창모드로
    되돌려버린다. 실제로 샵마인 메인 창을 창모드로 바꿔서 레이아웃 좌표가
    전부 어긋난 적이 있다. 최소화된 창만 복원한다.
    """
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)      # SW_RESTORE - 최소화된 경우에만
    u.SetForegroundWindow(hwnd)
    time.sleep(0.35)
    fg = u.GetForegroundWindow()
    return fg == hwnd or is_descendant(fg, hwnd)


def safe_click(hwnd, rel, expect_color=None, tol=28, label=""):
    """창 상대좌표를 클릭하되, 그 자리가 정말 대상 창인지 먼저 확인한다.

    반환: (성공여부, 메시지)
    """
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    ax, ay = r.left + rel[0], r.top + rel[1]

    top = window_at(ax, ay)
    if not is_descendant(top, hwnd):
        return False, (f"[{label}] 클릭 지점 ({ax},{ay})을 다른 창이 덮고 있음 "
                       f"(그 자리 창={top!r}, 제목={_text(u.GetAncestor(top,2))!r}) - 클릭 중단")

    if expect_color is not None:
        # 버튼 안 글자/아이콘 픽셀에 걸릴 수 있으므로 한 점이 아니라
        # 주변을 격자로 훑어 '다수가 버튼 색'인지로 판정한다.
        hits = total = 0
        samples = []
        for dy in (-12, -6, 0, 6, 12):
            for dx in (-24, -12, 0, 12, 24):
                c = pixel(ax + dx, ay + dy)
                samples.append(c)
                total += 1
                if close_to(c, expect_color, tol):
                    hits += 1
        if hits * 2 < total:
            return False, (f"[{label}] 버튼 색이 아님: {hits}/{total}개만 일치 "
                           f"(기대 {expect_color}, 예: {samples[:3]}) at ({ax},{ay}) - 클릭 중단")

    try:
        move_click(ax, ay)
    except RuntimeError as e:
        return False, f"[{label}] {e}"
    return True, f"[{label}] 클릭 완료 ({ax},{ay})"


# --- 표준 대화상자(#32770) 헬퍼 -------------------------------------
# 표준 대화상자는 컨트롤 ID가 고정이라 좌표를 쓸 필요가 없다.
DLG_OK, DLG_CANCEL, DLG_YES, DLG_NO = 1, 2, 6, 7


def dlg_button(hwnd, ctrl_id):
    """대화상자의 버튼 핸들과 화면 중심좌표를 반환한다."""
    btn = u.GetDlgItem(hwnd, ctrl_id)
    if not btn:
        return None, None
    r = wt.RECT()
    u.GetWindowRect(btn, ctypes.byref(r))
    return btn, ((r.left + r.right) // 2, (r.top + r.bottom) // 2)


def click_dlg_button(hwnd, ctrl_id, label=""):
    """대화상자 버튼을 컨트롤 ID로 찾아 클릭한다."""
    btn, center = dlg_button(hwnd, ctrl_id)
    if btn is None:
        return False, f"[{label}] 컨트롤 id={ctrl_id} 없음"
    caption = _text(btn)
    if not bring_to_front(hwnd):
        return False, f"[{label}] 대화상자를 앞으로 가져오지 못함"
    time.sleep(0.25)
    try:
        move_click(*center)
    except RuntimeError as e:
        return False, f"[{label}] {e}"
    return True, f"[{label}] '{caption}' 클릭"


BM_CLICK = 0x00F5


def press_button(hwnd):
    """HWND를 가진 버튼/체크박스를 '커서를 쓰지 않고' 누른다.

    좌표 클릭은 실행 중에 사람이 마우스를 건드리면 커서가 목표에 도달하지 못해
    중단된다. 실제로 70건짜리 실행이 [전체선택] 직전에 그렇게 멈췄다. 컨트롤이
    HWND를 갖고 있다면 BM_CLICK 한 방이 더 정확하고 마우스와 무관하다
    (WinForms 버튼도 OWNERDRAW지만 BM_CLICK은 정상 처리한다 - 실측 확인).

    좌표 클릭이 여전히 필요한 곳: HWND가 없는 툴바 버튼([송장수정모드],
    [송장번호수정]), WebView2 창의 버튼, 그리고 그리드 행 체크박스.

    SendMessage 가 아니라 PostMessage 를 쓴다. SendMessage 는 대상이 메시지를
    다 처리할 때까지 돌아오지 않는데, 버튼이 모달 대화상자를 띄우면 그 창이
    닫힐 때까지 영영 멈춘다 ([찾아보기]에서 실제로 그렇게 걸렸다).
    """
    u.PostMessageW(hwnd, BM_CLICK, 0, 0)


def ctrl_key(vk):
    """Ctrl + <키>."""
    c = INPUT(type=INPUT_KEYBOARD); c.u.ki = KEYBDINPUT(0x11, 0, 0, 0, None)
    d = INPUT(type=INPUT_KEYBOARD); d.u.ki = KEYBDINPUT(vk, 0, 0, 0, None)
    du = INPUT(type=INPUT_KEYBOARD); du.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)
    cu = INPUT(type=INPUT_KEYBOARD); cu.u.ki = KEYBDINPUT(0x11, 0, KEYEVENTF_KEYUP, 0, None)
    _send(c, d, du, cu)


def wait_for_window(title_equals=None, title_startswith=None, timeout=20.0, poll=0.4):
    """조건에 맞는 창이 뜰 때까지 기다린다. (hwnd, title, rect) 또는 None."""
    end = time.time() + timeout
    while time.time() < end:
        w = find_windows(title_equals=title_equals, title_startswith=title_startswith)
        if w:
            return w[0]
        time.sleep(poll)
    return None


def wait_for_window_gone(title_equals=None, timeout=20.0, poll=0.4):
    """해당 창이 사라질 때까지 기다린다."""
    end = time.time() + timeout
    while time.time() < end:
        if not find_windows(title_equals=title_equals):
            return True
        time.sleep(poll)
    return False


# --- 화면에서 버튼을 '색'으로 찾기 ----------------------------------
# WebView2로 그린 창(엑셀 파일 생성 등)은 버튼이 HWND를 갖지 않아서
# 컨트롤로 찾을 수 없다. 좌표를 하드코딩하는 대신 매번 색으로 찾으면
# 창 크기나 레이아웃이 바뀌어도 따라간다.

def read_bmp32(path):
    """shot()이 저장한 32bpp BMP를 (w, h, BGRA bytes, top-down)로 읽는다."""
    with open(path, "rb") as f:
        data = f.read()
    off = struct.unpack_from("<I", data, 10)[0]
    w, h = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    if bpp != 32:
        raise ValueError(f"32bpp가 아님: {bpp}")
    top_down = h < 0
    h = abs(h)
    px = data[off:off + w * h * 4]
    if not top_down:
        rows = [px[y * w * 4:(y + 1) * w * 4] for y in range(h)][::-1]
        px = b"".join(rows)
    return w, h, px


def find_color_button(w, h, px, target, tol=45, min_pixels=600):
    """target 색 덩어리들의 (x1,y1,x2,y2,중심x,중심y,픽셀수) 목록을 크기순으로."""
    tr, tg, tb = target
    mask = bytearray(w * h)
    for i in range(w * h):
        b, g_, r_ = px[i * 4], px[i * 4 + 1], px[i * 4 + 2]
        if abs(r_ - tr) <= tol and abs(g_ - tg) <= tol and abs(b - tb) <= tol:
            mask[i] = 1
    seen = bytearray(w * h)
    blobs = []
    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        stack = [start]; seen[start] = 1
        x1 = x2 = start % w; y1 = y2 = start // w; n = 0
        while stack:
            i = stack.pop(); n += 1
            x, y = i % w, i // w
            if x < x1: x1 = x
            if x > x2: x2 = x
            if y < y1: y1 = y
            if y > y2: y2 = y
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    if mask[j] and not seen[j]:
                        seen[j] = 1; stack.append(j)
        if n >= min_pixels:
            blobs.append((x1, y1, x2, y2, (x1 + x2) // 2, (y1 + y2) // 2, n))
    blobs.sort(key=lambda b: -b[6])
    return blobs


def locate_button_by_color(hwnd, color, tmp_path, tol=45, min_pixels=600):
    """창을 캡처해서 해당 색 버튼의 '창 상대좌표 중심'을 찾는다."""
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    shot((r.left, r.top, r.right, r.bottom), tmp_path)
    w, h, px = read_bmp32(tmp_path)
    blobs = find_color_button(w, h, px, color, tol=tol, min_pixels=min_pixels)
    if not blobs:
        return None
    x1, y1, x2, y2, cx, cy, n = blobs[0]
    return (cx, cy), (x1, y1, x2, y2), n


# --- 다른 프로세스의 컨트롤 텍스트 읽기/쓰기 ------------------------
# 주의: GetWindowTextW 는 '다른 프로세스'의 컨트롤에 대해서는 창 제목만
# 반환하고 EDIT 내용은 빈 문자열을 준다. 실제로 이것 때문에 값이 잘
# 들어갔는데도 "안 들어갔다"고 오판한 적이 있다. WM_GETTEXT를 직접 보낸다.
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C

u.SendMessageW.restype = wt.LPARAM


def ctrl_text(hwnd):
    """다른 프로세스의 컨트롤 텍스트를 WM_GETTEXT로 읽는다."""
    n = u.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(int(n) + 1)
    u.SendMessageW(hwnd, WM_GETTEXT, int(n) + 1, ctypes.cast(buf, ctypes.c_void_p))
    return buf.value


def set_ctrl_text(hwnd, value):
    """컨트롤 텍스트를 WM_SETTEXT로 설정하고, 실제로 반영됐는지 확인한다."""
    buf = ctypes.create_unicode_buffer(value)
    u.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p))
    time.sleep(0.2)
    return ctrl_text(hwnd) == value
