# -*- coding: utf-8 -*-
"""
设置对话框。

主界面原来把「每次都要操作的东西」和「配一次就不动的东西」混在一起铺了 4 行、
19 个按钮，把日志区挤到只剩 8px —— 而日志恰恰是排错时最需要的。这里把后者
全部收进对话框，主界面只保留流程本身。
"""

import tkinter as tk
from tkinter import ttk, messagebox

from gui_common import C_MUTED, C_OK, C_WARN, FORMAT_PRESETS, QUALITIES


class SettingsDialog(tk.Toplevel):
    """非模态设置窗口。直接操作各页签的 Tk 变量，所以关掉即生效，无需同步。"""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.dl = app.download_tab
        self.dc = app.decode_tab

        self.title('设置')
        self.resizable(False, False)
        self.transient(master)
        self._build()
        self._center(master)
        try:
            self.grab_set()
        except Exception:
            pass

        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda e: self.close())

    # ------------------------------------------------------------------ UI
    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=10, pady=(10, 0))

        nb.add(self._tab_download(nb), text='   下载   ')
        nb.add(self._tab_decode(nb), text='   解码   ')

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=10, pady=10)
        ttk.Button(bar, text='关闭', width=10, command=self.close).pack(side='right')
        self.lbl_saved = ttk.Label(bar, text='', foreground=C_OK)
        self.lbl_saved.pack(side='left')

    # -------------------------------------------------------------- 下载页
    def _tab_download(self, parent):
        f = ttk.Frame(parent)
        pad = {'padx': 12, 'pady': 4}

        # ---- 账号 ----
        acc = ttk.LabelFrame(f, text=' 账号 ')
        acc.pack(fill='x', **pad)

        r = ttk.Frame(acc)
        r.pack(fill='x', padx=10, pady=(8, 4))
        ttk.Label(r, text='Cookie:', width=9).pack(side='left')
        self.ent_cookie = ttk.Entry(r, textvariable=self.dl.var_cookie, show='*', width=54)
        self.ent_cookie.pack(side='left', fill='x', expand=True, padx=4)
        self.show_cookie = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text='显示', variable=self.show_cookie,
                        command=self._toggle_cookie).pack(side='left')

        r = ttk.Frame(acc)
        r.pack(fill='x', padx=10, pady=(0, 4))
        ttk.Label(r, text='', width=9).pack(side='left')
        ttk.Button(r, text='扫码登录', width=10,
                   command=self.dl.on_qr_login).pack(side='left', padx=(4, 4))
        ttk.Button(r, text='网页登录取 Cookie', width=17,
                   command=self.dl.on_web_login).pack(side='left', padx=4)
        ttk.Button(r, text='验证', width=8,
                   command=self._verify).pack(side='left', padx=4)
        ttk.Button(r, text='取 Cookie 说明', width=14,
                   command=self.dl.show_cookie_help).pack(side='left', padx=4)

        r = ttk.Frame(acc)
        r.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(r, text='状态:', width=9).pack(side='left')
        self.lbl_login = ttk.Label(r, text='', foreground=C_MUTED)
        self.lbl_login.pack(side='left', padx=4)
        # 登记制：下载逻辑把状态推给所有登记过的标签，这里注销后就不会再被写到
        self.dl.register_login_label(self.lbl_login)
        ttk.Label(acc, text='不填 Cookie 只能下免费曲目；付费/无损需要登录态。',
                  foreground=C_MUTED).pack(anchor='w', padx=12, pady=(0, 8))

        # ---- 下载 ----
        d = ttk.LabelFrame(f, text=' 下载 ')
        d.pack(fill='x', **pad)

        r = ttk.Frame(d)
        r.pack(fill='x', padx=10, pady=(8, 4))
        ttk.Label(r, text='音乐库目录:', width=11).pack(side='left')
        ttk.Entry(r, textvariable=self.dl.var_dir).pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(r, text='浏览…', width=8, command=self._pick_dir).pack(side='left')

        r = ttk.Frame(d)
        r.pack(fill='x', padx=10, pady=(0, 4))
        ttk.Label(r, text='音质:', width=11).pack(side='left')
        ttk.Combobox(r, textvariable=self.dl.var_quality, width=16, state='readonly',
                     values=[n for n, _ in QUALITIES]).pack(side='left', padx=4)
        ttk.Checkbutton(r, text='跳过已下载', variable=self.dl.var_skip).pack(side='left', padx=12)
        ttk.Checkbutton(r, text='断点续传', variable=self.dl.var_resume).pack(side='left', padx=4)

        r = ttk.Frame(d)
        r.pack(fill='x', padx=10, pady=(0, 4))
        ttk.Label(r, text='请求间隔:', width=11).pack(side='left')
        self.var_interval = tk.DoubleVar(
            value=float(self.dl.settings.get('download_interval', 1.0) or 0))
        ttk.Spinbox(r, from_=0, to=10, increment=0.5, width=6,
                    textvariable=self.var_interval).pack(side='left', padx=4)
        ttk.Label(r, text='秒/首').pack(side='left')
        ttk.Label(r, text='批量下载别调到 0 —— 短时间高频请求容易被风控',
                  foreground=C_WARN).pack(side='left', padx=8)

        r = ttk.Frame(d)
        r.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(r, text='附加内容:', width=11).pack(side='left')
        ttk.Checkbutton(r, text='下载歌词 .lrc', variable=self.dl.var_lyrics).pack(side='left', padx=4)
        ttk.Checkbutton(r, text='歌词写入标签', variable=self.dl.var_embed_lyrics).pack(side='left', padx=4)
        ttk.Label(r, text='(MP3→USLT / FLAC→LYRICS)', foreground=C_MUTED).pack(side='left', padx=4)

        # ---- 命名 ----
        nm = ttk.LabelFrame(f, text=' 命名与分类 ')
        nm.pack(fill='x', **pad)
        r = ttk.Frame(nm)
        r.pack(fill='x', padx=10, pady=8)
        ttk.Label(r, text='命名格式:', width=11).pack(side='left')
        ttk.Combobox(r, textvariable=self.dl.var_name_type, width=20, state='readonly',
                     values=['1 - 歌名', '2 - 歌手 - 歌名', '3 - 歌名 - 歌手']).pack(side='left', padx=4)
        ttk.Label(r, text='分类:', width=6).pack(side='left', padx=(14, 0))
        ttk.Combobox(r, textvariable=self.dl.var_folder_type, width=20, state='readonly',
                     values=['1 - 不分文件夹', '2 - 按歌手分', '3 - 按歌手/专辑分']).pack(side='left', padx=4)

        ttk.Label(f, text='改完直接关窗即生效。',
                  foreground=C_MUTED).pack(anchor='w', padx=14, pady=(2, 8))
        return f

    # -------------------------------------------------------------- 解码页
    def _tab_decode(self, parent):
        f = ttk.Frame(parent)
        pad = {'padx': 12, 'pady': 4}

        g = ttk.LabelFrame(f, text=' 解码器 ')
        g.pack(fill='x', **pad)
        r = ttk.Frame(g)
        r.pack(fill='x', padx=10, pady=(8, 4))
        ttk.Label(r, text='程序路径:', width=10).pack(side='left')
        ttk.Entry(r, textvariable=self.dc.var_decoder).pack(
            side='left', fill='x', expand=True, padx=4)
        ttk.Button(r, text='浏览…', width=8, command=self._pick_decoder).pack(side='left')

        r = ttk.Frame(g)
        r.pack(fill='x', padx=10, pady=(0, 4))
        ttk.Label(r, text='', width=10).pack(side='left')
        self.lbl_decoder = ttk.Label(r, text='', foreground=C_MUTED)
        self.lbl_decoder.pack(side='left', padx=4)
        self.dc.register_decoder_label(self.lbl_decoder)
        self.dc._refresh_decoder_label()

        r = ttk.Frame(g)
        r.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(r, text='说明:', width=10).pack(side='left')
        ttk.Label(r, text='解码器是外部独立程序，本便携包未附带。放进本文件夹或其 dist\\ 子目录会自动识别。',
                  foreground=C_MUTED).pack(side='left', padx=4)

        d = ttk.LabelFrame(f, text=' 默认参数（每次运行仍可在解码页临时修改） ')
        d.pack(fill='x', **pad)
        r = ttk.Frame(d)
        r.pack(fill='x', padx=10, pady=(8, 4))
        ttk.Label(r, text='输出格式:', width=10).pack(side='left')
        ttk.Combobox(r, textvariable=self.dc.var_format, width=20,
                     values=[n for n, _ in FORMAT_PRESETS]).pack(side='left', padx=4)
        ttk.Label(r, text='（可直接输入扩展名）', foreground=C_MUTED).pack(side='left', padx=4)

        r = ttk.Frame(d)
        r.pack(fill='x', padx=10, pady=(0, 4))
        ttk.Label(r, text='并发:', width=10).pack(side='left')
        ttk.Spinbox(r, from_=1, to=8, width=5,
                    textvariable=self.dc.var_conc).pack(side='left', padx=4)
        ttk.Checkbutton(r, text='联网嵌入封面',
                        variable=self.dc.var_cover).pack(side='left', padx=12)

        r = ttk.Frame(d)
        r.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(r, text='输出目录:', width=10).pack(side='left')
        ttk.Label(r, text='与下载页共用「音乐库目录」，在上面的「下载」页里改。',
                  foreground=C_MUTED).pack(side='left', padx=4)
        return f

    # -------------------------------------------------------------- 交互
    def _toggle_cookie(self):
        self.ent_cookie.configure(show='' if self.show_cookie.get() else '*')

    def _verify(self):
        self.dl.on_verify()

    def _pick_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.dl.var_dir.get() or '')
        if d:
            import os
            self.dl.var_dir.set(os.path.normpath(d))
            self.dl.app.sync_dir(self.dl.var_dir.get())
            self._flash('已更新音乐库目录')

    def _pick_decoder(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title='选择 NCMDecoder.exe',
                                       filetypes=[('可执行文件', '*.exe'), ('全部文件', '*.*')])
        if p:
            import os
            self.dc.var_decoder.set(os.path.normpath(p))
            self.dc._refresh_decoder_label()
            self._flash('已更新解码器路径')

    def _flash(self, msg):
        self.lbl_saved.configure(text=msg)
        self.after(2500, lambda: self.lbl_saved.configure(text=''))

    def _center(self, master):
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + 80
            self.geometry('+%d+%d' % (max(0, x), max(0, y)))
        except Exception:
            pass

    def close(self):
        # 非模态对话框：这里把只存在于对话框里的值写回 settings，其余变量
        # 本来就绑定在下载页的 Tk 变量上，由 collect() 统一收集。
        try:
            self.dl.settings['download_interval'] = float(self.var_interval.get() or 0)
        except Exception:
            pass
        # 注销标签，否则下载逻辑之后还会往已销毁的控件里写，抛 TclError
        try:
            self.dl.unregister_login_label(self.lbl_login)
            self.dc.unregister_decoder_label(self.lbl_decoder)
        except Exception:
            pass
        try:
            self.dl.app.persist()
            self.dl.collect()
            self.dl.app.sync_dir(self.dl.settings['download_dir'])
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
