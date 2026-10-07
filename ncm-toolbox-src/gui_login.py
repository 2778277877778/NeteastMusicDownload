# -*- coding: utf-8 -*-
"""
扫码登录对话框。

走的是网易云官方的二维码登录流程，和网页版/客户端一致：
  1. POST weapi/login/qrcode/unikey  取 unikey
  2. 把 https://music.163.com/login?codekey=<unikey> 渲染成二维码
  3. 轮询 weapi/login/qrcode/client/login 直到 803，此时会话里就有了 MUSIC_U

用这种方式不需要用户去开发者工具里翻 Cookie，也不经手密码。
"""

import queue
import threading
import time

import tkinter as tk
from tkinter import ttk

import requests

from ncm.constants import headers
from ncm.encrypt import encrypted_request

from gui_common import (
    C_BAD,
    C_INFO,
    C_MUTED,
    C_OK,
    C_WARN,
    VIP_TYPE_LABEL,
    fetch_account,
    log_file,
)

QR_UNIKEY = 'http://music.163.com/weapi/login/qrcode/unikey?csrf_token='
QR_POLL = 'http://music.163.com/weapi/login/qrcode/client/login?csrf_token='

# 网易云二维码登录的状态码
QR_EXPIRED = 800
QR_WAITING = 801
QR_SCANNED = 802
QR_OK = 803
QR_RISK = 8821      # 风控拦截：需要行为验证码 / 客户端版本过低

# 关键：创建和轮询都必须用 type=3。
# 旧的 type=1 在现在的风控下，手机确认后会返回 8821「请切换其他登录方式或升级新版本再试」，
# 而 type=3 是较新的客户端场景，不会被版本门拦下。
QR_TYPE = 3

STATUS_TEXT = {
    QR_EXPIRED: '二维码已过期，正在自动刷新…',
    QR_WAITING: '请用「网易云音乐」App 扫描二维码',
    QR_SCANNED: '已扫描 —— 请在手机上点击确认',
    QR_OK: '登录成功',
    QR_RISK: '网易云风控拒绝（8821）',
}
STATUS_COLOR = {
    QR_EXPIRED: C_WARN,
    QR_WAITING: C_INFO,
    QR_SCANNED: C_OK,
    QR_OK: C_OK,
    QR_RISK: C_BAD,
}

QR_VALID_SECONDS = 240      # 二维码有效期，超时后自动换一张
POLL_INTERVAL = 2.0
QR_CONTENT = 'https://music.163.com/login?codekey=%s'


def qr_available():
    """检查二维码渲染所需依赖；返回 (可用, 原因)"""
    try:
        import qrcode  # noqa: F401
    except Exception as e:
        return False, '缺少 qrcode 库（pip install "qrcode[pil]"）：%s' % e
    try:
        from PIL import ImageTk  # noqa: F401
    except Exception as e:
        return False, '缺少 Pillow 的 ImageTk（pip install Pillow）：%s' % e
    return True, ''


def make_qr_photo(text, box_size=6, border=2):
    """把文本渲染成 Tk 可用的二维码图片；缺 qrcode/Pillow 时抛异常由调用方处理"""
    import qrcode
    from PIL import ImageTk
    qr = qrcode.QRCode(box_size=box_size, border=border,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    return ImageTk.PhotoImage(img)


def cookie_from_session(session):
    """从登录会话里拼出可复用的 Cookie 串"""
    keys = ('MUSIC_U', '__csrf', 'NMTID', '_ntes_nuid')
    jar = session.cookies
    parts = []
    for k in keys:
        v = jar.get(k)
        if v:
            parts.append('%s=%s' % (k, v))
    return '; '.join(parts)


class WebLoginDialog(tk.Toplevel):
    """网页登录取 Cookie：打开官方登录页，然后自动从浏览器读取 MUSIC_U。

    比扫码更稳 —— 扫码会撞上风控 8821，而走浏览器登录完全不经过第三方 API 的风控面。
    唯一的限制是浏览器运行时会独占锁定 Cookie 库（Edge 会，Chrome 实测不会），
    锁住时窗口会明确提示关掉该浏览器。
    """

    POLL_MS = 3000
    TIMEOUT_S = 600

    def __init__(self, master, on_success, url='https://music.163.com/#/login'):
        super().__init__(master)
        self.on_success = on_success
        self.q = queue.Queue()
        self._stop = threading.Event()
        self._done = False
        self._last_msg = None
        self.browser = None          # 自适应方案二里我们自己启动的浏览器

        self.title('网页登录取 Cookie')
        self.resizable(False, False)
        self.transient(master)
        self._build(url)
        self.protocol('WM_DELETE_WINDOW', self._close)
        self._center(master)
        try:
            self.grab_set()
        except Exception:
            pass

        self.lbl_status.configure(text='正在检测本机浏览器登录态…', foreground=C_INFO)
        log_file('[网页登录] 对话框已打开，开始自适应检测')
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(self.POLL_MS, self._drain)

    def _build(self, url):
        pad = {'padx': 16, 'pady': 5}
        ttk.Label(self, text='在浏览器里登录网易云，登录态会自动读取',
                  font=('Microsoft YaHei UI', 10)).pack(**pad)
        ttk.Label(self, text='1. 浏览器已打开官方登录页（或用手机号/扫码在网页登录）\n'
                            '2. 登录成功后会停留在 music.163.com\n'
                            '3. 本窗口每 3 秒自动检测一次，检测到就自动填入',
                  justify='left', foreground=C_MUTED).pack(padx=16, pady=(0, 8))

        self.lbl_status = ttk.Label(self, text='准备中…', foreground=C_INFO,
                                    wraplength=380, justify='left')
        self.lbl_status.pack(padx=16, pady=(0, 6), anchor='w')

        ttk.Label(self, text='登录页地址：' + url, foreground=C_MUTED).pack(padx=16)

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=16, pady=12)
        ttk.Button(bar, text='立即读取', width=10, command=self.read_now).pack(side='left')
        ttk.Button(bar, text='取消', width=8, command=self._close).pack(side='right')

    def _center(self, master):
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry('+%d+%d' % (max(0, x), max(0, y)))
        except Exception:
            pass

    def read_now(self):
        self.lbl_status.configure(text='正在读取浏览器 Cookie…', foreground=C_INFO)
        threading.Thread(target=self._probe, daemon=True).start()

    def _probe(self):
        """手动重试：重跑一遍自适应流程"""
        self._stop.set()
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        self._stop = threading.Event()
        self.lbl_status.configure(text='正在重新检测…', foreground=C_INFO)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        """自适应获取登录态，两级策略依次尝试。

        策略一：直接读本机已有浏览器的 Cookie 库 —— 快，但浏览器运行时会锁库。
        策略二：自己启动一个使用独立配置目录的浏览器，用 DevTools 协议从浏览器
                内部读 Cookie —— 不碰文件、没有锁；该配置会记住登录，
                所以第二次以后连登录都不用了。
        """
        from gui_cookie import (CookieReadError, DebugBrowser, cookie_from_pairs,
                                find_music_cookie)

        # ---- 策略一 ----
        if not self._stop.is_set():
            try:
                ck, src = find_music_cookie()
                self.q.put(('ok', ck, src))
                return
            except CookieReadError as e:
                first = str(e).split('\n')[0]
                log_file('[网页登录] 策略一未成功：%s，转用自建浏览器' % first)
                self.q.put(('msg', '本机浏览器登录态读不到（%s）\n'
                                   '改用自动登录窗口…' % first, 'warn'))
            except Exception as e:
                log_file('[网页登录] 策略一异常：%s' % e)

        # ---- 策略二 ----
        if self._stop.is_set():
            return
        try:
            self.browser = DebugBrowser()
            ver = self.browser.start()
            log_file('[网页登录] 已启动专用浏览器 %s（端口 %s，配置 %s）'
                     % (ver.get('Browser'), self.browser.port, self.browser.profile))
            reused = getattr(self.browser, '_reused', False)
            self.q.put(('msg', '已启动专用浏览器，请在打开的窗口里登录网易云'
                              '（只需登录一次，之后会自动记住）'
                              if not reused else
                              '已连接到专用浏览器，正在检查登录态…', 'info'))

            deadline = time.time() + self.TIMEOUT_S
            while not self._stop.is_set() and time.time() < deadline:
                time.sleep(self.POLL_MS / 1000.0)
                if self._stop.is_set():
                    return
                try:
                    pairs = self.browser.read_cookies()
                except CookieReadError:
                    continue            # 页面还在加载，下一轮再来
                if pairs.get('MUSIC_U'):
                    self.q.put(('ok', cookie_from_pairs(pairs), '专用浏览器'))
                    return
                self.q.put(('msg', '等待在专用浏览器里完成登录…', 'info'))
            if not self._stop.is_set():
                self.q.put(('msg', '等待超时（10 分钟）。点「立即读取」可再试。', 'warn'))
        except CookieReadError as e:
            self.q.put(('msg', str(e), 'bad'))
        except ImportError as e:
            self.q.put(('msg', '缺少 websocket-client，无法用自动登录窗口：%s' % e, 'bad'))
        except Exception as e:
            log_file('[网页登录] 策略二异常：%s' % e)
            self.q.put(('msg', '自动登录窗口失败：%s' % e, 'bad'))

    def _close(self):
        self._stop.set()
        self._done = True
        if self.browser:
            try:
                self.browser.close()        # 关掉自建浏览器；专用配置保留，下次免登录
            except Exception:
                pass
            self.browser = None
        log_file('[网页登录] 对话框已关闭')
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _drain(self):
        if self._done:
            return
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == 'ok':
                    self._done = True
                    self.lbl_status.configure(
                        text='读取成功：%s' % msg[2], foreground=C_OK)
                    self.update_idletasks()
                    self.after(600, lambda c=msg[1], s=msg[2]: self._finish(c, s))
                    return
                text, level = msg[1], msg[2]
                # 只在内容变化时刷新，避免每 3 秒重复刷同样的提示
                if text != self._last_msg:
                    self._last_msg = text
                    self.lbl_status.configure(
                        text=text.replace('\n\n', '\n'),
                        foreground={'warn': C_WARN, 'bad': C_BAD}.get(level, C_MUTED))
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self._drain)

    def _finish(self, cookie, src):
        try:
            self.on_success(cookie, src)
        finally:
            self._close()


class QrLoginDialog(tk.Toplevel):
    """模态扫码登录窗口；成功后回调 on_success(cookie_str)"""

    def __init__(self, master, on_success):
        super().__init__(master)
        self.on_success = on_success
        self.q = queue.Queue()
        self._stop = threading.Event()
        self._photo = None
        self._unikey = None
        self._done = False

        self.title('扫码登录')
        self.resizable(False, False)
        self.transient(master)

        self._build()
        self.protocol('WM_DELETE_WINDOW', self._close)

        # 登录会话自己管 cookie：把 headers 里那条匿名 Cookie 摘掉，
        # 否则 requests 会一直用显式 Cookie 头，盖住服务器下发的 Set-Cookie
        h = dict(headers)
        h.pop('Cookie', None)
        self.session = requests.Session()
        self.session.headers.update(h)

        self._center(master)
        try:
            self.grab_set()
        except Exception:
            pass

        log_file('[扫码] 对话框已打开')
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(100, self._drain)

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = {'padx': 14, 'pady': 6}

        ttk.Label(self, text='用手机上的「网易云音乐」App 扫码登录',
                  font=('Microsoft YaHei UI', 10)).pack(**pad)

        holder = ttk.Frame(self)
        holder.pack(padx=14, pady=4)
        self.lbl_qr = tk.Label(holder, width=280, height=280,
                               background='white', relief='solid', borderwidth=1)
        self.lbl_qr.pack()

        self.lbl_status = ttk.Label(self, text='正在获取二维码…', foreground=C_INFO)
        self.lbl_status.pack(pady=(8, 2))

        self.lbl_hint = ttk.Label(
            self, text='登录成功后会自动填入 Cookie，无需手动复制',
            foreground=C_MUTED)
        self.lbl_hint.pack()

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=14, pady=(10, 12))
        ttk.Button(bar, text='刷新二维码', width=12,
                   command=self.refresh).pack(side='left')
        ttk.Button(bar, text='取消', width=8,
                   command=self._close).pack(side='right')

    def _center(self, master):
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry('+%d+%d' % (max(0, x), max(0, y)))
        except Exception:
            pass

    # -------------------------------------------------------------- 交互
    def refresh(self):
        """让当前轮询循环作废，重新取一张二维码"""
        self._stop.set()
        self._stop = threading.Event()
        self._unikey = None
        self.lbl_status.configure(text='正在刷新二维码…', foreground=C_INFO)
        threading.Thread(target=self._worker, daemon=True).start()

    def _finish(self, cookie, nick, vlabel):
        try:
            self.on_success(cookie, nick, vlabel)
        finally:
            self._close()

    def _close(self):
        self._stop.set()
        # 兜底：用户刚扫完就关窗时，会话里可能已经有 MUSIC_U 了，别白白丢掉
        if not self._done:
            try:
                ck = cookie_from_session(self.session)
                if 'MUSIC_U' in ck:
                    log_file('[扫码] 关窗时发现会话已有 MUSIC_U，仍按成功处理')
                    self._done = True
                    self.on_success(ck, None, None)
            except Exception as e:
                log_file('[扫码] 关窗兜底失败: %s' % e)
        self._done = True
        log_file('[扫码] 对话框已关闭')
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    # -------------------------------------------------------------- 后台
    def _worker(self):
        stop = self._stop
        try:
            while not stop.is_set():
                try:
                    j = self.session.post(
                        QR_UNIKEY, data=encrypted_request({'type': QR_TYPE}),
                        timeout=20).json()
                except Exception as e:
                    self.q.put(('error', '获取二维码失败：%s' % e))
                    return
                key = j.get('unikey')
                if not key:
                    self.q.put(('error', '服务器未返回二维码：%s' % j))
                    return
                self._unikey = key
                log_file('[扫码] 已取得 unikey=%s' % key)
                self.q.put(('qr', key))

                deadline = time.time() + QR_VALID_SECONDS
                while time.time() < deadline and not stop.is_set():
                    time.sleep(POLL_INTERVAL)
                    if stop.is_set():
                        return
                    try:
                        r = self.session.post(
                            QR_POLL, data=encrypted_request({'key': key, 'type': QR_TYPE}),
                            timeout=20)
                        j = r.json()
                    except Exception:
                        continue        # 单次网络抖动不致命，下一轮继续
                    code = j.get('code')
                    prev = getattr(self, '_last_code', None)
                    if code != prev:
                        self._last_code = code
                        log_file('[扫码] 状态 %s -> %s %s'
                                 % (prev, code, STATUS_TEXT.get(code, j.get('message') or '')))
                    if code == QR_OK:
                        ck = cookie_from_session(self.session)
                        if not ck:
                            self.q.put(('error', '授权已通过，但没能从会话里取到 MUSIC_U；'
                                                '请改用「说明」里的手动方式'))
                            return
                        # 先确认这个登录态真的能用，再对外宣布成功 ——
                        # 否则会出现"提示登录成功、紧接着又报 Cookie 无效"的自相矛盾
                        keys = sorted(k for k in ('MUSIC_U', '__csrf', 'NMTID', '_ntes_nuid')
                                      if k in ck)
                        log_file('[扫码] 803 授权通过，取得 Cookie 键=%s 长度=%d'
                                 % (keys, len(ck)))
                        prof = fetch_account(self.session)
                        nick = (prof or {}).get('nickname')
                        vtype = (prof or {}).get('vipType')
                        vlabel = VIP_TYPE_LABEL.get(vtype, '类型码 %s' % vtype) if prof else None
                        log_file('[扫码] 账号校验: nick=%s vipType=%s' % (nick, vtype))
                        self.q.put(('ok', ck, nick, vlabel))
                        return
                    if code == QR_RISK:
                        # 这是服务端风控，不是码过期 —— 换一张也没用，直接停
                        log_file('[扫码] 8821 风控拦截: %s' % (j.get('message') or ''))
                        self.q.put(('risk', j.get('message') or ''))
                        return
                    self.q.put(('status', code))
                    if code == QR_EXPIRED:
                        break           # 跳出内层，外层自动换一张
        except Exception as e:
            self.q.put(('error', '登录流程异常：%s' % e))

    # -------------------------------------------------------- 主线程消费
    def _drain(self):
        if self._done:
            return
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == 'qr':
                    try:
                        self._photo = make_qr_photo(QR_CONTENT % msg[1])
                        self.lbl_qr.configure(image=self._photo, width=0, height=0)
                    except Exception as e:
                        self.lbl_status.configure(
                            text='二维码渲染失败（缺 qrcode/Pillow）：%s' % e,
                            foreground=C_BAD)
                elif kind == 'status':
                    code = msg[1]
                    self.lbl_status.configure(
                        text=STATUS_TEXT.get(code, '状态码 %s' % code),
                        foreground=STATUS_COLOR.get(code, C_MUTED))
                elif kind == 'ok':
                    cookie, nick, vlabel = msg[1], msg[2], msg[3]
                    self._done = True
                    if nick:
                        self.lbl_status.configure(
                            text='登录成功：%s%s' % (nick, '（%s）' % vlabel if vlabel else ''),
                            foreground=C_OK)
                    else:
                        self.lbl_status.configure(
                            text='授权已通过（账号校验未返回昵称）', foreground=C_WARN)
                    self.update_idletasks()
                    # 让用户看清是哪个账号登录的，再关窗
                    self.after(700, lambda c=cookie, n=nick, v=vlabel: self._finish(c, n, v))
                    return
                elif kind == 'risk':
                    self._done = True
                    self.lbl_status.configure(
                        text='网易云风控拒绝（8821）—— 请改用「打开登录页」方式',
                        foreground=C_BAD)
                    self.lbl_hint.configure(
                        text='风控拦截，重扫无效。请点主界面的「打开登录页」，\n'
                             '在网页登录后复制 MUSIC_U 粘贴进来。',
                        foreground=C_BAD)
                    self.update_idletasks()
                    self.after(4000, self._close)
                    return
                elif kind == 'error':
                    self.lbl_status.configure(text=msg[1], foreground=C_BAD)
        except queue.Empty:
            pass
        self.after(200, self._drain)
