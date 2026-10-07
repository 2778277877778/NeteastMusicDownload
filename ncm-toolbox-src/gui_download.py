# -*- coding: utf-8 -*-
"""
「在线下载」标签页：粘贴网易云链接 → 下载带封面与 ID3/Vorbis 标签的音频。
逻辑与原先的单窗口版一致，改为标签页并接入共享设置。
"""

import json
import os
import queue
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from concurrent.futures import ThreadPoolExecutor, as_completed

import gui_theme
from ncm.constants import headers
from ncm.encrypt import encrypted_request

# requests / ncm.api / ncm.file_util 不在顶层导入：它们加起来约 300ms 的冷启动开销，
# 而全部只在函数里用到（建会话、解析合集、缩放封面）。放到用到时再导，
# 窗口能早半秒出现，代价只是首次解析时多等几十毫秒。

from gui_common import (
    BUSY_POLL_MS,
    C_BAD,
    C_INFO,
    C_MUTED,
    C_OK,
    C_WARN,
    FLAC_LEVELS,
    IDLE_POLL_MS,
    KIND_LABEL,
    LEVEL_ORDER,
    PLAY_FAIL_REASON,
    QUALITIES,
    StopRequested,
    VIP_TYPE_LABEL,
    append_log,
    apply_cookie,
    build_lrc_text,
    cover_url_of,
    enable_drop,
    fetch_account,
    fetch_lyric,
    fetch_song_details,
    fetch_vip_info,
    has_login,
    human_size,
    log_file,
    log_file_many,
    parse_target,
    safe_name,
    search_songs,
    song_meta,
    split_drop_paths,
    write_lrc_file,
    write_tags,
)


class DownloadTab(ttk.Frame):

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.settings = app.settings
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        # 每首自带来源上下文，这样多个歌单/专辑混在一起下载时，各自仍能落到自己的子文件夹
        # {'song': dict, 'is_program': bool, 'collection': str|None, 'source': str, 'failed': bool}
        self.tracks = []
        self._login_labels = []          # 所有用于显示登录状态的标签
        self._login = ('登录状态未知', C_MUTED)

        self._build_ui()
        self.after(80, self._drain)
        self.log('批量下载：每行粘贴一个链接或 ID，点「解析列表」合并进队列，再点「开始下载」。')
        self.log('提示：要下载无损/Hi-Res，请填入你自己的登录 Cookie（见「说明」按钮）。')

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        # 这些 Tk 变量归本页所有。设置对话框只是把它们绑到自己的控件上，
        # 所以即使从不打开对话框，变量也必须已经存在（collect/restore 要用）。
        self.var_dir = tk.StringVar()
        self.var_quality = tk.StringVar()
        self.var_cookie = tk.StringVar()
        self.var_skip = tk.BooleanVar(value=True)
        self.var_lyrics = tk.BooleanVar(value=True)
        self.var_embed_lyrics = tk.BooleanVar(value=True)
        self.var_resume = tk.BooleanVar(value=True)
        self.var_name_type = tk.StringVar()
        self.var_folder_type = tk.StringVar()
        self.var_kind = tk.StringVar(value='auto')
        self.var_search = tk.StringVar()
        self.var_search_n = tk.IntVar(value=20)

        # ---------------- ① 添加下载源 ----------------
        top = ttk.LabelFrame(self, text=' ① 添加下载源 ')
        top.pack(fill='x', **pad)

        box = ttk.Frame(top)
        box.pack(fill='x', padx=8, pady=(6, 2))
        self.txt_input = tk.Text(box, height=5, wrap='char', undo=True,
                                 font=gui_theme.FONT_MONO, relief='flat', borderwidth=1,
                                 highlightthickness=1, highlightbackground=gui_theme.BORDER,
                                 highlightcolor=gui_theme.ACCENT,
                                 background=gui_theme.SURFACE, foreground=gui_theme.TEXT,
                                 insertbackground=gui_theme.TEXT,
                                 selectbackground=gui_theme.SELECTION, spacing1=3, spacing3=3)
        vsi = ttk.Scrollbar(box, orient='vertical', command=self.txt_input.yview)
        self.txt_input.configure(yscrollcommand=vsi.set)
        self.txt_input.pack(side='left', fill='both', expand=True)
        vsi.pack(side='left', fill='y')
        self.txt_input.bind('<Control-Return>', lambda e: (self.on_parse(), 'break')[1])
        self.txt_input.bind('<KeyRelease>', lambda e: self.after(300, self._auto_detect_kind))
        enable_drop(self.txt_input, self._on_drop)

        # 占位提示做成盖在输入框上的标签，而不是往框里塞一行示例文字：
        # 后者每次读取都得先判断"这行是用户粘的还是我自己写的"，早晚会漏。
        self.lbl_ph = ttk.Label(box, foreground=C_MUTED, background=gui_theme.SURFACE,
                                justify='left',
                                text='在这里粘贴歌曲 / 专辑 / 歌单 / 电台链接或 ID，一行一个\n'
                                     '以 # 开头的是注释；也可以把 .txt 或 .ncm 直接拖进来')
        self._refresh_placeholder()

        # 粘贴下载 与 关键词搜索 是两条入口，分成两行；挤在同一行时
        # 换个主题或字号就会把「搜索」按钮顶出窗口（实测过）。
        bar = ttk.Frame(top)
        bar.pack(fill='x', padx=8, pady=(2, 0))
        ttk.Button(bar, text='解析列表', width=10, command=self.on_parse).pack(side='left')
        ttk.Button(bar, text='导入文件…', width=11, command=self.import_file).pack(side='left', padx=4)
        ttk.Button(bar, text='清空', width=7, command=self.clear_all).pack(side='left')
        self.lbl_kind = ttk.Label(bar, text='', foreground=C_MUTED)
        self.lbl_kind.pack(side='right')

        bar2 = ttk.Frame(top)
        bar2.pack(fill='x', padx=8, pady=(2, 6))
        ttk.Label(bar2, text='按关键词搜索:', foreground=C_MUTED).pack(side='left')
        ent_s = ttk.Entry(bar2, textvariable=self.var_search, width=26)
        ent_s.pack(side='left', padx=6, fill='x', expand=True)
        ent_s.bind('<Return>', lambda e: self.on_search())
        ttk.Button(bar2, text='搜索', width=7, command=self.on_search).pack(side='right', padx=(6, 0))
        ttk.Label(bar2, text='首').pack(side='right')
        ttk.Spinbox(bar2, from_=1, to=200, width=5,
                    textvariable=self.var_search_n).pack(side='right', padx=2)
        ttk.Label(bar2, text='取前').pack(side='right')

        # ---------------- ② 下载队列 ----------------
        mid = ttk.LabelFrame(self, text=' ② 下载队列 ')
        self.mid_frame = mid
        mid.pack(fill='both', expand=True, **pad)
        cols = ('idx', 'name', 'artist', 'album', 'source', 'status')
        self.tree = ttk.Treeview(mid, columns=cols, show='headings', height=6)
        for c, t, w in (('idx', '#', 44), ('name', '歌曲名', 250),
                        ('artist', '歌手', 140), ('album', '专辑', 175),
                        ('source', '来源', 145), ('status', '状态', 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor='w', stretch=(c in ('name', 'album', 'source')))
        vs = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        vs.pack(side='right', fill='y', pady=6)
        self.tree.tag_configure('ok', foreground=C_OK)
        self.tree.tag_configure('fail', foreground=C_BAD)
        self.tree.tag_configure('skip', foreground=C_MUTED)
        self.tree.tag_configure('run', foreground=C_INFO)

        # ---------------- ③ 操作 ----------------
        act = ttk.Frame(self)
        act.pack(fill='x', **pad)
        self.btn_start = ttk.Button(act, text='开始下载', width=12, command=self.on_start,
                                    style='Primary.TButton')
        self.btn_start.pack(side='left')
        self.btn_stop = ttk.Button(act, text='停止', width=8,
                                   command=self.on_stop, state='disabled')
        self.btn_stop.pack(side='left', padx=6)
        ttk.Button(act, text='重试失败项', width=12,
                   command=self.on_retry_failed).pack(side='left', padx=6)
        ttk.Button(act, text='打开下载目录', width=14,
                   command=self.open_dir).pack(side='left', padx=6)
        ttk.Button(act, text='设置…', width=9,
                   command=self.open_settings).pack(side='left', padx=6)
        self.lbl_stat = ttk.Label(act, text='就绪')
        self.lbl_stat.pack(side='right')

        prg = ttk.Frame(self)
        prg.pack(fill='x', **pad)
        ttk.Label(prg, text='整单进度', width=9).pack(side='left')
        self.bar_all = ttk.Progressbar(prg, mode='determinate', maximum=100)
        self.bar_all.pack(side='left', fill='x', expand=True, padx=6)
        self.lbl_all = ttk.Label(prg, text='0 / 0', width=12)
        self.lbl_all.pack(side='left')

        prg2 = ttk.Frame(self)
        prg2.pack(fill='x', padx=8, pady=(0, 4))
        ttk.Label(prg2, text='当前文件', width=9).pack(side='left')
        self.bar_one = ttk.Progressbar(prg2, mode='determinate', maximum=100)
        self.bar_one.pack(side='left', fill='x', expand=True, padx=6)
        self.lbl_one = ttk.Label(prg2, text='', width=26)
        self.lbl_one.pack(side='left')

        # ---------------- ④ 日志 ----------------
        logf = ttk.LabelFrame(self, text=' ④ 日志 ')
        logf.pack(fill='both', expand=True, **pad)
        self.txt = tk.Text(logf, height=11, wrap='word', state='disabled',
                           background=gui_theme.LOG_BG, foreground=gui_theme.LOG_FG,
                           insertbackground=gui_theme.LOG_FG, relief='flat',
                           highlightthickness=1, highlightbackground=gui_theme.BORDER,
                           font=gui_theme.FONT_MONO, spacing1=2, spacing3=2)
        ls = ttk.Scrollbar(logf, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=ls.set)
        self.txt.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        ls.pack(side='right', fill='y', pady=6)

        self._register_tree_shortcuts()
    def restore(self):
        s = self.settings
        self.var_dir.set(s['download_dir'])
        self.var_name_type.set('%d - %s' % (s['name_type'],
                                            ['歌名', '歌手 - 歌名', '歌名 - 歌手'][s['name_type'] - 1]))
        self.var_folder_type.set('%d - %s' % (s['folder_type'],
                                              ['不分文件夹', '按歌手分', '按歌手/专辑分'][s['folder_type'] - 1]))
        for n, lv in QUALITIES:
            if lv == s['quality']:
                self.var_quality.set(n)
                break
        else:
            self.var_quality.set(QUALITIES[2][0])
        self.var_cookie.set(s.get('cookie', ''))
        self.var_skip.set(bool(s.get('skip_existing', True)))
        self.var_lyrics.set(bool(s.get('download_lyrics', True)))
        self.var_embed_lyrics.set(bool(s.get('embed_lyrics', True)))
        self.var_resume.set(bool(s.get('resume_partial', True)))
        apply_cookie(s.get('cookie', ''))

    def set_dir(self, value):
        self.var_dir.set(value)


    def collect(self):
        """把界面上的设置写回 settings（不落盘）"""
        self.settings.update({
            'download_dir': self.var_dir.get().strip() or self.settings['download_dir'],
            'name_type': int(self.var_name_type.get().split(' ')[0]),
            'folder_type': int(self.var_folder_type.get().split(' ')[0]),
            'quality': dict(QUALITIES)[self.var_quality.get()],
            'cookie': self.var_cookie.get().strip(),
            'skip_existing': bool(self.var_skip.get()),
            'download_lyrics': bool(self.var_lyrics.get()),
            'embed_lyrics': bool(self.var_embed_lyrics.get()),
            'resume_partial': bool(self.var_resume.get()),
        })

    def on_save_settings(self):
        self.collect()
        self.app.persist()
        apply_cookie(self.settings['cookie'])
        self.app.sync_dir(self.settings['download_dir'])
        self.log('设置已保存 → %s' % self.settings['download_dir'])
        messagebox.showinfo('已保存', '设置已保存。')

    # ----------------------------------------------------------------- 日志
    def log(self, msg):
        self.log_many([msg])

    def log_many(self, msgs):
        """一批一起上屏、一起落盘：逐行刷新文本控件是这里最贵的一步"""
        if isinstance(msgs, str):
            msgs = [msgs]
        msgs = [m for m in msgs if m]
        if not msgs:
            return
        ts = time.strftime('%H:%M:%S')
        lines = ['[%s] %s' % (ts, m) for m in msgs]
        append_log(self.txt, lines)
        log_file_many(lines)

    # ------------------------------------------------------------- 界面辅助
    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or '')
        if d:
            self.var_dir.set(os.path.normpath(d))

    def open_dir(self):
        d = self.var_dir.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showwarning('目录不存在', '目录还不存在：\n%s' % d)
            return
        try:
            os.startfile(d)
        except Exception as e:
            messagebox.showerror('打开失败', str(e))

    # ------------------------------------------------------- 设置与登录状态
    def open_settings(self):
        """打开设置对话框（非模态，关掉即生效）"""
        dlg = getattr(self, '_settings_dlg', None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            return
        from gui_settings import SettingsDialog
        self._settings_dlg = SettingsDialog(self.winfo_toplevel(), self.app)

    def register_login_label(self, lbl):
        """登记一个用于显示登录状态的标签。

        状态本来只写在下载页的一个标签上，但设置对话框关掉后那个控件就没了，
        再往里写会抛 TclError。改成登记制：谁想显示就登记，销毁时注销。
        """
        if lbl not in self._login_labels:
            self._login_labels.append(lbl)
        self._push_login_status()

    def unregister_login_label(self, lbl):
        if lbl in self._login_labels:
            self._login_labels.remove(lbl)

    def set_login_status(self, text, color=C_MUTED):
        self._login = (text, color)
        self._push_login_status()

    def _push_login_status(self):
        text, color = self._login
        for lbl in list(self._login_labels):
            try:
                lbl.configure(text=text, foreground=color)
            except Exception:
                try:
                    self._login_labels.remove(lbl)
                except ValueError:
                    pass

    def _register_tree_shortcuts(self):
        self.tree.bind('<Delete>', lambda e: self.on_retry_failed())

    def clear_list(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.tracks = []
        self.bar_all['value'] = 0
        self.bar_one['value'] = 0
        self.lbl_all.configure(text='0 / 0')
        self.lbl_one.configure(text='')

    def clear_all(self):
        self.txt_input.delete('1.0', 'end')
        self._refresh_placeholder()
        self.clear_list()
        self.lbl_kind.configure(text='')

    # ------------------------------------------------------------ 批量输入
    def get_input_lines(self):
        """取输入框里的有效行：跳过空行与 # 注释，去掉行首尾空白"""
        raw = self.txt_input.get('1.0', 'end-1c')
        out = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                out.append(ln)
        return out

    def _append_input_lines(self, lines):
        cur = self.txt_input.get('1.0', 'end-1c').strip()
        if cur:
            self.txt_input.insert('end', '\n')
        self.txt_input.insert('end', '\n'.join(lines))
        self.txt_input.see('end')
        self._refresh_placeholder()
        self._auto_detect_kind()

    def import_file(self):
        p = filedialog.askopenfilename(
            title='导入链接列表（每行一个）',
            filetypes=[('文本文件', '*.txt'), ('全部文件', '*.*')])
        if not p:
            return
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as fh:
                lines = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith('#')]
        except Exception as e:
            messagebox.showerror('读取失败', str(e))
            return
        self._append_input_lines(lines)
        self.log('已从 %s 导入 %d 行' % (os.path.basename(p), len(lines)))

    def _refresh_placeholder(self):
        """输入框空着才显示占位提示"""
        try:
            if self.txt_input.get('1.0', 'end-1c').strip():
                self.lbl_ph.place_forget()
            else:
                self.lbl_ph.place(in_=self.txt_input, relx=0.012, rely=0.05)
        except Exception:
            pass

    def _auto_detect_kind(self):
        self._refresh_placeholder()
        lines = self.get_input_lines()
        if not lines:
            self.lbl_kind.configure(text='')
            return
        force = self.var_kind.get()
        n_song = n_coll = n_bad = 0
        for ln in lines:
            k, tid = parse_target(ln, force)
            if not (k and tid):
                n_bad += 1
            elif k == 'song':
                n_song += 1
            else:
                n_coll += 1
        bits = []
        if n_song:
            bits.append('%d 首单曲' % n_song)
        if n_coll:
            bits.append('%d 个合集' % n_coll)
        if n_bad:
            bits.append('%d 行无法识别' % n_bad)
        self.lbl_kind.configure(text='共 %d 行 · %s' % (len(lines), ' · '.join(bits)),
                              foreground=C_WARN if n_bad else C_MUTED)

    def _on_drop(self, event):
        """拖到本页的文件：.ncm 转给解码页；.txt 当作链接列表导入"""
        paths = [p for p in split_drop_paths(self, event.data)]
        ncm = [p for p in paths if p.lower().endswith('.ncm')]
        txt = [p for p in paths if p.lower().endswith('.txt')]
        if ncm:
            self.app.decode_tab._add_paths(ncm)
            self.app.select_tab(1)
            return 'break'
        if txt:
            added = 0
            for p in txt:
                try:
                    with open(p, 'r', encoding='utf-8', errors='replace') as fh:
                        lines = [ln.strip() for ln in fh
                                 if ln.strip() and not ln.strip().startswith('#')]
                except Exception as e:
                    self.log('× 读取 %s 失败：%s' % (os.path.basename(p), e))
                    continue
                self._append_input_lines(lines)
                added += len(lines)
            if added:
                self.log('已从拖入的文本文件导入 %d 行' % added)
            return 'break'
        return None

    def show_cookie_help(self):
        messagebox.showinfo(
            '如何获取 Cookie（下载付费曲目必看）',
            '网易云对「取播放地址」这一步是看登录态的。不带 Cookie 时，付费/VIP 曲目\n'
            '会返回 code -110（需要登录），无损/Hi-Res 也基本都拿不到。\n\n'
            '【最省事】直接点旁边的「扫码登录」——用网易云音乐 App 扫一下就行，\n'
            '登录成功后 Cookie 会自动填好，不需要看下面的内容。\n\n'
            '———————————————————————————\n'
            '如果扫码不可用，再手动取 Cookie。\n'
            '【注意】MUSIC_U 是 HttpOnly 的，在 Console 里执行 document.cookie\n'
            '看不到它。下面两种方式都行：\n\n'
            '方式一（推荐）\n'
            '  1. 浏览器打开 music.163.com 并登录好你的账号\n'
            '  2. F12 → Application（应用）→ 左侧 Storage → Cookies → music.163.com\n'
            '  3. 找到 MUSIC_U，双击复制它的 Value\n'
            '  4. 粘进上面的 Cookie 框（只填值即可，程序会自动补成 MUSIC_U=…）\n\n'
            '方式二（复制整串）\n'
            '  1. F12 → Network（网络）→ 刷新页面\n'
            '  2. 随便点一个发往 music.163.com 的请求\n'
            '  3. 右侧 Headers → Request Headers → 找到 Cookie 这一行\n'
            '  4. 整行的值全部复制，粘进来（整串或只填 MUSIC_U 的值都能识别）\n\n'
            '填好后点「验证」：会显示登录的昵称、会员类型，并真的去取一次流，\n'
            '直接告诉你到底能不能下付费曲目。\n\n'
            'Cookie 只保存在本机 ~/.ncm/gui.json，不会外发。\n'
            '但它等同于账号凭证 —— 不要分享给别人，也不要贴到聊天记录里。')

    def set_busy(self, busy):
        self.busy = busy
        self.btn_start.configure(state='disabled' if busy else 'normal')
        self.btn_stop.configure(state='normal' if busy else 'disabled')

    # ------------------------------------------------------------- 快捷登录
    def on_web_login(self):
        """网页登录 + 自动读取浏览器 Cookie。

        比扫码稳：走的是官方网页登录，不经过第三方 API 的风控面（扫码那条路
        现在会被 8821 拦掉）。登录态从浏览器本地 cookie 库读，密钥用 DPAPI 解。
        """
        if self.busy:
            return
        try:
            from gui_login import WebLoginDialog
        except Exception as e:
            messagebox.showerror('无法启动', '登录模块加载失败：\n%s' % e)
            return
        WebLoginDialog(self.winfo_toplevel(), self._on_web_success)

    def _on_web_success(self, cookie, src):
        self.var_cookie.set(cookie)
        self.settings['cookie'] = cookie
        self.app.persist()
        self.log('√ 已从 %s 读取到登录态，Cookie 已保存到本机' % (src or '浏览器'))
        self.on_verify()

    def on_qr_login(self):
        """扫码登录：不用去开发者工具里翻 Cookie，也不用输密码"""
        if self.busy:
            return
        try:
            from gui_login import QrLoginDialog, qr_available
        except Exception as e:
            messagebox.showerror('无法扫码登录', '登录模块加载失败：\n%s' % e)
            return
        ok, why = qr_available()
        if not ok:
            messagebox.showerror(
                '缺少依赖',
                '%s\n\n内置运行时本来自带全部依赖，出现这个提示说明 python\\ 目录没有完整解压。\n'
                '请把整个文件夹重新解压后再试（便携包的 pip 也未打包，无法现场安装）。' % why)
            return
        QrLoginDialog(self.winfo_toplevel(), self._on_qr_success)

    def _on_qr_success(self, cookie, nick=None, vlabel=None):
        """扫码成功且账号校验通过后：写入 Cookie 框、落盘，并再验证一次（带取流实测）"""
        self.var_cookie.set(cookie)
        self.settings['cookie'] = cookie
        self.app.persist()
        who = '%s%s' % (nick, '（%s）' % vlabel if vlabel else '') if nick else '(未取得昵称)'
        self.log('√ 扫码登录成功：%s，Cookie 已保存到本机' % who)
        self.on_verify()

    # ------------------------------------------------------------- 登录校验
    def on_verify(self):
        if self.busy:
            return
        cookie = self.var_cookie.get().strip()
        quality = dict(QUALITIES)[self.var_quality.get()]
        # 优先拿队列里第一首做实测；队列空就用《海阔天空》（付费曲目，正好当探针）
        probe = self.tracks[0]['song'].get('id') if self.tracks else 347230
        self.set_login_status('校验中…', C_INFO)
        threading.Thread(target=self._verify_worker,
                         args=(cookie, quality, probe), daemon=True).start()

    def _verify_worker(self, cookie, quality, probe_id):
        try:
            apply_cookie(cookie)
            import requests
            session = requests.Session()
            session.headers.update(headers)
            self.q.put(('log', '—— 校验登录态 ——'))

            if not has_login(cookie):
                self.q.put(('verify', '未填写 Cookie', 'bad'))
                self.q.put(('log', '× Cookie 框是空的。付费/VIP 曲目必须带登录态，'
                                   '且里面要含 MUSIC_U。点「说明」看怎么取。'))
                return

            prof = fetch_account(session)
            if not prof:
                self.q.put(('verify', 'Cookie 无效或已过期', 'bad'))
                self.q.put(('log', '× 账号接口返回空 —— Cookie 可能过期，或被复制时截断了。'))
                return

            nick = prof.get('nickname') or '(未知昵称)'
            vtype = prof.get('vipType')
            label = VIP_TYPE_LABEL.get(vtype, '类型码 %s' % vtype)
            self.q.put(('log', '√ 已登录：%s    vipType=%s（%s）' % (nick, vtype, label)))

            vi = fetch_vip_info(session)
            if vi:
                keep = ('redVipLevel', 'redVipAnnualCount', 'musicPackage', 'associateVip')
                self.q.put(('log', '  会员信息：%s' % json.dumps(
                    {k: vi.get(k) for k in keep if k in vi}, ensure_ascii=False)))

            # 真正决定成败的是能不能取到流，所以直接实测一次
            self.q.put(('log', '  实测取流（歌曲 id=%s，目标音质 %s）…' % (probe_id, quality)))
            play, lvl, code = self._fetch_play_url(session, probe_id, quality)
            if not play:
                reason = PLAY_FAIL_REASON.get(code, '未知原因（code=%s）' % code)
                self.q.put(('verify', '已登录 %s，但取流失败' % nick, 'warn'))
                self.q.put(('log', '  × %s' % reason))
                return
            if play.get('freeTrialInfo'):
                self.q.put(('verify', '已登录 %s，但只能拿到试听片段' % nick, 'warn'))
                self.q.put(('log', '  ! 该曲返回的是试听片段，说明账号权限不足以完整下载。'
                                   '确认是 SVIP 账号、且这首没被单独下架。'))
                return
            br = play.get('br')
            self.q.put(('verify', '已登录：%s（%s）· 取流正常 %s' % (nick, label, play.get('type') or ''), 'ok'))
            self.q.put(('log', '  √ 取流成功：音质档位 %s / 格式 %s / 码率 %s%s'
                        % (lvl, play.get('type'), br,
                           '（已从所选音质降级，说明该账号拿不到更高档）' if lvl != quality else '')))
        except Exception as e:
            self.q.put(('verify', '校验出错', 'bad'))
            self.q.put(('log', '× 校验异常：%s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('verified',))

    # ----------------------------------------------------------------- 搜索
    def _read_search_limit(self):
        try:
            n = int(self.var_search_n.get())
        except Exception:
            n = 20
        n = max(1, min(200, n))
        try:
            self.var_search_n.set(n)
        except Exception:
            pass
        return n

    def on_search(self):
        if self.busy:
            return
        kw = self.var_search.get().strip()
        if not kw:
            messagebox.showwarning('没有关键词', '请输入歌名或歌手名。')
            return
        self.clear_list()
        self.set_busy(True)
        self.lbl_stat.configure(text='搜索中…')
        self.stop_event.clear()
        # Tk 变量只能在主线程读
        cookie = self.var_cookie.get().strip()
        threading.Thread(target=self._search_worker,
                         args=(kw, self._read_search_limit(), cookie), daemon=True).start()

    def _search_worker(self, keywords, limit, cookie):
        try:
            self.settings['cookie'] = cookie
            apply_cookie(cookie)
            import requests
            session = requests.Session()
            session.headers.update(headers)
            self.q.put(('log', '搜索「%s」，取前 %d 首…' % (keywords, limit)))
            songs, total = search_songs(session, keywords, limit=limit)
            if not songs:
                self.q.put(('log', '× 没有搜到结果（共 %d 条匹配）' % total))
                self.q.put(('parsed',))
                return
            src = '搜索：%s' % keywords
            tracks = [{'song': s, 'is_program': False, 'collection': None,
                       'source': src, 'failed': False} for s in songs]
            self.q.put(('tracks', tracks))
            self.q.put(('log', '共匹配 %d 条，已取前 %d 首' % (total, len(songs))))
            if total > len(songs):
                self.q.put(('log', '  · 想多要一些就把「取前 N 首」调大'))
        except Exception as e:
            self.q.put(('log', '× 搜索出错: %s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('parsed',))

    # ----------------------------------------------------------------- 解析
    def on_parse(self):
        if self.busy:
            return
        lines = self.get_input_lines()
        if not lines:
            messagebox.showwarning('没有输入', '请粘贴至少一个网易云链接或 ID（每行一个）。')
            return
        self.clear_list()
        self.set_busy(True)
        self.lbl_stat.configure(text='解析中…')
        self.stop_event.clear()
        # 注意：Tk 变量只能在主线程读，子线程碰它会抛 "main thread is not in main loop"
        cookie = self.var_cookie.get().strip()
        force = self.var_kind.get()
        threading.Thread(target=self._parse_worker,
                         args=(lines, force, cookie), daemon=True).start()

    def _resolve_one(self, api, kind, tid):
        """解析一个合集类下载源，返回 (tracks, 合集名)"""
        def wrap(songs, is_program, coll, src):
            return [{'song': s, 'is_program': is_program, 'collection': coll,
                     'source': src or '合集', 'failed': False} for s in songs], coll

        if kind == 'album':
            songs = api.get_album_songs(tid)
            coll = '%s - album' % songs[0]['album']['name'] if songs else None
            return wrap(songs, False, coll, coll)
        if kind == 'artist':
            songs = api.get_hot_songs(tid)
            coll = '%s - hot50' % songs[0]['artists'][0]['name'] if songs else None
            return wrap(songs, False, coll, coll)
        if kind == 'playlist':
            track_ids, pname = api.get_playlist_songs(tid)
            ids = [t['id'] for t in track_ids]
            songs = fetch_song_details(api.session, ids)
            coll = '%s - playlist' % pname
            return wrap(songs, False, coll, coll)
        if kind == 'radio':
            programs = api.get_radio_programs(tid)
            name = ((programs[0].get('radio') or {}).get('name') if programs else None) or 'unknown'
            coll = '%s - radio' % name
            return wrap(programs, True, coll, coll)
        if kind == 'program':
            p = api.get_program(tid)
            if not p:
                return [], None
            coll = '%s - program' % p['dj']['brand']
            return wrap([p], True, coll, coll)
        return [], None

    def _parse_worker(self, lines, force_kind, cookie):
        try:
            self.settings['cookie'] = cookie
            apply_cookie(cookie)
            from ncm.api import CloudApi
            api = CloudApi()

            # 1) 逐行拆成 (kind, id)，顺带按 (kind,id) 去重
            targets, bad, seen = [], [], set()
            for ln in lines:
                k, tid = parse_target(ln, force_kind)
                if not (k and tid):
                    bad.append(ln)
                    continue
                if (k, tid) in seen:
                    continue
                seen.add((k, tid))
                targets.append((k, tid, ln))

            for ln in bad[:5]:
                self.q.put(('log', '  · 无法识别，已跳过：%s' % ln[:80]))
            if len(bad) > 5:
                self.q.put(('log', '  · 另有 %d 行无法识别' % (len(bad) - 5)))
            if len(targets) < len(lines) - len(bad):
                self.q.put(('log', '已合并 %d 个重复的下载源'
                            % (len(lines) - len(bad) - len(targets))))
            self.q.put(('log', '共 %d 个下载源，开始解析…' % len(targets)))

            # 2) 纯单曲走批量详情接口：一次 200 首，比逐首请求快一个数量级
            song_ids = [int(tid) for k, tid, _ in targets if k == 'song']
            others = [(k, tid, ln) for k, tid, ln in targets if k != 'song']
            tracks = []
            if song_ids:
                self.q.put(('log', '批量拉取 %d 首单曲详情…' % len(song_ids)))
                got = fetch_song_details(api.session, song_ids)
                by_id = {s.get('id'): s for s in got}
                for sid in song_ids:
                    s = by_id.get(sid)
                    if s:
                        tracks.append({'song': s, 'is_program': False, 'collection': None,
                                       'source': '单曲', 'failed': False})
                missing = [i for i in song_ids if i not in by_id]
                if missing:
                    self.q.put(('log', '  · %d 首取不到详情（可能已下架）：%s'
                                % (len(missing),
                                   ', '.join(str(x) for x in missing[:8]))))

            # 3) 专辑/歌单/歌手/电台各自并发解析
            if others:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    futs = {ex.submit(self._resolve_one, api, k, tid): (k, tid, ln)
                            for k, tid, ln in others}
                    for fut in as_completed(futs):
                        k, tid, ln = futs[fut]
                        try:
                            got, coll = fut.result()
                        except Exception as e:
                            self.q.put(('log', '  × %s 解析失败：%s' % (ln[:60], e)))
                            continue
                        if not got:
                            self.q.put(('log', '  × %s 没有解析到曲目' % ln[:60]))
                            continue
                        self.q.put(('log', '√ %s → %d 首' % (coll or ln[:40], len(got))))
                        tracks.extend(got)

            # 4) 跨来源去重：同一首歌出现在多个歌单里只下一次
            uniq, ids = [], set()
            for t in tracks:
                sid = t['song'].get('id')
                if sid in ids:
                    continue
                ids.add(sid)
                uniq.append(t)
            if len(uniq) < len(tracks):
                self.q.put(('log', '已合并 %d 首重复曲目（多个来源含同一首）'
                            % (len(tracks) - len(uniq))))

            if not uniq:
                self.q.put(('log', '× 没有解析到任何曲目，请检查链接或 ID。'))
            else:
                self.q.put(('tracks', uniq))
        except Exception as e:
            self.q.put(('log', '× 解析出错: %s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('parsed',))

    # ----------------------------------------------------------------- 下载
    def on_start(self):
        if self.busy:
            return
        if not self.tracks:
            messagebox.showwarning('列表为空', '请先在输入框粘贴链接（每行一个），点「解析列表」。')
            return
        self.collect()
        self.app.persist()
        self.app.sync_dir(self.settings['download_dir'])
        # 付费曲目要到取流那一步才会失败，提前说一声省得白等
        if not has_login(self.settings.get('cookie', '')):
            self.log('提示：Cookie 框为空 —— 免费曲目能下，付费/VIP 会返回 -110；'
                     '无损/Hi-Res 也基本都需要登录态。')

        self.stop_event.clear()
        self.set_busy(True)
        self.lbl_stat.configure(text='下载中…')
        self._reset_status()
        threading.Thread(target=self._download_worker, daemon=True).start()

    def on_stop(self):
        self.stop_event.set()
        self.log('收到停止请求，正在中断…')

    def _fill_tree(self):
        """按 self.tracks 重建列表（批量解析后、以及重试筛选用）"""
        for i in self.tree.get_children():
            self.tree.delete(i)
        for i, tr in enumerate(self.tracks):
            t, a, al, _ = song_meta(tr['song'], tr['is_program'])
            self.tree.insert('', 'end', iid=str(i), values=(
                i + 1, t, a, al, tr.get('source') or '单曲', '等待'))
        self.bar_all['value'] = 0
        self.lbl_all.configure(text='0 / %d' % len(self.tracks))

    def _reset_status(self):
        for item in self.tree.get_children():
            self.tree.set(item, 'status', '等待')
            self.tree.item(item, tags=())
        for t in self.tracks:
            t['failed'] = False
        total = len(self.tracks)
        self.bar_all['value'] = 0
        self.lbl_all.configure(text='0 / %d' % total)

    def on_retry_failed(self):
        """只把上一轮失败的曲目留下重下，成功的不用再走一遍"""
        if self.busy:
            return
        keep = [t for t in self.tracks if t.get('failed')]
        if not keep:
            messagebox.showinfo('没有失败项', '上一次下载没有失败的曲目。')
            return
        self.tracks = keep
        self._fill_tree()
        self.log('已筛出 %d 首失败曲目，开始重试' % len(keep))
        self.on_start()

    def _download_worker(self):
        s = self.settings
        done = ok = skip = fail = 0
        total = len(self.tracks)
        try:
            apply_cookie(s['cookie'])
            import requests
            session = requests.Session()
            session.headers.update(headers)
            out_dir = s['download_dir']

            interval = float(s.get('download_interval') or 0)
            for idx, tr in enumerate(self.tracks):
                if self.stop_event.is_set():
                    self.q.put(('log', '已停止。'))
                    break

                # 批量下载的节奏控制：短时间高频请求是风控最敏感的模式。
                # 用 wait 而不是 sleep，这样停止请求能立刻打断等待。
                if idx and interval > 0:
                    if self.stop_event.wait(interval):
                        self.q.put(('log', '已停止。'))
                        break

                song, is_program = tr['song'], tr['is_program']
                title, artist, album, track = song_meta(song, is_program)
                self.q.put(('row_status', idx, '下载中', 'run'))
                self.q.put(('log', '[%d/%d] %s — %s' % (idx + 1, total, title, artist)))

                try:
                    res = self._download_one(session, song, is_program, out_dir,
                                             idx, total, tr.get('collection'))
                except StopRequested:
                    self.q.put(('row_status', idx, '已取消', 'skip'))
                    self.q.put(('log', '已停止。'))
                    break
                except Exception as e:
                    res = ('fail', '异常: %s' % e)
                    self.q.put(('log', traceback.format_exc()))

                status, detail = res
                if status == 'ok':
                    ok += 1
                    self.q.put(('row_status', idx, detail or '完成', 'ok'))
                elif status == 'skip':
                    skip += 1
                    self.q.put(('row_status', idx, detail or '已存在', 'skip'))
                else:
                    fail += 1
                    tr['failed'] = True
                    self.q.put(('row_status', idx, detail or '失败', 'fail'))
                    self.q.put(('log', '  × %s' % (detail or '失败')))

                done += 1
                self.q.put(('overall', done, total))
                self.q.put(('file_progress', 0, 0, ''))

            self.q.put(('log', '—— 结束：成功 %d，跳过 %d，失败 %d，共 %d ——' % (ok, skip, fail, total)))
        except Exception as e:
            self.q.put(('log', '× 下载线程异常: %s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('finished',))

    @staticmethod
    def _same_song(path, song, is_program):
        """磁盘上已有的文件是不是**这首**歌。

        光看文件名不够：不同专辑的同名曲（同歌手同标题）会生成同一个文件名，
        只看"文件存在"就会把后面那首当成已下载而跳过。这里读标签比对
        标题/歌手/专辑，才能区分「已下过」和「文件名撞车」。
        """
        try:
            title, artist, album, _ = song_meta(song, is_program)
            ext = os.path.splitext(path)[1].lower()
            if ext == '.flac':
                from mutagen.flac import FLAC
                a = FLAC(path)
                g = lambda k: ((a.get(k) or [''])[0])
                return g('title') == title and g('artist') == artist
            if ext == '.mp3':
                from mutagen.id3 import ID3
                t = ID3(path)
                return (str(t.get('TIT2') or '') == title
                        and str(t.get('TPE1') or '') == artist
                        and str(t.get('TALB') or '') == album)
        except Exception:
            return False
        return False

    @staticmethod
    def _disambiguation_names(base, ext, song):
        """撞车时可能用到的候选文件名（按顺序填）"""
        sid = song.get('id')
        yield '%s [%s]%s' % (base, sid, ext)
        for n in range(1, 50):
            yield '%s [%s-%d]%s' % (base, sid, n, ext)

    @classmethod
    def _find_disambiguated(cls, folder, base, ext, song, is_program):
        """之前是否已经用带 id 的名字存过这首；有就返回路径。

        没有这一步的话，重复运行会对同一首歌不断生成 [id-1]、[id-2]… 
        —— 因为它每次都去找"下一个空名字"。
        """
        for name in cls._disambiguation_names(base, ext, song):
            p = os.path.join(folder, name)
            if not os.path.exists(p):
                return None      # 按顺序填的，遇到空位说明前面都查过了
            if cls._same_song(p, song, is_program):
                return p
        return None

    @classmethod
    def _disambiguated_path(cls, folder, base, ext, song, is_program):
        """给撞车的曲目挑一个没被占用的名字"""
        for name in cls._disambiguation_names(base, ext, song):
            p = os.path.join(folder, name)
            if not os.path.exists(p):
                return p
        return os.path.join(folder, '%s [%s-x]%s' % (base, song.get('id'), ext))

    def _download_one(self, session, song, is_program, out_dir, idx, total, collection=None):
        s = self.settings
        title, artist, album, track = song_meta(song, is_program)
        title, artist, album = safe_name(title), safe_name(artist), safe_name(album)

        audio_id = song['mainSong']['id'] if is_program else song['id']

        # 合集来源各自落到自己的子文件夹；单曲则按「智能分类」设置走
        if collection:
            folder = os.path.join(out_dir, safe_name(collection))
        elif s['folder_type'] == 2:
            folder = os.path.join(out_dir, artist)
        elif s['folder_type'] == 3:
            folder = os.path.join(out_dir, artist, album)
        else:
            folder = out_dir

        level = s['quality']
        play, got_level, code = self._fetch_play_url(session, audio_id, level)
        if not play:
            # 注意：这里先不建目录。建了却下不成，音乐库里会留下一堆空文件夹。
            return 'fail', PLAY_FAIL_REASON.get(
                code, '无法获取音频地址（code=%s）：可能已下架、无版权或 Cookie 失效' % code)
        os.makedirs(folder, exist_ok=True)

        if got_level != level:
            self.q.put(('log', '  · 所选音质不可用，已降级为 %s' % got_level))

        ext = '.' + (play.get('type') or ('flac' if got_level in FLAC_LEVELS else 'mp3'))
        if s['name_type'] == 2:
            fname = '%s - %s%s' % (artist, title, ext)
        elif s['name_type'] == 3:
            fname = '%s - %s%s' % (title, artist, ext)
        else:
            fname = '%s%s' % (title, ext)
        path = os.path.join(folder, fname)

        if s['skip_existing'] and os.path.exists(path) and os.path.getsize(path) > 1024:
            if self._same_song(path, song, is_program):
                return 'skip', '已存在'
            # 文件名被另一首歌占了。先看之前是不是已经用带 id 的名字存过这首
            base_name = os.path.splitext(fname)[0]
            found = self._find_disambiguated(folder, base_name, ext, song, is_program)
            if found:
                return 'skip', '已存在（带 id 文件名）'
            path = self._disambiguated_path(folder, base_name, ext, song, is_program)
            self.q.put(('log', '  · 文件名与已有曲目重名（不同专辑），改存为 %s'
                        % os.path.basename(path)))

        trial = play.get('freeTrialInfo')
        if trial:
            self.q.put(('log', '  ! 这首只拿到试听片段（账号权限不足或非 VIP 曲目）'))

        self._stream_to_file(session, play['url'], path, path, 'audio')

        cover_path = None
        curl = cover_url_of(song, is_program)
        if curl:
            cover_path = os.path.join(folder, 'cover_%s.jpg' % audio_id)
            try:
                self._stream_to_file(session, curl, cover_path, cover_path, 'cover')
                from ncm.file_util import resize_img
                resize_img(cover_path)
            except Exception as e:
                self.q.put(('log', '  · 封面下载失败，跳过: %s' % e))
                cover_path = None

        # 歌词：抓取 → 存同名 .lrc → 可选嵌入标签
        lrc_text = ''
        if s.get('download_lyrics') or s.get('embed_lyrics'):
            try:
                ly = fetch_lyric(session, audio_id)
                lrc_text = build_lrc_text(ly.get('lrc'), ly.get('tlyric'))
                if lrc_text:
                    base = os.path.splitext(fname)[0]
                    saved = write_lrc_file(folder, base, lrc_text) \
                        if s.get('download_lyrics') else None
                    self.q.put(('log', '  · 歌词 %d 字符%s' % (
                        len(lrc_text), '，已存 .lrc' if saved else '')))
                else:
                    self.q.put(('log', '  · 这首没有歌词'))
            except Exception as e:
                self.q.put(('log', '  · 歌词获取失败: %s' % e))

        raw_title, raw_artist, raw_album, raw_track = song_meta(song, is_program)
        good, msg = write_tags(path, cover_path, raw_title, raw_artist, raw_album, raw_track,
                               lrc_text if s.get('embed_lyrics') else None)
        if not good:
            self.q.put(('log', '  · 标签写入失败: %s' % msg))
        if cover_path and os.path.exists(cover_path):
            try:
                os.remove(cover_path)
            except OSError:
                pass

        size = human_size(os.path.getsize(path))
        br = play.get('br')
        return 'ok', '%s %s' % (size, ('%dk' % (br // 1000)) if br else '')

    def _fetch_play_url(self, session, song_id, level):
        """v1 接口取播放地址，失败则逐级降级"""
        try:
            start = LEVEL_ORDER.index(level)
        except ValueError:
            start = 2
        chain = LEVEL_ORDER[start::-1] or ['standard']

        last_code = None
        for lv in chain:
            if self.stop_event.is_set():
                raise StopRequested()
            encode = 'flac' if lv in FLAC_LEVELS else 'mp3'
            try:
                url = 'http://music.163.com/weapi/song/enhance/player/url/v1?csrf_token='
                params = {'ids': json.dumps([int(song_id)]), 'level': lv,
                          'encodeType': encode, 'csrf_token': ''}
                r = session.post(url, data=encrypted_request(params), timeout=30)
                j = r.json()
                d = (j.get('data') or [None])[0]
                if d and d.get('url'):
                    return d, lv, None
                if d is not None:
                    last_code = d.get('code')
            except Exception as e:
                log_file('fetch_play_url(%s) failed: %s' % (lv, e))

        try:
            url = 'http://music.163.com/weapi/song/enhance/player/url?csrf_token='
            params = {'ids': [int(song_id)], 'br': 320000, 'csrf_token': ''}
            r = session.post(url, data=encrypted_request(params), timeout=30)
            d = (r.json().get('data') or [None])[0]
            if d and d.get('url'):
                return d, 'exhigh(legacy)', None
            if d is not None:
                last_code = d.get('code')
        except Exception as e:
            log_file('legacy url failed: %s' % e)
        return None, None, last_code

    def _stream_to_file(self, session, url, path, final_path, tag):
        """流式下载到 .part 再原子改名。

        任何失败（断流、超时、取消、磁盘满）都必须把 .part 删掉：
        否则网络抖几次，音乐库里就会攒下一堆半截文件。
        """
        tmp = final_path + '.part'
        try:
            # 断点续传：上次中断留下的 .part 从它的大小接着下
            start = 0
            if self.settings.get('resume_partial') and os.path.exists(tmp):
                start = os.path.getsize(tmp)
            hdrs = {'Range': 'bytes=%d-' % start} if start else {}

            with session.get(url, stream=True, timeout=60, headers=hdrs) as r:
                r.raise_for_status()
                # CDN 出错时会以 HTTP 200 返回一段 JSON/HTML（例如 Host 头不对时
                # 的 {"code":404}）。不校验的话就会把这种错误页当成音频写进音乐库。
                ctype = (r.headers.get('Content-Type') or '').lower()
                if 'json' in ctype or 'text' in ctype or 'html' in ctype:
                    peek = b''
                    try:
                        for chunk in r.iter_content(chunk_size=256):
                            peek = chunk
                            break
                    except Exception:
                        pass
                    raise IOError('目标返回的不是音频（Content-Type: %s），'
                                  '内容开头: %r' % (ctype or '空', peek[:120]))

                if start and r.status_code != 206:
                    # 服务器没理会 Range（返回 200 全量），只能重头来
                    self.q.put(('log', '  · 服务器不支持断点续传，改为重新下载'))
                    start = 0
                elif start:
                    self.q.put(('log', '  · 从断点继续（已下 %s）' % human_size(start)))

                length = int(r.headers.get('Content-Length') or 0)
                total = length + start
                got = start
                if start and total:
                    self.q.put(('file_progress', got, total, tag))
                with open(tmp, 'ab' if start else 'wb') as fh:
                    for chunk in r.iter_content(chunk_size=65536):
                        if self.stop_event.is_set():
                            raise StopRequested()
                        if chunk:
                            fh.write(chunk)
                            got += len(chunk)
                            self.q.put(('file_progress', got, total, tag))
            if os.path.exists(final_path):
                os.remove(final_path)
            os.replace(tmp, final_path)
            return got
        except BaseException:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------- 主线程消费队列
    def _drain(self):
        got = 0
        buf = []

        def flush():
            if buf:
                self.log_many(buf)
                del buf[:]

        try:
            while True:
                msg = self.q.get_nowait()
                got += 1
                kind = msg[0]
                if kind == 'log':
                    buf.append(msg[1])
                    continue
                flush()          # 先落日志，保持它和其它更新的先后顺序
                if kind == 'tracks':
                    self.tracks = msg[1]
                    self._fill_tree()
                    srcs = sorted({t.get('source') or '单曲' for t in self.tracks})
                    self.log('√ 解析完成 — 共 %d 首，来自 %d 个来源' % (len(self.tracks), len(srcs)))
                    if len(srcs) > 1:
                        self.log('  来源：%s' % '、'.join(srcs[:8])
                                 + ('…' if len(srcs) > 8 else ''))
                elif kind == 'verify':
                    _, text, level = msg
                    color = {'ok': C_OK, 'warn': C_WARN, 'bad': C_BAD}[level]
                    self.set_login_status(text, color)
                elif kind == 'verified':
                    # 校验线程没来得及给结论就退出时，别把标签卡在"校验中…"
                    if self._login[0] == '校验中…':
                        self.set_login_status('未验证')
                elif kind == 'parsed':
                    self.set_busy(False)
                    self.lbl_stat.configure(text='解析完成' if self.tracks else '解析失败')
                elif kind == 'row_status':
                    _, idx, text, tag = msg
                    iid = str(idx)
                    if self.tree.exists(iid):
                        self.tree.set(iid, 'status', text)
                        self.tree.item(iid, tags=(tag,))
                        self.tree.see(iid)
                elif kind == 'overall':
                    _, done, total = msg
                    self.bar_all['value'] = (done / total * 100) if total else 0
                    self.lbl_all.configure(text='%d / %d' % (done, total))
                elif kind == 'file_progress':
                    _, got, total, tag = msg
                    if total:
                        self.bar_one['value'] = got / total * 100
                        self.lbl_one.configure(text='%s %s / %s' % (tag, human_size(got), human_size(total)))
                    else:
                        self.bar_one['value'] = 0
                        self.lbl_one.configure(text='')
                elif kind == 'finished':
                    self.set_busy(False)
                    self.lbl_stat.configure(text='已完成')
                    self.bar_one['value'] = 0
                    self.lbl_one.configure(text='')
                    self.log('任务结束。')
        except queue.Empty:
            flush()
        # 有积压或仍在跑就快轮询，彻底空闲后放慢
        self.after(BUSY_POLL_MS if (got or self.busy) else IDLE_POLL_MS, self._drain)
