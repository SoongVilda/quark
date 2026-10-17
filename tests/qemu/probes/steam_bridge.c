/* The Steam bridge without a Steam client: lsteamclient must load as a
 * builtin and expose the Steamworks entry point, and asking it for an
 * interface must fail cleanly instead of crashing the process. */
#include "probe.h"

typedef void *(*create_interface_fn)(const char *version, int *ret);

int main(void)
{
    HMODULE mod = LoadLibraryA("lsteamclient.dll");
    PROBE_REQUIRE(mod, "LoadLibrary(lsteamclient.dll) failed (%lu)", GetLastError());

    create_interface_fn create = (create_interface_fn)GetProcAddress(mod, "CreateInterface");
    PROBE_REQUIRE(create, "lsteamclient.dll exports no CreateInterface");

    int ret = 0;
    void *iface = create("SteamClient020", &ret);
    probe_verdict(1, "loaded, CreateInterface(SteamClient020) returned %s with no Steam running",
                  iface ? "an interface" : "NULL");
    return 0;
}
