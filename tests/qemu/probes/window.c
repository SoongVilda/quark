/* A window and message pump: create and show a window, post a message to
 * it, take a timer tick and see both arrive through GetMessage. */
#include "probe.h"

#define PROBE_MSG (WM_APP + 1)

static int got_posted, got_timer;

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == PROBE_MSG && w == 0x51 && l == 0x7a) {
        got_posted = 1;
        return 0;
    }
    if (m == WM_TIMER) {
        got_timer = 1;
        KillTimer(h, 1);
        return 0;
    }
    return DefWindowProcA(h, m, w, l);
}

int main(void)
{
    WNDCLASSA wc = {0};
    wc.lpfnWndProc = wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "quark_probe_window";
    PROBE_REQUIRE(RegisterClassA(&wc), "RegisterClass failed (%lu)", GetLastError());
    HWND hwnd = CreateWindowA(wc.lpszClassName, "probe", WS_OVERLAPPEDWINDOW,
                              0, 0, 200, 100, NULL, NULL, wc.hInstance, NULL);
    PROBE_REQUIRE(hwnd, "CreateWindow failed (%lu)", GetLastError());
    ShowWindow(hwnd, SW_SHOW);
    PROBE_REQUIRE(PostMessageA(hwnd, PROBE_MSG, 0x51, 0x7a), "PostMessage failed (%lu)", GetLastError());
    PROBE_REQUIRE(SetTimer(hwnd, 1, 50, NULL), "SetTimer failed (%lu)", GetLastError());

    DWORD deadline = GetTickCount() + 5000;
    MSG msg;
    while (!(got_posted && got_timer) && GetTickCount() < deadline) {
        if (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        } else {
            MsgWaitForMultipleObjects(0, NULL, FALSE, 50, QS_ALLINPUT);
        }
    }
    PROBE_REQUIRE(got_posted, "posted message never arrived");
    PROBE_REQUIRE(got_timer, "WM_TIMER never arrived within 5s");
    DestroyWindow(hwnd);
    probe_verdict(1, "posted message and timer delivered");
    return 0;
}
