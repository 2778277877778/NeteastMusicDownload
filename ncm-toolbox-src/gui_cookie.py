# -*- coding: utf-8 -*-
r"""
从本机浏览器读取网易云登录 Cookie。

原理（Chrome / Edge / Brave 等 Chromium 系一致）：
  * Cookie 值用 AES-256-GCM 加密存在 SQLite 里，密文前 3 字节是 v10/v11，后接 12 字节 nonce
  * 主密钥存在 <User Data>\Local State 的 os_crypt.encrypted_key，base64 后前 5 字节是 "DPAPI"
  * 去掉前缀剩下的用 Windows DPAPI 解出来就是 32 字节 AES 密钥（绑定当前用户）

注意两点：
  1. 浏览器运行时会独占锁定 Cookies 文件，此时读不了 —— 会抛出可读的提示，让用户关掉浏览器重试。
     所以读取是"复制一份再读"，绝不碰原始文件。
  2. 只挑域名含 163.com 的项，不碰其它站点。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time

from gui_common import APP_DIR

LOCAL = os.environ.get('LOCALAPPDATA', '')
ROAMING = os.environ.get('APPDATA', '')

# 常见 Chromium 系浏览器，顺序即优先级
BROWSERS = [
    ('Chrome', os.path.join(LOCAL, r'Google\Chrome\User Data')),
    ('Edge', os.path.join(LOCAL, r'Microsoft\Edge\User Data')),
    ('Brave', os.path.join(LOCAL, r'BraveSoftware\Brave-Browser\User Data')),
    ('Vivaldi', os.path.join(LOCAL, r'Vivaldi\User Data')),
    ('360极速', os.path.join(LOCAL, r'360Chrome\Chrome\User Data')),
    ('QQ浏览器', os.path.join(LOCAL, r'Tencent\QQBrowser\User Data')),
]

# 我们需要的键；MUSIC_U 是登录态本体，其余是配套
WANTED = ('MUSIC_U', '__csrf', 'NMTID', '_ntes_nuid')


class CookieReadError(Exception):
    """带用户可读原因的读取失败"""


class _BLOB(ctypes.Structure):
    _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(data):
    """Windows DPAPI 解密（密钥绑定当前用户，换用户/换机器解不开）"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise CookieReadError('DPAPI 解密失败（换过 Windows 用户或系统重装过？）')
    n = blob_out.cbData
    res = ctypes.create_string_buffer(n)
    ctypes.memmove(res, blob_out.pbData, n)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return res.raw


def _master_key(user_data_dir):
    path = os.path.join(user_data_dir, 'Local State')
    if not os.path.exists(path):
        raise CookieReadError('找不到 Local State')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            js = json.load(f)
        enc = base64.b64decode(js['os_crypt']['encrypted_key'])
    except KeyError:
        raise CookieReadError('Local State 里没有 os_crypt.encrypted_key'
                              '（可能是 Chrome 127+ 的应用绑定加密，暂不支持）')
    except Exception as e:
        raise CookieReadError('读取 Local State 失败：%s' % e)
    if enc[:5] != b'DPAPI':
        raise CookieReadError('密钥前缀不是 DPAPI')
    return _dpapi_unprotect(enc[5:])


# 新版 Chromium 在密文的明文前面加了 32 字节的「域绑定哈希」，
# 作用是让 Cookie 被复制到其它地方后失效。直接当值用的话，前面会是一段乱码 ——
# 现象很迷惑：长度只多 31，尾部还和真实值完全一致（GCM 校验是通过的，
# 因为密文本身没错，错的是我们没剥这个前缀）。
BINDING_PREFIX_LEN = 32


def _strip_binding(raw):
    """剥掉可能的域绑定哈希前缀。

    判据保守：前 32 字节不是 ASCII，且其后都是 ASCII —— 正常的 cookie 值
    都是可见 ASCII，所以这个判据不会误伤老格式。
    """
    if (len(raw) > BINDING_PREFIX_LEN
            and not raw[:BINDING_PREFIX_LEN].isascii()
            and raw[BINDING_PREFIX_LEN:].isascii()):
        return raw[BINDING_PREFIX_LEN:]
    return raw


def _decrypt_value(key, blob):
    from Cryptodome.Cipher import AES
    if not blob:
        return ''
    if blob[:3] in (b'v10', b'v11'):
        nonce, payload = blob[3:15], blob[15:]
        if len(payload) < 16:
            return ''
        raw = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
            payload[:-16], payload[-16:])
    else:
        # 很老的版本整块走 DPAPI
        raw = _dpapi_unprotect(blob)
    return _strip_binding(raw).decode('utf-8', 'replace')


def list_profiles(user_data_dir):
    out = []
    if os.path.isdir(os.path.join(user_data_dir, 'Default')):
        out.append('Default')
    try:
        for d in os.listdir(user_data_dir):
            if d.startswith('Profile ') and os.path.isdir(os.path.join(user_data_dir, d)):
                out.append(d)
    except OSError:
        pass
    return out


def _cookie_db(user_data_dir, profile):
    for rel in (os.path.join(profile, 'Network', 'Cookies'), os.path.join(profile, 'Cookies')):
        p = os.path.join(user_data_dir, rel)
        if os.path.exists(p):
            return p
    return None


def read_site_cookies(user_data_dir, profile='Default', domain='163.com'):
    """读某个配置里指定域名的 cookie，返回 {name: value}。

    先把库复制到临时目录再读：浏览器运行时锁住原文件，而且我们也不该直接动它。
    """
    db = _cookie_db(user_data_dir, profile)
    if not db:
        raise CookieReadError('找不到 Cookies 数据库')
    tmp = tempfile.mkdtemp(prefix='ncmck_')
    try:
        for suf in ('', '-wal', '-shm'):
            src = db + suf
            if not os.path.exists(src):
                continue
            try:
                # 用共享读方式打开，避免干扰正在运行的浏览器
                with open(src, 'rb') as f:
                    data = f.read()
            except PermissionError:
                raise CookieReadError(
                    'LOCKED:%s' % os.path.basename(os.path.dirname(os.path.dirname(db))))
            with open(os.path.join(tmp, 'Cookies' + suf), 'wb') as f:
                f.write(data)

        con = sqlite3.connect(os.path.join(tmp, 'Cookies'))
        try:
            rows = con.execute(
                'SELECT host_key, name, value, encrypted_value FROM cookies '
                'WHERE host_key LIKE ?', ('%' + domain + '%',)).fetchall()
        except sqlite3.DatabaseError as e:
            raise CookieReadError('Cookie 库格式异常：%s' % e)
        finally:
            con.close()

        if not rows:
            return {}
        key = _master_key(user_data_dir)
        out = {}
        for host, name, plain, enc in rows:
            try:
                out[name] = plain if (plain and not enc) else _decrypt_value(key, enc)
            except Exception:
                continue        # 单条解不开不影响其它
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_music_cookie(only=None):
    """在已安装的浏览器里找 MUSIC_U。

    返回 (cookie 串, 来源描述)；找不到时抛 CookieReadError，消息可直接展示给用户。
    """
    tried, locked = [], []

    # 先看我们自己的专用配置 —— 最可靠：它是我们建的，登录一次就长期有效，
    # 而且浏览器没跑时可以直接读文件，不需要启动任何东西。
    if not only and os.path.isdir(DEDICATED_PROFILE):
        try:
            ck = read_site_cookies(DEDICATED_PROFILE, 'Default')
            if ck.get('MUSIC_U'):
                s = cookie_from_pairs(ck)
                if cookie_is_usable(s):
                    return s, '专用浏览器配置（已保存的登录态）'
                tried.append('专用配置: 解出的值不可用（加密格式可能变了）')
            else:
                tried.append('专用配置: 尚未登录')
        except CookieReadError as e:
            if str(e).startswith('LOCKED:'):
                locked.append('专用浏览器')
            tried.append('专用配置: %s' % e)

    for name, base in BROWSERS:
        if only and name != only:
            continue
        if not os.path.isdir(base):
            continue
        for prof in list_profiles(base):
            try:
                ck = read_site_cookies(base, prof)
            except CookieReadError as e:
                if str(e).startswith('LOCKED:'):
                    locked.append(name)
                tried.append('%s/%s: %s' % (name, prof, e))
                continue
            if not ck:
                tried.append('%s/%s: 没有 163.com 的 Cookie' % (name, prof))
                continue
            if 'MUSIC_U' not in ck:
                tried.append('%s/%s: 有 163.com Cookie 但没有 MUSIC_U（可能未登录）'
                             % (name, prof))
                continue
            s = cookie_from_pairs(ck)
            if not cookie_is_usable(s):
                tried.append('%s/%s: 解出的 MUSIC_U 含非法字符' % (name, prof))
                continue
            return s, '%s（%s）' % (name, prof)

    if locked:
        raise CookieReadError(
            '%s 正在运行，Cookie 数据库被独占锁定，读不了。\n\n'
            '请先完全退出 %s（注意托盘图标也要退），再点「读取」。\n'
            '或者改用另一个浏览器登录。' % ('、'.join(sorted(set(locked))),
                                            '、'.join(sorted(set(locked)))))
    raise CookieReadError(
        '没有找到可用的登录态。\n\n'
        '请先在浏览器里打开 music.163.com 并登录，再回来点「读取」。\n\n'
        '检查过的位置：\n  ' + '\n  '.join(tried[:10]))

# =========================================================================== #
# 自适应方案二：自己启动一个带调试端口的浏览器，用 DevTools 协议读 Cookie
#
# 为什么需要它：读别人浏览器的 cookie 文件必然受制于文件锁（Edge 运行时就锁死，
# 实测连 SeBackupPrivilege + FILE_FLAG_BACKUP_SEMANTICS 都绕不过共享冲突）。
# 而由我们自己启动、使用独立配置目录的浏览器，可以直接通过 CDP 从浏览器内部拿
# Cookie —— 不碰文件、没有锁，而且登录态存在专用配置里，下次启动就是已登录状态。
# =========================================================================== #

DEDICATED_PROFILE = os.path.join(APP_DIR, 'browser_profile')

_BROWSER_EXES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    os.path.join(LOCAL, r'Google\Chrome\Application\chrome.exe'),
]


def find_browser_exe():
    for p in _BROWSER_EXES:
        if os.path.exists(p):
            return p
    return None


def _free_port(start=9333, end=9450):
    import socket
    for cand in range(start, end):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', cand))
            return cand
        except OSError:
            pass
        finally:
            s.close()
    raise CookieReadError('找不到可用端口')


class DebugBrowser(object):
    """自己启动的、带远程调试的浏览器；用 CDP 读 Cookie"""

    def __init__(self, profile_dir=None, url='https://music.163.com/#/login'):
        self.exe = find_browser_exe()
        if not self.exe:
            raise CookieReadError('没找到 Edge 或 Chrome，无法自动登录')
        self.profile = profile_dir or DEDICATED_PROFILE
        self.url = url
        self.port = None
        self.proc = None
        self._ws = None
        self._reused = False
        os.makedirs(self.profile, exist_ok=True)

    # ---------------------------------------------------------------- 启动
    def _port_file(self):
        """把上次用的调试端口记在专用配置里。

        不能用 wmic 去枚举进程找端口 —— wmic 在新版 Windows 已被移除，会直接失败。
        记端口 + 探测是否真的在服务，既不依赖系统工具，也天然避开了"端口被别的
        调试浏览器占用"的误判。
        """
        return os.path.join(self.profile, 'debug_port.txt')

    @staticmethod
    def _probe(port, timeout=2):
        import json as _json
        import urllib.request
        try:
            return _json.load(urllib.request.urlopen(
                'http://127.0.0.1:%d/json/version' % port, timeout=timeout))
        except Exception:
            return None

    def start(self, timeout=90):
        """启动浏览器并等调试端口就绪；若已有实例在跑则直接复用"""
        import subprocess

        # 1) 复用：读上次记下的端口并探测
        try:
            with open(self._port_file(), 'r', encoding='utf-8') as f:
                old = int(f.read().strip())
            ver = self._probe(old)
            if ver:
                self.port, self._reused = old, True
                return ver
        except Exception:
            pass

        # 2) 自己启动一个
        self.port = _free_port()
        try:
            self.proc = subprocess.Popen([
                self.exe,
                '--user-data-dir=' + self.profile,
                '--remote-debugging-port=%d' % self.port,
                '--no-first-run', '--no-default-browser-check',
                '--disable-features=msEdgeSidebarV2',
                self.url,
            ], creationflags=0x08000000)
        except Exception as e:
            raise CookieReadError('启动浏览器失败：%s' % e)
        try:
            with open(self._port_file(), 'w', encoding='utf-8') as f:
                f.write(str(self.port))
        except OSError:
            pass

        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            ver = self._probe(self.port)
            if ver:
                return ver
            last = '端口 %d 无响应' % self.port
            time.sleep(0.5)
        raise CookieReadError('浏览器调试端口未就绪（%s）。'
                              '若专用配置已被另一个浏览器实例占用，'
                              '请先关闭它再重试。' % last)

    # ------------------------------------------------------------------ CDP
    def _connect(self):
        """连到 page target。

        坑：Network 域只在 **page** target 上可用。连 browser 端点
        （/json/version 里的 webSocketDebuggerUrl）调用 Network.getCookies
        会返回 -32601 method not found —— 而且如果调用方不检查 error 字段，
        会误以为"成功但读了 0 条"。
        """
        import json as _json
        import urllib.request
        import websocket
        targets = _json.load(urllib.request.urlopen(
            'http://127.0.0.1:%d/json/list' % self.port, timeout=5))
        pages = [t for t in targets if t.get('type') == 'page']
        page = next((t for t in pages if 'music.163.com' in (t.get('url') or '')), None)
        if page is None and pages:
            page = pages[0]
        if page is None:
            raise CookieReadError('专用浏览器里没有可用页面')
        # suppress_origin：新版 Chromium 会拒绝带 Origin 的 WS 握手（403），
        # 这样不必开 --remote-allow-origins=*
        return websocket.create_connection(page['webSocketDebuggerUrl'],
                                           timeout=15, suppress_origin=True)

    def cdp(self, method, params=None, timeout=20):
        import json as _json
        if self._ws is None:
            self._ws = self._connect()
        self._ws.send(_json.dumps({'id': 1, 'method': method, 'params': params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._ws.settimeout(max(1, deadline - time.time()))
            msg = _json.loads(self._ws.recv())
            if msg.get('id') == 1:
                if 'error' in msg:
                    err = msg['error']
                    raise CookieReadError('CDP %s 失败：%s（code %s）'
                                          % (method, err.get('message'), err.get('code')))
                return msg.get('result', {})
        raise CookieReadError('CDP %s 超时' % method)

    def read_cookies(self, domain='163.com'):
        """从浏览器内部读指定域名的 cookie（不需要文件权限）"""
        try:
            res = self.cdp('Network.getCookies', {'urls': ['https://music.163.com',
                                                           'http://music.163.com']})
        except CookieReadError:
            # 页面可能刚跳转过，target 变了 —— 重连后再试一次
            self._ws = None
            res = self.cdp('Network.getCookies', {'urls': ['https://music.163.com',
                                                           'http://music.163.com']})
        out = {}
        for c in res.get('cookies', []):
            if domain in c.get('domain', ''):
                out[c['name']] = c.get('value', '')
        return out

    def close(self):
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=15)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


def cookie_from_pairs(pairs):
    """把 {name: value} 拼成可复用的 cookie 串"""
    return '; '.join('%s=%s' % (k, pairs[k]) for k in WANTED if pairs.get(k))


def cookie_is_usable(ck):
    """粗校验：cookie 必须只含 latin-1。

    HTTP 头只能是 latin-1；解出来带 U+FFFD 之类的乱码时，requests 会直接抛
    'latin-1 codec can't encode'。这类损坏值绝不能落盘，否则应用一启动
    就带着一个坏 Cookie，所有请求都失败还看不出原因。
    """
    if not ck or 'MUSIC_U=' not in ck:
        return False
    try:
        ck.encode('latin-1')
        return True
    except UnicodeEncodeError:
        return False
