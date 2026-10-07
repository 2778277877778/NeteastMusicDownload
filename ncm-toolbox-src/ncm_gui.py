# -*- coding: utf-8 -*-
"""
网易云音乐工具箱 —— 主窗口

把两件事合成一个应用，共用一个音乐库目录：
  * 「在线下载」：走网易云 API 直接下载带标签/封面的音频
  * 「NCM 解码」：驱动 NCMDecoder.exe 把本地 .ncm 解码成通用格式

入口：python ncm_gui.py
"""

import queue
import re
import sys
import traceback

import tkinter as tk
from tkinter import ttk

import time

from gui_common import (
    APP_TITLE,
    BUSY_POLL_MS,
    C_INFO,
    C_MUTED,
    C_WARN,
    IDLE_POLL_MS,
    QUALITIES,
    dnd_available,
    load_settings,
    log_file,
    make_root,
    save_settings,
)
import gui_theme
from gui_decode import DecodeTab
from gui_download import DownloadTab

WINDOW_MIN = (1000, 720)
DEFAULT_GEOM = '1140x960'


class MainApp(object):

    def __init__(self, root, dnd_ok):
        self.root = root
        self.dnd_ok = dnd_ok
        self.settings = load_settings()

        root.title(APP_TITLE)
        self._restore_geometry()
        root.minsize(*WINDOW_MIN)

        self._build_style()

        self.nb = ttk.Notebook(root)
        self.download_tab = DownloadTab(self.nb, self)
        self.decode_tab = DecodeTab(self.nb, self)
        self.nb.add(self.download_tab, text='   在线下载   ')
        self.nb.add(self.decode_tab, text='   NCM 解码   ')

        # 状态栏：设置都收进对话框了，关键状态就在这里一眼可见
        bar = ttk.Frame(root)
        bar.pack(fill='x', side='bottom', padx=10, pady=(0, 8))
        self.lbl_status = ttk.Label(bar, text='', foreground=C_MUTED)
        self.lbl_status.pack(side='left')
        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=10)
        self.lbl_right = ttk.Label(bar, text='', foreground=C_MUTED)
        self.lbl_right.pack(side='right')
        self.lbl_login_bar = ttk.Label(bar, text='', foreground=C_MUTED)
        self.lbl_login_bar.pack(side='left')

        # pack 顺序决定空间不够时谁被牺牲：状态栏必须先于 expand 的笔记本 pack。
        # 反过来写时两个标签页请求 909px 而默认窗口只有 880px，状态栏会被整个挤成
        # 0 高（mapped=0），音乐库目录/音质/登录状态平时根本看不到。
        self.nb.pack(fill='both', expand=True, padx=6, pady=(6, 0))

        self.download_tab.restore()
        self.decode_tab.restore()
        self.download_tab.register_login_label(self.lbl_login_bar)
        self._update_status()

        root.protocol('WM_DELETE_WINDOW', self.on_close)

        # 后台线程不能直接碰 Tk —— root.after() 本身也是 Tk 调用，主线程不在
        # mainloop 里时会抛 "main thread is not in main loop"。用队列把回调
        # 交回主线程执行。
        self._q = queue.Queue()
        self.root.after(IDLE_POLL_MS, self._drain_app)
        self.root.after(300, self._auto_login)      # MainApp 不是 Tk 控件，得经 root

    def _post(self, fn):
        """从任意线程投递一个在主线程执行的回调"""
        self._q.put(fn)

    def _drain_app(self):
        got = 0
        try:
            while True:
                fn = self._q.get_nowait()
                got += 1
                try:
                    fn()
                except Exception as e:
                    log_file('主线程回调出错: %s' % e)
        except queue.Empty:
            pass
        # 和两个标签页同一套策略：有活干 60ms，空闲 400ms。
        # 原来固定 200ms，窗口开着什么都不干也在每秒醒 5 次。
        busy = got or self.download_tab.busy or self.decode_tab.busy
        self.root.after(BUSY_POLL_MS if busy else IDLE_POLL_MS, self._drain_app)

    # ------------------------------------------------------------- 自动登录
    def _auto_login(self):
        """启动时静默尝试取一次登录态。

        专用配置里已经存着登录态，所以正常情况一两秒就完成，用户什么都不用点。
        失败也不弹窗打扰，只在日志里留一句。
        """
        from gui_common import has_login
        from gui_cookie import cookie_is_usable
        cur = self.settings.get('cookie', '')
        if has_login(cur):
            if cookie_is_usable(cur):
                self.download_tab.set_login_status('已载入登录态，正在校验…', C_INFO)
                return
            # 存着的是一个坏值（例如旧版本没剥域绑定前缀解出来的乱码），清掉重取
            self.download_tab.log('· 已存的登录态不可用，重新获取…')
            self.settings['cookie'] = ''
            self.download_tab.var_cookie.set('')

        def worker():
            try:
                from gui_cookie import find_music_cookie
                ck, src = find_music_cookie()
            except Exception as e:
                msg = str(e)
                self._post(lambda: self._auto_login_failed(msg))
                return
            self._post(lambda: self._auto_login_ok(ck, src))

        self.download_tab.set_login_status('正在获取登录态…', C_INFO)
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _auto_login_ok(self, cookie, src):
        self.download_tab.var_cookie.set(cookie)
        self.settings['cookie'] = cookie
        self.persist()
        self.download_tab.log('√ 已自动获取登录态（%s），可直接下载付费曲目' % src)
        self._update_status()

    def _auto_login_failed(self, reason):
        self.download_tab.set_login_status('未登录（只能下免费曲目）', C_WARN)
        self.download_tab.log('· 未自动获取到登录态：%s' % reason.split(chr(10))[0])
        self.download_tab.log('  付费/VIP 曲目需要登录：「设置…」→「网页登录取 Cookie」')

    # ------------------------------------------------------------------ 样式
    def _build_style(self):
        """视觉层全部在 gui_theme 里；这里只负责在建控件之前装上它。"""
        try:
            gui_theme.apply(self.root, ttk.Style())
        except Exception as e:
            log_file('主题应用失败，退回系统默认外观: %s' % e)

    # ------------------------------------------------------------- 窗口几何
    def _geometry_usable(self, g):
        """校验记忆的窗口位置仍然落在屏幕内。

        换显示器或拔掉外接屏之后，旧坐标可能整个跑到屏幕之外，
        窗口会"启动了但看不见"，用户只能去手改配置文件。
        """
        if not isinstance(g, str):
            return False
        m = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', g.strip())
        if not m:
            return False
        w, h, x, y = (int(v) for v in m.groups())
        if w < WINDOW_MIN[0] or h < WINDOW_MIN[1]:
            return False
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            return True
        # 至少要有可观的一部分在主屏内，标题栏得点得到
        if x > sw - 120 or x + w < 120:
            return False
        if y > sh - 80 or y + h < 80:
            return False
        return True

    def _restore_geometry(self):
        g = self.settings.get('window_geometry')
        if self._geometry_usable(g):
            try:
                self.root.geometry(g)
                return
            except Exception:
                pass
        self.root.geometry(DEFAULT_GEOM)
        # 首次启动居中
        try:
            self.root.update_idletasks()
            w, h = 1140, 960
            x = max(0, (self.root.winfo_screenwidth() - w) // 2)
            y = max(0, (self.root.winfo_screenheight() - h) // 3)
            self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))
        except Exception:
            pass

    def _save_geometry(self):
        try:
            g = self.root.winfo_geometry()
            # 只保留 WxH+X+Y，去掉可能的额外信息
            self.settings['window_geometry'] = g
        except Exception:
            pass

    # --------------------------------------------------------------- 应用接口
    def persist(self):
        save_settings(self.settings)

    def sync_dir(self, value):
        """两个标签页共用同一个音乐库目录"""
        value = (value or '').strip()
        if value:
            self.settings['download_dir'] = value
        cur = self.settings.get('download_dir', '')
        self.download_tab.set_dir(cur)
        self.decode_tab.set_dir(cur)
        self._update_status()

    def select_tab(self, index):
        try:
            self.nb.select(index)
        except Exception:
            pass

    def _update_status(self):
        q = dict(QUALITIES).get(self.settings.get('quality'), self.settings.get('quality') or '-')
        self.lbl_status.configure(text='音乐库: %s    音质: %s'
                                       % (self.settings.get('download_dir', ''), q))
        self.lbl_right.configure(text='拖放可用' if self.dnd_ok else '拖放不可用（缺 tkinterdnd2）')

    def on_close(self):
        self._save_geometry()
        save_settings(self.settings)
        self.root.destroy()


_MUTEX_NAME = 'Local\\NCM_Music_Toolbox_SingleInstance'
_mutex_handle = None


def acquire_single_instance():
    """Windows 命名互斥体做单实例保护；返回 False 表示已有实例在跑。

    必要：两个实例会同时读写 ~/.ncm/gui.json，后写的会把先写的设置（包括
    刚登录的 Cookie）整个覆盖掉。进程退出时互斥体自动释放，不会残留。
    """
    global _mutex_handle
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        _mutex_handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        return k32.GetLastError() != 183        # ERROR_ALREADY_EXISTS
    except Exception as e:
        log_file('单实例检测失败（忽略）: %s' % e)
        return True


def activate_existing():
    """把已有窗口恢复到前台。

    这里**绝对不能弹模态对话框**：被拒绝的实例会一直等用户点击，而它持有的
    互斥体句柄会让互斥体对象继续存在，于是之后每次启动都会被判为"已有实例"，
    程序再也起不来。踩过一次，改成静默激活后立即退出。
    """
    try:
        import ctypes
        u32 = ctypes.windll.user32
        u32.FindWindowW.restype = ctypes.c_void_p
        h = u32.FindWindowW(None, APP_TITLE)
        if h:
            u32.ShowWindow(h, 9)          # SW_RESTORE
            u32.SetForegroundWindow(h)
            log_file('已激活现有窗口')
        else:
            log_file('没找到现有窗口（可能在托盘/最小化）')
    except Exception as e:
        log_file('激活已有窗口失败: %s' % e)


def install_crash_log():
    """把未捕获异常写进日志。

    pythonw 没有控制台，也没人接 stderr —— 启动阶段一旦抛异常，进程直接消失，
    用户只看到"双击了没反应"。这类问题必须留痕，否则每次都要靠猜。
    """
    def hook(exc_type, exc, tb):
        try:
            log_file('=' * 62)
            log_file('CRASH %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
            for line in traceback.format_exception(exc_type, exc, tb):
                for sub in line.rstrip().splitlines():
                    log_file(sub)
        except Exception:
            pass
        try:
            sys.__stderr__ and sys.__stderr__.write(
                ''.join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
    sys.excepthook = hook


def main():
    install_crash_log()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if not acquire_single_instance():
        log_file('已有实例在运行，本次启动退出')
        activate_existing()
        return

    dnd_ok = dnd_available()
    try:
        root, dnd_ok = make_root()
    except Exception as e:
        log_file('make_root failed: %s' % e)
        root = tk.Tk()
        dnd_ok = False

    if not dnd_ok:
        log_file('tkinterdnd2 不可用，拖放功能关闭')

    try:
        MainApp(root, dnd_ok)
    except Exception:
        log_file('MainApp 构造失败：')
        for line in traceback.format_exc().splitlines():
            log_file(line)
        raise
    root.mainloop()


if __name__ == '__main__':
    main()
