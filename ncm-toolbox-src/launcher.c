/*
 * 网易云音乐工具箱 启动器
 *
 * 存在的意义：让 Release 里解压出来就能双击运行，而不是只能靠 .bat
 * （.bat 在 cmd 下对行尾敏感，LF-only 会被吃掉行首字符，已经踩过一次）。
 *
 * 它只做三件事：定位自己所在的目录 → 拉起内置的 python\pythonw.exe 跑 ncm_gui.py → 退出。
 * 带 --debug 参数时改用 python.exe 并保留控制台，方便看报错。
 *
 * 编译（VS 2022 Build Tools，x64 本机工具提示符里）：
 *   cl /nologo /W3 /O2 /MT /utf-8 launcher.c /Fe:launcher.exe ^
 *      /link /SUBSYSTEM:WINDOWS user32.lib kernel32.lib
 */

#include <windows.h>

static BOOL file_exists(const wchar_t *p)
{
    DWORD a = GetFileAttributesW(p);
    return (a != INVALID_FILE_ATTRIBUTES) && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static void spawn(const wchar_t *exe, const wchar_t *script,
                  const wchar_t *dir, BOOL new_console)
{
    wchar_t cmd[32768];
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;

    wsprintfW(cmd, L"\"%s\" \"%s\"", exe, script);
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    CreateProcessW(exe, cmd, NULL, NULL, FALSE,
                   new_console ? CREATE_NEW_CONSOLE : 0,
                   NULL, dir, &si, &pi);
    if (pi.hProcess) CloseHandle(pi.hProcess);
    if (pi.hThread)  CloseHandle(pi.hThread);
}

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, LPWSTR lpCmdLine, int nShow)
{
    wchar_t base[MAX_PATH], pyw[MAX_PATH], py[MAX_PATH], script[MAX_PATH];
    wchar_t *slash;
    BOOL debug = (lpCmdLine && *lpCmdLine);

    (void)hInst; (void)hPrev; (void)nShow;

    if (!GetModuleFileNameW(NULL, base, MAX_PATH)) return 1;
    slash = wcsrchr(base, L'\\');
    if (!slash) return 1;
    *slash = L'\0';                                   /* 只留目录部分 */

    wsprintfW(script, L"%s\\ncm_gui.py", base);
    wsprintfW(pyw,    L"%s\\python\\pythonw.exe", base);
    wsprintfW(py,     L"%s\\python\\python.exe", base);

    if (!file_exists(script)) {
        MessageBoxW(NULL,
            L"找不到主程序 ncm_gui.py。\n\n"
            L"请确认整个文件夹被完整解压，不要把文件拆开放在不同目录。",
            L"网易云音乐工具箱", MB_OK | MB_ICONERROR);
        return 1;
    }

    /* 带参数启动 = 调试模式，用 python.exe 并保留控制台 */
    if (debug) {
        if (!file_exists(py)) {
            MessageBoxW(NULL,
                L"找不到内置运行时 python\\python.exe。\n\n"
                L"请确认整个文件夹被完整解压，不要只复制部分文件。",
                L"网易云音乐工具箱", MB_OK | MB_ICONERROR);
            return 1;
        }
        spawn(py, script, base, TRUE);
        return 0;
    }

    if (!file_exists(pyw)) {
        MessageBoxW(NULL,
            L"找不到内置运行时 python\\pythonw.exe。\n\n"
            L"请确认整个文件夹被完整解压，不要只复制部分文件。",
            L"网易云音乐工具箱", MB_OK | MB_ICONERROR);
        return 1;
    }
    spawn(pyw, script, base, FALSE);
    return 0;
}
