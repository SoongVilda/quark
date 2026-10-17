/* The Windows environment quark builds: TEMP that resolves to a real
 * directory, the user profile paths and the Steam registry keys the
 * launcher injects. */
#include "probe.h"

static void require_dir_var(const char *name)
{
    char value[MAX_PATH];
    DWORD n = GetEnvironmentVariableA(name, value, sizeof value);
    PROBE_REQUIRE(n > 0 && n < sizeof value, "%s unset", name);
    DWORD attrs = GetFileAttributesA(value);
    PROBE_REQUIRE(attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY),
                  "%s=%s is not a directory", name, value);
}

int main(void)
{
    require_dir_var("TEMP");
    require_dir_var("TMP");
    require_dir_var("USERPROFILE");
    require_dir_var("APPDATA");
    require_dir_var("LOCALAPPDATA");

    char tmp[MAX_PATH], file[MAX_PATH];
    PROBE_REQUIRE(GetTempPathA(sizeof tmp, tmp) > 0, "GetTempPath failed (%lu)", GetLastError());
    PROBE_REQUIRE(GetTempFileNameA(tmp, "qrk", 0, file), "GetTempFileName in %s failed (%lu)", tmp, GetLastError());
    DeleteFileA(file);

    HKEY key;
    LONG rc = RegOpenKeyExA(HKEY_CURRENT_USER, "Software\\Valve\\Steam\\ActiveProcess", 0, KEY_READ, &key);
    PROBE_REQUIRE(rc == ERROR_SUCCESS, "HKCU\\Software\\Valve\\Steam\\ActiveProcess missing (%ld)", rc);
    DWORD pid = 0, size = sizeof pid, type = 0;
    rc = RegQueryValueExA(key, "PID", NULL, &type, (BYTE *)&pid, &size);
    RegCloseKey(key);
    PROBE_REQUIRE(rc == ERROR_SUCCESS && type == REG_DWORD,
                  "ActiveProcess\\PID rc=%ld type=%lu", rc, type);
    /* steam.exe replaces the 0xfffe placeholder with its own PID; either way
     * a game checks that Steam is alive by opening it. */
    HANDLE steam = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    PROBE_REQUIRE(steam, "OpenProcess(ActiveProcess\\PID 0x%lx) failed (%lu)", pid, GetLastError());
    CloseHandle(steam);

    probe_verdict(1, "TEMP, profile paths and Steam ActiveProcess keys in place");
    return 0;
}
