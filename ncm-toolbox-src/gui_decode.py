# -*- coding: utf-8 -*-
"""
「NCM 解码」标签页：把 .ncm 解码/转码为通用音频格式。

不重写解码算法，而是驱动已有的 NCMDecoder.exe：
  * 每个文件一个子进程，Python 侧控制并发 —— 换来精确的单文件状态、可取消、可重试
  * 子进程的 -c 固定为 1，避免与 Python 的并发池叠加
  * 结果以 stdout 的 [OK]/[!!] 行为准，退出码兜底，-log 文件提供 WARN 细节
"""

import os
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import gui_theme
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui_common import (
    BUSY_POLL_MS,
    BITRATE_PRESETS,
    C_BAD,
    C_INFO,
    C_MUTED,
    C_OK,
    DECODER_ERRORS,
    FORMAT_PRESETS,
    IDLE_POLL_MS,
    LOSSLESS_FORMATS,
    append_log,
    enable_drop,
    find_decoder,
    human_size,
    log_file,
    log_file_many,
    safe_name,
    same_file,
    split_drop_paths,
)

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

RE_OK = re.compile(r'\[OK\]\s+(.+?)\s+→\s+(.+?)\s*[\r\n]')
RE_FAIL = re.compile(r'\[!!\]\s+(.+?)\s+失败\((\d+)\)\s*(.*?)\s*[\r\n]')
RE_WARN = re.compile(r'\[WARN\]\s*(.+?)\s*[\r\n]')


class DecodeTab(ttk.Frame):

    # 1003 = 输出文件写入失败。两个源文件的元数据相同时，并发解码会同时选中
    # 同一个候选文件名，撞上"文件正由另一进程使用"。属瞬时冲突，退避重试即可。
    RETRY_CODES = (1003,)
    RETRY_MAX = 3

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.settings = app.settings
        self.files = []            # 绝对路径列表
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self._procs = {}
        self._lock = threading.Lock()
        self._name_lock = threading.Lock()   # 保护"移入音乐库"时的重名判重
        self._tmpdir = None
        self._sizes = {}                     # 入队时缓存文件大小，避免重绘时逐个 stat
        self._file_btns = []
        self._decoder_labels = []            # 所有显示解码器状态的标签
        self._decoder_status = ('', C_MUTED)
        self._build_ui()
        self.after(80, self._drain)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=8, pady=(8, 2))
        for txt, w, cmd, gap in (('添加文件…', 11, self.add_files, 0),
                                 ('添加文件夹…', 13, self.add_folder, 4),
                                 ('移除选中', 10, self.remove_selected, 4),
                                 ('清空', 7, self.clear, 0)):
            b = ttk.Button(bar, text=txt, width=w, command=cmd)
            b.pack(side='left', padx=gap)      # 注意别用 pad 当循环变量，上面那个 pad 是 pack 参数字典
            self._file_btns.append(b)
        self.lbl_drop = ttk.Label(bar, text='（可直接把 .ncm 文件或文件夹拖进下方列表）',
                                  foreground=C_MUTED)
        self.lbl_drop.pack(side='left', padx=12)

        # ---- 参数 ----
        opt = ttk.LabelFrame(self, text=' 解码参数 ')
        opt.pack(fill='x', **pad)

        r1 = ttk.Frame(opt)
        r1.pack(fill='x', padx=8, pady=6)
        ttk.Label(r1, text='输出格式:').pack(side='left')
        self.var_format = tk.StringVar()
        cb = ttk.Combobox(r1, textvariable=self.var_format, width=20,
                          values=[n for n, _ in FORMAT_PRESETS])
        cb.pack(side='left', padx=(4, 2))
        cb.bind('<<ComboboxSelected>>', lambda e: self._sync_bitrate_state())
        cb.bind('<KeyRelease>', lambda e: self._sync_bitrate_state())
        ttk.Label(r1, text='（可直接输入扩展名）', foreground=C_MUTED).pack(side='left')

        ttk.Label(r1, text='码率:').pack(side='left', padx=(14, 0))
        self.var_bitrate = tk.StringVar()
        self.cb_bitrate = ttk.Combobox(r1, textvariable=self.var_bitrate, width=8,
                                       values=BITRATE_PRESETS)
        self.cb_bitrate.pack(side='left', padx=4)
        ttk.Label(r1, text='kbps').pack(side='left')

        ttk.Label(r1, text='并发:').pack(side='left', padx=(14, 0))
        self.var_conc = tk.IntVar(value=4)
        ttk.Spinbox(r1, from_=1, to=8, width=4, textvariable=self.var_conc).pack(side='left', padx=4)

        self.var_cover = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text='联网嵌入封面', variable=self.var_cover).pack(side='left', padx=14)

        r2 = ttk.Frame(opt)
        r2.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(r2, text='输出目录:').pack(side='left')
        self.var_dir = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_dir).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(r2, text='统一到下载目录', width=15,
                   command=self.use_shared_dir).pack(side='left')

        r3 = ttk.Frame(opt)
        r3.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(r3, text='解码器:').pack(side='left')
        self.var_decoder = tk.StringVar()
        ent = ttk.Entry(r3, textvariable=self.var_decoder)
        ent.pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(r3, text='浏览…', width=8, command=self.pick_decoder).pack(side='left')
        self.lbl_decoder = ttk.Label(r3, text='', width=34)
        self.lbl_decoder.pack(side='left', padx=6)

        # ---- 任务列表 ----
        mid = ttk.LabelFrame(self, text=' 任务列表 ')
        mid.pack(fill='both', expand=True, **pad)
        cols = ('idx', 'file', 'size', 'status', 'output')
        self.tree = ttk.Treeview(mid, columns=cols, show='headings', height=7)
        for c, t, w in (('idx', '#', 46), ('file', '文件', 300), ('size', '大小', 90),
                        ('status', '状态', 190), ('output', '输出', 300)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor='w',
                             stretch=(c in ('file', 'status', 'output')))
        vs = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        vs.pack(side='right', fill='y', pady=6)
        self.tree.tag_configure('ok', foreground=C_OK)
        self.tree.tag_configure('fail', foreground=C_BAD)
        self.tree.tag_configure('skip', foreground=C_MUTED)
        self.tree.tag_configure('run', foreground=C_INFO)
        self.tree.bind('<Delete>', lambda e: self.remove_selected())
        enable_drop(self.tree, self._on_drop)

        # ---- 操作 ----
        act = ttk.Frame(self)
        act.pack(fill='x', **pad)
        self.btn_start = ttk.Button(act, text='开始解码', width=12, command=self.on_start,
                                    style='Primary.TButton')
        self.btn_start.pack(side='left')
        self.btn_stop = ttk.Button(act, text='停止', width=8,
                                   command=self.on_stop, state='disabled')
        self.btn_stop.pack(side='left', padx=6)
        ttk.Button(act, text='打开输出目录', width=14, command=self.open_dir).pack(side='left', padx=6)
        self.lbl_stat = ttk.Label(act, text='就绪')
        self.lbl_stat.pack(side='right')

        prg = ttk.Frame(self)
        prg.pack(fill='x', **pad)
        ttk.Label(prg, text='总进度', width=7).pack(side='left')
        self.bar = ttk.Progressbar(prg, mode='determinate', maximum=100)
        self.bar.pack(side='left', fill='x', expand=True, padx=6)
        self.lbl_prog = ttk.Label(prg, text='0 / 0', width=12)
        self.lbl_prog.pack(side='left')

        logf = ttk.LabelFrame(self, text=' 日志 ')
        logf.pack(fill='both', expand=True, **pad)
        self.txt = tk.Text(logf, height=10, wrap='word', state='disabled',
                           background=gui_theme.LOG_BG, foreground=gui_theme.LOG_FG,
                           insertbackground=gui_theme.LOG_FG, relief='flat',
                           highlightthickness=1, highlightbackground=gui_theme.BORDER,
                           font=gui_theme.FONT_MONO, spacing1=2, spacing3=2)
        ls = ttk.Scrollbar(logf, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=ls.set)
        self.txt.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        ls.pack(side='right', fill='y', pady=6)

    # ------------------------------------------------------------- 设置绑定
    def restore(self):
        s = self.settings
        name = dict(FORMAT_PRESETS).get(s.get('decode_format', 'auto'))
        self.var_format.set(name or s.get('decode_format') or 'auto')
        self.var_bitrate.set(s.get('decode_bitrate', '') or '')
        self.var_conc.set(int(s.get('decode_concurrency', 4) or 4))
        self.var_cover.set(bool(s.get('decode_cover', False)))
        self.var_dir.set(s.get('download_dir') or '')
        dec = find_decoder(s.get('decoder_path'))
        self.var_decoder.set(dec or s.get('decoder_path', '') or '')
        self._sync_bitrate_state()
        self._refresh_decoder_label()

    def set_dir(self, value):
        self.var_dir.set(value)

    def use_shared_dir(self):
        self.app.sync_dir(self.app.settings.get('download_dir', ''))

    def _format_key(self):
        """下拉框显示名 → 格式键（也允许直接输入扩展名）"""
        txt = (self.var_format.get() or '').strip()
        for label, key in FORMAT_PRESETS:
            if txt == label:
                return key
        return txt.lower()

    def _sync_bitrate_state(self):
        key = self._format_key()
        disabled = key in LOSSLESS_FORMATS
        self.cb_bitrate.configure(state='disabled' if disabled else 'normal')
        if disabled:
            self.var_bitrate.set('')

    def register_decoder_label(self, lbl):
        """同登录状态：登记制，设置对话框关掉后不会再被写到已销毁的控件"""
        if lbl not in self._decoder_labels:
            self._decoder_labels.append(lbl)
        self._push_decoder_status()

    def unregister_decoder_label(self, lbl):
        if lbl in self._decoder_labels:
            self._decoder_labels.remove(lbl)

    def _push_decoder_status(self):
        text, color = self._decoder_status
        for lbl in list(self._decoder_labels):
            try:
                lbl.configure(text=text, foreground=color)
            except Exception:
                try:
                    self._decoder_labels.remove(lbl)
                except ValueError:
                    pass

    def _refresh_decoder_label(self):
        p = find_decoder(self.var_decoder.get())
        if p:
            try:
                sz = human_size(os.path.getsize(p))
            except OSError:
                sz = '?'
            self._decoder_status = ('已找到 ✓ %s' % sz, C_OK)
        else:
            # 状态标签是固定 34 列宽（约 242px），文案长了会被裁掉，这里保持简短；
            # 完整说明在「开始解码」的报错框和设置页的「说明」行里。
            self._decoder_status = ('未找到解码器（本包未附带）✗', C_BAD)
        self._push_decoder_status()

    def pick_decoder(self):
        p = filedialog.askopenfilename(
            title='选择 NCMDecoder.exe',
            filetypes=[('可执行文件', '*.exe'), ('全部文件', '*.*')])
        if p:
            self.var_decoder.set(os.path.normpath(p))
            self._refresh_decoder_label()

    # ------------------------------------------------------------- 文件管理
    def _add_paths(self, paths):
        """把文件/文件夹加入队列。

        解码进行中一律拒绝：此时 _rebuild_tree 会把所有状态刷回「等待」，
        而行号是结果回填的依据，重建会让状态错位到别的文件上，
        新加进来的文件又不会被正在跑的线程处理。
        """
        if self.busy:
            self.log('· 解码进行中，暂不能修改队列（可先点「停止」）')
            return 0
        seen = set(self.files)      # 原先用 list 判重，上千个文件时是 O(n²)
        added = 0
        for p in paths:
            if os.path.isdir(p):
                for root, _dirs, names in os.walk(p):
                    for n in sorted(names):
                        if n.lower().endswith('.ncm'):
                            fp = os.path.join(root, n)
                            if fp not in seen:
                                seen.add(fp)
                                self.files.append(fp)
                                self._remember_size(fp)
                                added += 1
            elif os.path.isfile(p) and p.lower().endswith('.ncm'):
                if p not in seen:
                    seen.add(p)
                    self.files.append(p)
                    self._remember_size(p)
                    added += 1
        self._rebuild_tree()
        self.log('已添加 %d 个文件（队列共 %d 个）' % (added, len(self.files)))
        return added

    def _remember_size(self, path):
        try:
            self._sizes[path] = os.path.getsize(path)
        except OSError:
            self._sizes[path] = None

    def add_files(self):
        ps = filedialog.askopenfilenames(
            title='选择 .ncm 文件',
            filetypes=[('NCM 文件', '*.ncm'), ('全部文件', '*.*')])
        if ps:
            self._add_paths([os.path.normpath(p) for p in ps])

    def add_folder(self):
        d = filedialog.askdirectory(title='选择包含 .ncm 的文件夹')
        if d:
            self._add_paths([os.path.normpath(d)])

    def _on_drop(self, event):
        paths = split_drop_paths(self, event.data)
        # 拖到 EXE 图标那种多参数场景也一并处理
        if paths:
            self._add_paths([os.path.normpath(p) for p in paths])
        return event.action if hasattr(event, 'action') else None

    def remove_selected(self):
        if self.busy:
            return
        sel = set(self.tree.selection())
        keep = [f for i, f in enumerate(self.files) if str(i) not in sel]
        self.files = keep
        self._rebuild_tree()

    def clear(self):
        if self.busy:
            return
        self.files = []
        self._sizes = {}
        self._rebuild_tree()

    def _rebuild_tree(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for i, f in enumerate(self.files):
            # 大小在入队时已缓存，这里不再逐个 stat —— 否则上千个文件会卡住界面
            n = self._sizes.get(f)
            sz = human_size(n) if n else '-'
            self.tree.insert('', 'end', iid=str(i),
                             values=(i + 1, os.path.basename(f), sz, '等待', ''))
        self.lbl_prog.configure(text='0 / %d' % len(self.files))
        self.bar['value'] = 0

    def open_dir(self):
        d = self.var_dir.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showwarning('目录不存在', '输出目录还不存在：\n%s' % d)
            return
        try:
            os.startfile(d)
        except Exception as e:
            messagebox.showerror('打开失败', str(e))

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

    def set_busy(self, busy):
        self.busy = busy
        self.btn_start.configure(state='disabled' if busy else 'normal')
        self.btn_stop.configure(state='normal' if busy else 'disabled')
        # 运行中把文件管理按钮也禁掉：队列一旦变动，结果就没法按行号回填了
        for b in self._file_btns:
            b.configure(state='disabled' if busy else 'normal')

    # ----------------------------------------------------------------- 执行
    def _read_concurrency(self):
        """Spinbox 允许任意键入，IntVar.get() 遇到非数字会抛 TclError —— 必须兜住，
        否则点「开始解码」时异常直接冒泡到 Tk，按钮看起来像完全没反应。"""
        try:
            n = int(self.var_conc.get())
        except Exception:
            n = 4
        n = max(1, min(8, n))
        try:
            self.var_conc.set(n)    # 回写纠正后的值，让用户看到实际生效的并发
        except Exception:
            pass
        return n

    def on_start(self):
        if self.busy:
            return
        if not self.files:
            messagebox.showwarning('队列为空', '请先添加 .ncm 文件或文件夹。')
            return
        dec = find_decoder(self.var_decoder.get())
        if not dec:
            messagebox.showerror(
                '找不到解码器',
                'NCMDecoder.exe 是外部独立程序，本便携包没有附带。\n\n'
                '拿到解码器之后二选一：\n'
                '  · 把它放进本文件夹（或其 dist\\ 子目录），会自动识别\n'
                '  · 或在上方「解码参数 → 解码器」框里填完整路径 / 点「浏览…」指定')
            return
        outdir = self.var_dir.get().strip()
        if not outdir:
            messagebox.showwarning('缺少输出目录', '请先指定输出目录。')
            return

        self.settings.update({
            'decoder_path': self.var_decoder.get().strip(),
            'decode_format': self._format_key(),
            'decode_bitrate': self.var_bitrate.get().strip(),
            'decode_concurrency': self._read_concurrency(),
            'decode_cover': bool(self.var_cover.get()),
        })
        self.app.persist()
        self.app.sync_dir(outdir)

        self.stop_event.clear()
        self._tmpdir = tempfile.mkdtemp(prefix='ncmdec_')
        self.set_busy(True)
        self.lbl_stat.configure(text='解码中…')
        for i in range(len(self.files)):
            if self.tree.exists(str(i)):
                self.tree.set(str(i), 'status', '等待')
                self.tree.set(str(i), 'output', '')
                self.tree.item(str(i), tags=())
        self.bar['value'] = 0
        threading.Thread(target=self._worker, args=(dec, outdir), daemon=True).start()

    def on_stop(self):
        self.stop_event.set()
        self.log('收到停止请求，正在结束子进程…')
        with self._lock:
            for p in list(self._procs.values()):
                try:
                    p.terminate()
                except Exception:
                    pass

    def _worker(self, decoder, outdir):
        files = list(self.files)
        total = len(files)
        conc = max(1, min(8, int(self.settings.get('decode_concurrency', 4))))
        done = ok = fail = cancelled = dup = 0
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self.q.put(('log', '× 输出目录不可用: %s' % e))
            self.q.put(('finished',))
            return

        # 清理上次异常退出（断电/强杀）可能残留的临时目录
        try:
            for name in os.listdir(outdir):
                if name.startswith('_ncm_tmp_'):
                    shutil.rmtree(os.path.join(outdir, name), ignore_errors=True)
        except OSError:
            pass

        self.q.put(('log', '开始解码 %d 个文件，并发 %d，格式 %s，输出 %s'
                    % (total, conc, self.settings['decode_format'], outdir)))

        try:
            with ThreadPoolExecutor(max_workers=conc) as ex:
                futs = {ex.submit(self._decode_one, decoder, i, p, outdir): i
                        for i, p in enumerate(files)}
                for fut in as_completed(futs):
                    idx = futs[fut]
                    try:
                        status, text, outp, warns = fut.result()
                    except Exception as e:
                        status, text, outp, warns = 'fail', '异常: %s' % e, '', []
                        self.q.put(('log', traceback.format_exc()))
                    tag = {'ok': 'ok', 'fail': 'fail', 'skip': 'skip',
                           'dup': 'skip'}.get(status, 'fail')
                    self.q.put(('row', idx, text, tag, outp))
                    if status == 'ok':
                        ok += 1
                    elif status == 'dup':
                        dup += 1
                    elif status == 'skip':
                        cancelled += 1
                    else:
                        fail += 1
                        self.q.put(('log', '  × %s：%s' % (os.path.basename(files[idx]), text)))
                    for w in warns:
                        self.q.put(('log', '  ! %s: %s' % (os.path.basename(files[idx]), w)))
                    done += 1
                    self.q.put(('progress', done, total))
        except Exception as e:
            self.q.put(('log', '× 解码线程异常: %s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('log', '—— 结束：成功 %d，已存在 %d，失败 %d，取消 %d，共 %d ——'
                        % (ok, dup, fail, cancelled, total)))
            self.q.put(('finished',))

    @staticmethod
    def _read_warns(logf):
        """读 -log 文件里的 WARN（例如转码失败已降级为原格式）"""
        warns = []
        try:
            with open(logf, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    m = RE_WARN.search(line)
                    if m:
                        # 转码失败会把 ffmpeg 的多行输出带进来，压成一行
                        warns.append(re.sub(r'\s+', ' ', m.group(1).strip())[:220])
        except Exception:
            pass
        return warns

    def _build_cmd(self, decoder, path, outdir, logf):
        cmd = [decoder, '-i', path, '-o', outdir, '-c', '1', '-log', logf]
        fmt = self.settings.get('decode_format', 'auto')
        if fmt and fmt != 'auto':
            cmd += ['-f', fmt]
        br = (self.settings.get('decode_bitrate') or '').strip()
        if br and fmt.lower() not in LOSSLESS_FORMATS:
            cmd += ['-b', br]
        if self.settings.get('decode_cover'):
            cmd += ['-cover']
        return cmd

    def _run_decoder(self, decoder, idx, path, outdir, logf):
        """跑一次解码；返回 (status, 文本, 输出路径, 警告, 错误码)"""
        try:
            p = subprocess.Popen(
                self._build_cmd(decoder, path, outdir, logf),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            return 'fail', '启动解码器失败: %s' % e, '', [], None

        with self._lock:
            self._procs[idx] = p
        try:
            so, _se = p.communicate()
        except Exception as e:
            return 'fail', '等待解码器失败: %s' % e, '', [], None
        finally:
            with self._lock:
                self._procs.pop(idx, None)

        txt = (so or b'').decode('utf-8', 'replace')

        # 先看是否真的成功了 —— 已完成的任务不能因为随后点了停止而被误标为取消
        if p.returncode == 0:
            m = RE_OK.search(txt)
            if m:
                return 'ok', '完成', m.group(2).strip(), self._read_warns(logf), 0
        if self.stop_event.is_set():
            return 'skip', '已取消', '', [], None

        warns = self._read_warns(logf)
        m = RE_FAIL.search(txt)
        if m:
            code = int(m.group(2))
            detail = (m.group(3) or '').strip()
            reason = DECODER_ERRORS.get(code, '')
            text = '失败(%d) %s' % (code, reason or detail)
            if reason and detail and detail not in reason:
                text += '｜' + detail
            return 'fail', text[:120], '', warns, code

        tail = (txt.strip().splitlines() or [''])[-1]
        return 'fail', '失败(退出码 %s) %s' % (p.returncode, tail[:80]), '', warns, None

    def _claim_output(self, src, outdir):
        """把临时目录里的成品移入音乐库，返回 (最终路径, 是否与已有文件重复)。

        重名时先比内容：完全一致说明这首之前已经解过，直接丢弃新产物并复用旧文件，
        免得音乐库里越堆越多 X (1).mp3、X (2).mp3…；内容不同才退让成 (n) 命名。
        判重放在锁内，防止并发抢名。
        """
        name = os.path.basename(src)
        base, ext = os.path.splitext(name)
        with self._name_lock:
            dest = os.path.join(outdir, name)
            if os.path.exists(dest) and same_file(src, dest):
                return dest, True
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(outdir, '%s (%d)%s' % (base, n, ext))
                n += 1
            os.replace(src, dest)      # 临时目录在 outdir 下，同卷，是原子的
        return dest, False

    def _decode_one(self, decoder, idx, path, outdir):
        """解码单个文件；返回 (status, 状态文本, 输出路径, 警告列表)

        每个任务写到自己的临时子目录，再原子移入音乐库，好处有二：
          1) 并发时不会互相抢同一个输出文件名（解码器内部按"存在即加 (n)"去重，会撞车）
          2) 中途取消留下的半成品只在临时目录里，随删随净，不会污染音乐库
        """
        if self.stop_event.is_set():
            return 'skip', '已取消', '', []

        self.q.put(('row', idx, '解码中', 'run', ''))
        logf = os.path.join(self._tmpdir, 'd%d.log' % idx)
        tmp_out = os.path.join(outdir, '_ncm_tmp_%d_%d' % (idx, random.randint(1000, 99999)))
        try:
            os.makedirs(tmp_out, exist_ok=True)
        except OSError as e:
            return 'fail', '无法创建临时目录: %s' % e, '', []

        try:
            res = ('fail', '未执行', '', [], None)
            for attempt in range(self.RETRY_MAX):
                res = self._run_decoder(decoder, idx, path, tmp_out, logf)
                status, text, outp, warns, code = res
                if status != 'fail' or code not in self.RETRY_CODES:
                    break
                if self.stop_event.is_set():
                    return 'skip', '已取消', '', []
                if attempt < self.RETRY_MAX - 1:
                    backoff = 0.3 * (attempt + 1) + random.random() * 0.3
                    self.q.put(('log', '  · %s 输出名冲突，%.1fs 后重试（%d/%d）'
                                % (os.path.basename(path), backoff, attempt + 1, self.RETRY_MAX - 1)))
                    time.sleep(backoff)

            status, text, outp, warns, code = res
            if status == 'ok':
                if not (outp and os.path.isfile(outp)):
                    # 解码器报成功但路径不符预期，退一步在临时目录里找
                    found = [f for f in os.listdir(tmp_out)
                             if not f.endswith('.log')] if os.path.isdir(tmp_out) else []
                    outp = os.path.join(tmp_out, found[0]) if found else None
                if outp and os.path.isfile(outp):
                    final, dup = self._claim_output(outp, outdir)
                    if dup:
                        return 'dup', '已存在', final, warns
                    return 'ok', '完成', final, warns
                return 'fail', '解码器报成功，但未找到输出文件', '', warns
            return status, text, outp, warns
        finally:
            shutil.rmtree(tmp_out, ignore_errors=True)

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
                if kind == 'row':
                    _, idx, text, tag, outp = msg
                    iid = str(idx)
                    if self.tree.exists(iid):
                        self.tree.set(iid, 'status', text)
                        self.tree.set(iid, 'output', os.path.basename(outp) if outp else '')
                        self.tree.item(iid, tags=(tag,))
                        self.tree.see(iid)
                elif kind == 'progress':
                    _, done, total = msg
                    self.bar['value'] = (done / total * 100) if total else 0
                    self.lbl_prog.configure(text='%d / %d' % (done, total))
                elif kind == 'finished':
                    self.set_busy(False)
                    self.lbl_stat.configure(text='已完成')
                    if self._tmpdir:
                        shutil.rmtree(self._tmpdir, ignore_errors=True)
                        self._tmpdir = None
        except queue.Empty:
            flush()
        # 有积压或仍在跑就快轮询，彻底空闲后放慢，避免每秒无谓唤醒 12 次
        self.after(BUSY_POLL_MS if (got or self.busy) else IDLE_POLL_MS, self._drain)
