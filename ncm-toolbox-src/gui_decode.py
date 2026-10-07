# -*- coding: utf-8 -*-
r"""
「NCM 解码」标签页：把本地 .ncm 解密成可播放的 mp3 / flac。

V2.0 起改为**内置纯 Python 解密**（见 ncm_dump.py，算法照搬 taurusxin/ncmdump，MIT），
不再驱动外部 NCMDecoder.exe —— 少一个"包里没附带"的依赖，状态、进度、取消也都能
精确到单个文件。

只解容器，不转码：输出就是封在里面的原始格式（mp3 / flac），
再用下载页同一套代码写封面与标签，命名也沿用下载页的「命名格式」。

流程上沿用之前验证过的几条约束：
  * 每个任务写自己的临时子目录，成功后原子移入音乐库 —— 并发时不会抢同一个输出名，
    中途取消也不会把半成品留在音乐库里
  * 移入前和同名文件比内容（大小 + 首尾各 1MB），一致就判「已存在」，不堆 (1)(2)
  * 解码进行中不许改队列：行号是结果回填的依据
"""

import os
import queue
import random
import shutil
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import gui_theme
import ncm_dump
from gui_common import (
    BUSY_POLL_MS,
    C_BAD,
    C_INFO,
    C_MUTED,
    C_OK,
    IDLE_POLL_MS,
    append_log,
    enable_drop,
    human_size,
    log_file_many,
    safe_name,
    same_file,
    split_drop_paths,
    write_tags,
)


class DecodeTab(ttk.Frame):

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.settings = app.settings
        self.files = []            # 绝对路径列表
        self.sizes = {}            # 路径 → 字节数，避免每行都去 stat
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self._tmpdir = None
        self._name_lock = threading.Lock()
        self._file_btns = []
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=8, pady=(8, 2))
        for txt, w, cmd, gap in (('添加文件…', 11, self.add_files, 0),
                                 ('添加文件夹…', 13, self.add_folder, 4),
                                 ('移除选中', 10, self.remove_selected, 4),
                                 ('清空', 7, self.clear, 0)):
            b = ttk.Button(bar, text=txt, width=w, command=cmd)
            b.pack(side='left', padx=gap)
            self._file_btns.append(b)
        self.lbl_drop = ttk.Label(bar, text='（可直接把 .ncm 文件或文件夹拖进下方列表）',
                                  foreground=C_MUTED)
        self.lbl_drop.pack(side='left', padx=12)

        opt = ttk.LabelFrame(self, text=' 解码参数 ')
        opt.pack(fill='x', **pad)

        r1 = ttk.Frame(opt)
        r1.pack(fill='x', padx=8, pady=6)
        ttk.Label(r1, text='并发:').pack(side='left')
        self.var_conc = tk.IntVar(value=4)
        ttk.Spinbox(r1, from_=1, to=8, width=4, textvariable=self.var_conc).pack(side='left', padx=4)
        self.var_cover = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text='写入封面与标签', variable=self.var_cover).pack(side='left', padx=14)
        self.var_lrc = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text='带上同目录的同名 .lrc', variable=self.var_lrc).pack(side='left')
        ttk.Label(r1, text='（输出为容器内的原始格式，不转码）',
                  foreground=C_MUTED).pack(side='left', padx=10)

        r2 = ttk.Frame(opt)
        r2.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(r2, text='输出目录:').pack(side='left')
        self.var_dir = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_dir).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(r2, text='统一到下载目录', width=15,
                   command=self.use_shared_dir).pack(side='left')

        mid = ttk.LabelFrame(self, text=' 任务列表 ')
        mid.pack(fill='both', expand=True, **pad)
        cols = ('idx', 'file', 'size', 'status', 'output')
        self.tree = ttk.Treeview(mid, columns=cols, show='headings', height=12)
        for c, t, w in (('idx', '#', 46), ('file', '文件', 320), ('size', '大小', 90),
                        ('status', '状态', 170), ('output', '输出', 300)):
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

    # ---------------------------------------------------------------- 状态
    def restore(self):
        s = self.settings
        self.var_dir.set(s.get('download_dir', '') or '')
        try:
            self.var_conc.set(int(s.get('decode_concurrency', 4) or 4))
        except Exception:
            self.var_conc.set(4)
        self.var_cover.set(bool(s.get('decode_cover', True)))
        self.var_lrc.set(bool(s.get('decode_lrc', True)))

    def set_dir(self, value):
        if value:
            self.var_dir.set(value)

    def use_shared_dir(self):
        self.var_dir.set(self.settings.get('download_dir', '') or '')

    def _read_concurrency(self):
        """Spinbox 允许任意键入，IntVar.get() 遇到非数字会抛 TclError —— 必须兜住，
        否则点「开始解码」时异常直接冒泡到 Tk，按钮看起来像完全没反应。"""
        try:
            n = int(self.var_conc.get())
        except Exception:
            n = 4
        n = max(1, min(8, n))
        try:
            self.var_conc.set(n)        # 回写纠正后的值，让用户看到实际生效的并发
        except Exception:
            pass
        return n

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

    # ------------------------------------------------------------- 文件管理
    def _add_paths(self, paths):
        """把文件/文件夹加入队列。

        解码进行中一律拒绝：此时 _rebuild_tree 会把所有状态刷回「等待」，
        而行号是结果回填的依据，重建会让状态错位到别的文件上，
        新加进来的文件又不会被正在跑的线程处理。
        """
        if self.busy:
            self.log('· 解码进行中，不能改动队列')
            return
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for root, _d, fs in os.walk(p):
                    for fn in fs:
                        if fn.lower().endswith('.ncm'):
                            fp = os.path.normpath(os.path.join(root, fn))
                            if fp not in self.files:
                                self.files.append(fp)
                                added += 1
            elif p.lower().endswith('.ncm') and os.path.isfile(p):
                if p not in self.files:
                    self.files.append(p)
                    added += 1
        if added:
            self._rebuild_tree()
            self.log('√ 加入 %d 个 .ncm，队列共 %d 个' % (added, len(self.files)))
        elif paths:
            self.log('· 没有新增（已在本页队列里，或不含 .ncm）')

    def _remember_size(self, path):
        try:
            self.sizes[path] = os.path.getsize(path)
        except OSError:
            self.sizes[path] = 0

    def add_files(self):
        paths = filedialog.askopenfilenames(title='选择 .ncm 文件',
                                            filetypes=[('NCM 文件', '*.ncm'), ('全部文件', '*.*')])
        if paths:
            self._add_paths(list(paths))

    def add_folder(self):
        d = filedialog.askdirectory(title='选择包含 .ncm 的文件夹')
        if d:
            self._add_paths([d])

    def _on_drop(self, event):
        self._add_paths(split_drop_paths(self, event.data))

    def remove_selected(self):
        if self.busy:
            return
        sel = self.tree.selection()
        if not sel:
            return
        idxs = {int(i) for i in sel if str(i).isdigit()}
        self.files = [p for i, p in enumerate(self.files) if i not in idxs]
        self._rebuild_tree()

    def clear(self):
        if self.busy:
            return
        self.files = []
        self.sizes = {}
        self._rebuild_tree()

    def _rebuild_tree(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for i, p in enumerate(self.files):
            self._remember_size(p)
            self.tree.insert('', 'end', iid=str(i), values=(
                i + 1, os.path.basename(p), human_size(self.sizes.get(p, 0)), '等待', ''))
        self.bar['value'] = 0
        self.lbl_prog.configure(text='0 / %d' % len(self.files))

    def open_dir(self):
        d = self.var_dir.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showwarning('目录不存在', '输出目录还不存在：\n%s' % d)
            return
        try:
            os.startfile(d)
        except Exception as e:
            self.log('× 打不开目录: %s' % e)

    # ----------------------------------------------------------------- 执行
    def on_start(self):
        if self.busy:
            return
        if not self.files:
            messagebox.showwarning('队列为空', '请先添加 .ncm 文件或文件夹。')
            return
        outdir = self.var_dir.get().strip()
        if not outdir:
            messagebox.showwarning('缺少输出目录', '请先指定输出目录。')
            return

        self.settings.update({
            'decode_concurrency': self._read_concurrency(),
            'decode_cover': bool(self.var_cover.get()),
            'decode_lrc': bool(self.var_lrc.get()),
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
        threading.Thread(target=self._worker, args=(outdir,), daemon=True).start()

    def on_stop(self):
        self.stop_event.set()
        self.log('收到停止请求，正在收尾…（当前文件解完就停）')

    def _worker(self, outdir):
        files = list(self.files)
        total = len(files)
        conc = max(1, min(8, int(self.settings.get('decode_concurrency', 4) or 4)))
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

        self.q.put(('log', '开始解码 %d 个文件，并发 %d，输出 %s' % (total, conc, outdir)))
        try:
            with ThreadPoolExecutor(max_workers=conc) as ex:
                futs = {ex.submit(self._decode_one, i, p, outdir): i
                        for i, p in enumerate(files)}
                for fut in as_completed(futs):
                    idx = futs[fut]
                    try:
                        status, text, outp = fut.result()
                    except Exception as e:
                        status, text, outp = 'fail', '异常: %s' % e, ''
                        self.q.put(('log', traceback.format_exc()))
                    tag = {'ok': 'ok', 'fail': 'fail',
                           'skip': 'skip', 'dup': 'skip'}.get(status, 'fail')
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
                    done += 1
                    self.q.put(('progress', done, total))
        except Exception as e:
            self.q.put(('log', '× 解码线程异常: %s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('log', '—— 结束：成功 %d，已存在 %d，失败 %d，取消 %d，共 %d ——'
                        % (ok, dup, fail, cancelled, total)))
            self.q.put(('finished',))

    # ------------------------------------------------------------ 单个文件
    def _target_name(self, meta, src, fmt):
        """命名沿用下载页的「命名格式」，让解出来的文件和下载的文件在库里保持一致"""
        title = safe_name(meta.get('musicName')
                          or os.path.splitext(os.path.basename(src))[0])
        artist = safe_name(ncm_dump.artists(meta) or '未知歌手')
        try:
            nt = int(self.settings.get('name_type', 1) or 1)
        except Exception:
            nt = 1
        if nt == 2:
            return '%s - %s.%s' % (artist, title, fmt)
        if nt == 3:
            return '%s - %s.%s' % (title, artist, fmt)
        return '%s.%s' % (title, fmt)

    def _apply_tags(self, path, meta, image, lyrics):
        """写封面 + 标签。write_tags 收的是封面文件路径，所以先把图片落成临时文件。"""
        cover_path = None
        try:
            if image:
                ext = '.png' if image[:8] == b'\x89PNG\r\n\x1a\n' else '.jpg'
                fd, cover_path = tempfile.mkstemp(suffix=ext,
                                                  dir=self._tmpdir or tempfile.gettempdir())
                with os.fdopen(fd, 'wb') as f:
                    f.write(image)
            return write_tags(path, cover_path,
                              meta.get('musicName') or '',
                              ncm_dump.artists(meta),
                              meta.get('album') or '', '', lyrics)
        except Exception as e:
            return False, '写标签失败: %s' % e
        finally:
            if cover_path:
                try:
                    os.remove(cover_path)
                except OSError:
                    pass

    def _claim_output(self, src, outdir, final_name):
        """把临时目录里的成品按 final_name 移入音乐库，返回 (最终路径, 是否与已有文件重复)。

        重名时先比内容：完全一致说明这首之前已经解过，直接丢弃新产物并复用旧文件，
        免得音乐库里越堆越多 X (1).mp3、X (2).mp3…；内容不同才退让成 (n) 命名。
        判重放在锁内，防止并发抢名。
        """
        base, ext = os.path.splitext(final_name)
        with self._name_lock:
            dest = os.path.join(outdir, final_name)
            if os.path.exists(dest) and same_file(src, dest):
                return dest, True
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(outdir, '%s (%d)%s' % (base, n, ext))
                n += 1
            os.replace(src, dest)      # 临时目录在 outdir 下，同卷，是原子的
        return dest, False

    def _decode_one(self, idx, path, outdir):
        """解码单个文件；返回 (status, 状态文本, 输出路径)。

        每个任务写到自己的临时子目录，再原子移入音乐库，好处有二：
          1) 并发时不会互相抢同一个输出文件名
          2) 中途取消留下的半成品只在临时目录里，随删随净，不会污染音乐库
        """
        if self.stop_event.is_set():
            return 'skip', '已取消', ''

        self.q.put(('row', idx, '解码中', 'run', ''))
        tmp_out = os.path.join(outdir, '_ncm_tmp_%d_%d' % (idx, random.randint(1000, 99999)))
        try:
            os.makedirs(tmp_out, exist_ok=True)
        except OSError as e:
            return 'fail', '无法创建临时目录: %s' % e, ''

        try:
            raw = os.path.join(tmp_out, 'audio.raw')
            try:
                _p, fmt, meta, image = ncm_dump.decrypt(path, raw, stop=self.stop_event)
            except ncm_dump.NcmError as e:
                if '取消' in str(e):
                    return 'skip', '已取消', ''
                return 'fail', str(e)[:120], ''
            except Exception as e:
                return 'fail', '解密失败: %s' % e, ''

            size = os.path.getsize(raw) if os.path.isfile(raw) else 0
            if size < 1024:
                # 和下载页同一个原则：宁可报错，也不把几十字节的东西放进音乐库
                return 'fail', '解出来的音频过小（%d 字节）' % size, ''

            name = self._target_name(meta, path, fmt)
            final = os.path.join(tmp_out, name)
            os.replace(raw, final)

            warns = []
            lyrics = None
            lrc_src = os.path.splitext(path)[0] + '.lrc'
            if self.settings.get('decode_lrc', True) and os.path.isfile(lrc_src):
                try:
                    with open(lrc_src, 'r', encoding='utf-8', errors='replace') as f:
                        lyrics = ncm_dump.normalize_lrc(f.read())
                except OSError as e:
                    warns.append('读歌词失败: %s' % e)
            if self.settings.get('decode_cover', True):
                ok_t, msg = self._apply_tags(final, meta, image, lyrics)
                if not ok_t:
                    warns.append(msg)

            dest, dup = self._claim_output(final, outdir, name)
            for w in warns:
                self.q.put(('log', '  ! %s: %s' % (os.path.basename(path), w)))
            if dup:
                return 'dup', '已存在', dest

            # 落盘的也是归一化后的文本：原始 .lrc 里混着 JSON 行，直接拷过去播放器认不了
            if lyrics and self.settings.get('decode_lrc', True):
                dst_lrc = os.path.splitext(dest)[0] + '.lrc'
                if not os.path.exists(dst_lrc):
                    try:
                        with open(dst_lrc, 'w', encoding='utf-8') as f:
                            f.write(lyrics)
                    except OSError:
                        pass
            return 'ok', '完成', dest
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
        # 有积压或仍在跑就快轮询，彻底空闲后放慢，避免每秒无谓唤醒
        self.after(BUSY_POLL_MS if (got or self.busy) else IDLE_POLL_MS, self._drain)
