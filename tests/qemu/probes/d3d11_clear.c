/* D3D11 through DXVK: clear a swap-chain back buffer to a known color,
 * copy it to a staging texture and read the pixel back. */
#define COBJMACROS
#include "probe.h"
#include <initguid.h>
#include <d3d11.h>

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    return DefWindowProcA(h, m, w, l);
}

/* not near(): windows.h defines near as an empty macro */
static int close_to(int got, int want)
{
    return abs(got - want) <= 2;
}

int main(void)
{
    WNDCLASSA wc = {0};
    wc.lpfnWndProc = wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "quark_probe_d3d11";
    PROBE_REQUIRE(RegisterClassA(&wc), "RegisterClass failed (%lu)", GetLastError());
    HWND hwnd = CreateWindowA(wc.lpszClassName, "probe", WS_OVERLAPPEDWINDOW,
                              0, 0, 64, 64, NULL, NULL, wc.hInstance, NULL);
    PROBE_REQUIRE(hwnd, "CreateWindow failed (%lu)", GetLastError());

    DXGI_SWAP_CHAIN_DESC sd = {0};
    sd.BufferCount = 1;
    sd.BufferDesc.Width = 64;
    sd.BufferDesc.Height = 64;
    sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = hwnd;
    sd.SampleDesc.Count = 1;
    sd.Windowed = TRUE;

    IDXGISwapChain *swap = NULL;
    ID3D11Device *dev = NULL;
    ID3D11DeviceContext *ctx = NULL;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0,
                                               NULL, 0, D3D11_SDK_VERSION, &sd,
                                               &swap, &dev, NULL, &ctx);
    PROBE_REQUIRE(SUCCEEDED(hr), "D3D11CreateDeviceAndSwapChain hr=0x%08lx", hr);

    ID3D11Texture2D *back = NULL;
    hr = IDXGISwapChain_GetBuffer(swap, 0, &IID_ID3D11Texture2D, (void **)&back);
    PROBE_REQUIRE(SUCCEEDED(hr), "GetBuffer hr=0x%08lx", hr);

    ID3D11RenderTargetView *rtv = NULL;
    hr = ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource *)back, NULL, &rtv);
    PROBE_REQUIRE(SUCCEEDED(hr), "CreateRenderTargetView hr=0x%08lx", hr);

    const float color[4] = {0.25f, 0.5f, 0.75f, 1.0f};
    ID3D11DeviceContext_ClearRenderTargetView(ctx, rtv, color);

    D3D11_TEXTURE2D_DESC td;
    ID3D11Texture2D_GetDesc(back, &td);
    td.Usage = D3D11_USAGE_STAGING;
    td.BindFlags = 0;
    td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    td.MiscFlags = 0;
    ID3D11Texture2D *staging = NULL;
    hr = ID3D11Device_CreateTexture2D(dev, &td, NULL, &staging);
    PROBE_REQUIRE(SUCCEEDED(hr), "CreateTexture2D (staging) hr=0x%08lx", hr);
    ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource *)staging, (ID3D11Resource *)back);

    D3D11_MAPPED_SUBRESOURCE map;
    hr = ID3D11DeviceContext_Map(ctx, (ID3D11Resource *)staging, 0, D3D11_MAP_READ, 0, &map);
    PROBE_REQUIRE(SUCCEEDED(hr), "Map hr=0x%08lx", hr);
    const unsigned char *px = map.pData;
    int r = px[0], g = px[1], b = px[2], a = px[3];
    ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource *)staging, 0);

    PROBE_REQUIRE(close_to(r, 64) && close_to(g, 128) && close_to(b, 191) && close_to(a, 255),
                  "pixel (%d,%d,%d,%d), want (64,128,191,255)", r, g, b, a);
    probe_verdict(1, "cleared and read back (%d,%d,%d,%d)", r, g, b, a);
    return 0;
}
