"""'엑셀 파일 생성' 창(WebView2)의 화면 요소를 **읽기만** 하는 도구.

샵마인에서 이 창 하나만 WebView2로 그려져 있어 버튼/입력칸이 HWND를 갖지
않는다. 지금까지는 [엑셀 파일 생성] 버튼을 '색'으로 찾아 클릭해서 넘겼지만,
'엑셀 양식' 선택칸은 색이 아니라 **글자**를 읽어야 한다 (지금 어떤 양식이
골라져 있는지, 목록의 어느 줄이 '송장 자동화'인지).

그래서 이 창에 한해 MSAA(oleacc)로 접근성 트리를 읽는다. 화면을 그리는
쪽이 크롬(WebView2)이라 콤보박스의 현재 값과 목록 항목의 이름/좌표가 그대로
나온다. 좌표를 세지 않아도 되고, 목록 순서가 바뀌어도 이름으로 찾는다.

**주의 - 이 모듈을 샵마인 메인 창에 쓰지 말 것.**
메인 창의 DataGridView를 UI Automation으로 조회하면 앱이 반복해서 죽었다
(project-shopmine-no-ui-automation). 이 창이 안전한 이유는 읽는 대상이
샵마인(.NET)이 아니라 **별도 프로세스인 msedgewebview2.exe가 그린 화면**이기
때문이다. 2026-08-28에 이 창을 여러 번 읽어도 앱이 멀쩡함을 확인했다.

읽는 것만 여기서 하고, 실제 조작(클릭/키 입력)은 지금까지처럼 winui 로 한다.

**믿을 수 있는 것과 없는 것** (2026-08-28 실측):
- 콤보박스의 **현재 값**(accValue)은 언제 읽어도 정확했다. 목록이 펼쳐진
  중에도 지금 가리키는 줄로 즉시 갱신된다.
- 펼쳐진 목록의 **줄 목록**(role 34)은 나올 때도 있고 안 나올 때도 있다.
  같은 코드로 20개가 읽히다가 다음 창에서는 0개가 읽혔다 (목록은 크롬이
  띄우는 별도 팝업 창이라 접근성 트리에 늦게/안 실려온다). 그래서
  export.ensure_template 은 줄 좌표를 클릭하지 않고 **이름을 타이핑**해서
  고르고, 값으로 확인한다. 줄 목록은 오류 메시지용으로만 쓴다.
"""

from __future__ import annotations

import ctypes

OBJID_CLIENT = 0xFFFFFFFC

# MSAA 역할 코드 (oleacc.h)
ROLE_LIST = 33
ROLE_LISTITEM = 34
ROLE_COMBOBOX = 46

_iaccessible = None


def _iface():
    """IAccessible 인터페이스 정의. 첫 호출 때 comtypes 가 oleacc.dll 에서 생성한다."""
    global _iaccessible
    if _iaccessible is None:
        import comtypes.client
        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen import Accessibility
        _iaccessible = Accessibility.IAccessible
    return _iaccessible


def from_window(hwnd):
    """창 핸들의 접근성 루트 요소. 실패하면 None."""
    iface = _iface()
    p = ctypes.POINTER(iface)()
    try:
        ctypes.oledll.oleacc.AccessibleObjectFromWindow(
            hwnd, OBJID_CLIENT, ctypes.byref(iface._iid_), ctypes.byref(p))
    except OSError:
        return None
    return p


def children(el):
    """자식 요소들. 자기 HWND/객체가 없는 '단순 요소'(정수 childId)는 건너뛴다.

    WebView2 트리는 모든 노드를 객체로 주므로 실제로 걸러지는 것은 없다.
    """
    import comtypes
    from comtypes.automation import VARIANT
    try:
        n = el.accChildCount
    except Exception:
        return []
    if not n:
        return []
    arr = (VARIANT * n)()
    got = ctypes.c_long()
    try:
        ctypes.oledll.oleacc.AccessibleChildren(el, 0, n, arr, ctypes.byref(got))
    except OSError:
        return []
    out = []
    for i in range(got.value):
        v = arr[i].value
        if isinstance(v, comtypes.IUnknown):
            try:
                out.append(v.QueryInterface(_iface()))
            except Exception:
                pass
    return out


def _prop(el, getter):
    try:
        return getter()
    except Exception:
        return None


def name(el):
    return _prop(el, lambda: el.accName(0))


def value(el):
    return _prop(el, lambda: el.accValue(0))


def role(el):
    return _prop(el, lambda: el.accRole(0))


def location(el):
    """화면 절대좌표 (left, top, width, height). 실패하면 None."""
    r = _prop(el, lambda: el.accLocation(0))
    if r is None:
        return None
    return tuple(int(v) for v in r)


def center(el):
    """요소의 화면 절대 중심좌표. 크기가 0이면 None."""
    box = location(el)
    if box is None or box[2] <= 0 or box[3] <= 0:
        return None
    return box[0] + box[2] // 2, box[1] + box[3] // 2


def walk(el, max_depth=25, _depth=0):
    """요소와 그 아래 전부를 훑는다."""
    yield el
    if _depth >= max_depth:
        return
    for k in children(el):
        yield from walk(k, max_depth, _depth + 1)


def find(root, predicate, max_depth=25):
    """조건에 맞는 첫 요소. 없으면 None."""
    for el in walk(root, max_depth):
        if predicate(el):
            return el
    return None


def find_all(root, predicate, max_depth=25):
    return [el for el in walk(root, max_depth) if predicate(el)]
