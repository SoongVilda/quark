/* Audio device enumeration through mmdevapi, and an IAudioClient that
 * initializes on the default render endpoint. */
#define COBJMACROS
#include "probe.h"
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audioclient.h>

int main(void)
{
    HRESULT hr = CoInitializeEx(NULL, COINIT_MULTITHREADED);
    PROBE_REQUIRE(SUCCEEDED(hr), "CoInitializeEx hr=0x%08lx", hr);

    IMMDeviceEnumerator *en = NULL;
    hr = CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL,
                          &IID_IMMDeviceEnumerator, (void **)&en);
    PROBE_REQUIRE(SUCCEEDED(hr), "CoCreateInstance(MMDeviceEnumerator) hr=0x%08lx", hr);

    IMMDeviceCollection *devices = NULL;
    hr = IMMDeviceEnumerator_EnumAudioEndpoints(en, eRender, DEVICE_STATE_ACTIVE, &devices);
    PROBE_REQUIRE(SUCCEEDED(hr), "EnumAudioEndpoints hr=0x%08lx", hr);
    UINT count = 0;
    IMMDeviceCollection_GetCount(devices, &count);
    PROBE_REQUIRE(count > 0, "no active render endpoints");

    IMMDevice *dev = NULL;
    hr = IMMDeviceEnumerator_GetDefaultAudioEndpoint(en, eRender, eConsole, &dev);
    PROBE_REQUIRE(SUCCEEDED(hr), "GetDefaultAudioEndpoint hr=0x%08lx", hr);

    IAudioClient *client = NULL;
    hr = IMMDevice_Activate(dev, &IID_IAudioClient, CLSCTX_ALL, NULL, (void **)&client);
    PROBE_REQUIRE(SUCCEEDED(hr), "Activate(IAudioClient) hr=0x%08lx", hr);

    WAVEFORMATEX *fmt = NULL;
    hr = IAudioClient_GetMixFormat(client, &fmt);
    PROBE_REQUIRE(SUCCEEDED(hr), "GetMixFormat hr=0x%08lx", hr);
    hr = IAudioClient_Initialize(client, AUDCLNT_SHAREMODE_SHARED, 0, 10000000, 0, fmt, NULL);
    PROBE_REQUIRE(SUCCEEDED(hr), "IAudioClient::Initialize hr=0x%08lx", hr);

    probe_verdict(1, "%u render endpoint(s), shared client at %lu Hz", count, fmt->nSamplesPerSec);
    return 0;
}
