/* Stress for the cross-game heap corruption (#18): the GLX present path on
 * winex11's shared display, an audio client and 160-byte allocation churn,
 * all concurrent. The verdict is PASS when the budget runs out with the
 * process alive; a corrupted heap aborts before a verdict is written.
 *
 * The corruption victim was a ~160-byte libX11 XCreateGC chunk in a fresh
 * glibc arena, smashed at +160, aborting in win32u free_dce / glibc sysmalloc,
 * on Wine-internal threads.
 *
 * Common denominator across all six crashing games (incl. Balatro = pure
 * OpenGL): the GLX/OpenGL present path driven on winex11's shared gdi_display
 * from multiple threads with little display locking. This repro hammers that:
 * N threads each own a GL context on a window and loop glClear + SwapBuffers,
 * concurrent with an mmdevapi audio client thread and 160-byte malloc churn.
 *
 * Run as a probe by tests/qemu/suite.py, or in a repro loop under quark
 * directly (no Steam) via tests/heap_repro/run.py.
 *
 * Time-bounded loops so a winex11 wedge can't hang the run.
 */
#define COBJMACROS
#define _WIN32_WINNT 0x0600
#include "probe.h"
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <GL/gl.h>

#define N_GL_THREADS 12
#define RUN_MS 20000   /* wall-clock budget per thread */

static volatile LONG g_go = 0;

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l) {
    return DefWindowProcA(h, m, w, l);
}

/* GL/GLX churn on the shared gdi_display: create a window + GL context, render,
 * tear down, repeat. Each create/destroy churns winex11's GLX drawable + the
 * GC create/free on gdi_display, concurrently across threads. */
static DWORD WINAPI gl_worker(LPVOID arg) {
    (void)arg;
    while (!g_go) Sleep(0);
    char clsname[32];
    snprintf(clsname, sizeof clsname, "gl%lu", GetCurrentThreadId());
    WNDCLASSA wc = {0};
    wc.lpfnWndProc = wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = clsname;
    wc.style = CS_OWNDC;
    RegisterClassA(&wc);

    PIXELFORMATDESCRIPTOR pfd = {0};
    pfd.nSize = sizeof(pfd);
    pfd.nVersion = 1;
    pfd.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    pfd.iPixelType = PFD_TYPE_RGBA;
    pfd.cColorBits = 32;
    pfd.cDepthBits = 24;

    DWORD start = GetTickCount();
    while (GetTickCount() - start < RUN_MS) {
        HWND hw = CreateWindowExA(0, clsname, "gl", WS_POPUP,
                                  0, 0, 64, 64, NULL, NULL, wc.hInstance, NULL);
        if (!hw) continue;
        HDC dc = GetDC(hw);
        if (dc) {
            int pf = ChoosePixelFormat(dc, &pfd);
            if (pf) SetPixelFormat(dc, pf, &pfd);
            HGLRC rc = wglCreateContext(dc);     /* GLX context on gdi_display */
            if (rc) {
                if (wglMakeCurrent(dc, rc)) {
                    for (int f = 0; f < 30; f++) {
                        glClearColor((f & 1) ? 1.f : 0.f, 0.2f, 0.3f, 1.f);
                        glClear(GL_COLOR_BUFFER_BIT);
                        glFlush();
                        SwapBuffers(dc);          /* GLX present path */
                    }
                    wglMakeCurrent(NULL, NULL);
                }
                wglDeleteContext(rc);
            }
            ReleaseDC(hw, dc);
        }
        DestroyWindow(hw);
        void *p = malloc(160);
        if (p) { memset(p, 0xAB, 160); free(p); }
    }
    return 0;
}

static DWORD WINAPI audio_worker(LPVOID arg) {
    (void)arg;
    CoInitializeEx(NULL, COINIT_MULTITHREADED);
    IMMDeviceEnumerator *en = NULL;
    if (CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL,
                         &IID_IMMDeviceEnumerator, (void **)&en) == S_OK && en) {
        IMMDevice *dev = NULL;
        if (IMMDeviceEnumerator_GetDefaultAudioEndpoint(en, eRender, eConsole, &dev) == S_OK && dev) {
            IAudioClient *ac = NULL;
            if (IMMDevice_Activate(dev, &IID_IAudioClient, CLSCTX_ALL, NULL, (void **)&ac) == S_OK && ac) {
                WAVEFORMATEX *fmt = NULL;
                if (IAudioClient_GetMixFormat(ac, &fmt) == S_OK && fmt) {
                    IAudioClient_Initialize(ac, AUDCLNT_SHAREMODE_SHARED, 0,
                                            10000000, 0, fmt, NULL);
                    IAudioClient_Start(ac);
                    CoTaskMemFree(fmt);
                }
            }
            IMMDevice_Release(dev);
        }
        IMMDeviceEnumerator_Release(en);
    }
    DWORD start = GetTickCount();
    while (GetTickCount() - start < RUN_MS) {
        void *p = malloc(160);
        if (p) { memset(p, 0xCD, 160); free(p); }
        Sleep(0);
    }
    return 0;
}

int main(void) {
    HANDLE th[N_GL_THREADS + 1];
    th[0] = CreateThread(NULL, 0, audio_worker, NULL, 0, NULL);
    for (int i = 0; i < N_GL_THREADS; i++)
        th[i + 1] = CreateThread(NULL, 0, gl_worker, NULL, 0, NULL);
    InterlockedExchange(&g_go, 1);
    DWORD w = WaitForMultipleObjects(N_GL_THREADS + 1, th, TRUE, RUN_MS + 30000);
    PROBE_REQUIRE(w < WAIT_OBJECT_0 + N_GL_THREADS + 1,
                  "worker threads still running %d ms past their budget", 30000);
    probe_verdict(1, "%d GL threads and an audio client for %d ms with no heap abort",
                  N_GL_THREADS, RUN_MS);
}
