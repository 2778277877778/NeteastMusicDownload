# -*- coding: utf-8 -*-
"""
「补完模式」标签页：拿音乐库当线索，把听到一半的专辑/歌手补齐。

两种模式：
  * 专辑补完 —— 收集音乐库里出现过的所有专辑，把每张专辑的曲目全部补齐，
                这样任何一首歌都能顺着专辑整张听
  * 歌手补完 —— 收集音乐库里出现过的所有歌手，把每位歌手的热门 50 首补齐，
                这样任何一位歌手都有可播的曲库

难点在规模：一个几千首的库就能牵出上千张专辑，全量下载是几万首的量级。
所以这里只做「选目标」这一件事 —— 扫描、列清单、搜索过滤、多选，
解析出来的曲目直接并进「在线下载」队列，下载/去重/断点续传全部复用那一页，
不重复实现第二套下载逻辑。
"""

import os
import queue
import threading
import time
import traceback

import tkinter as tk
from tkinter import messagebox, ttk

import gui_theme
from gui_common import (
    BUSY_POLL_MS,
    C_BAD,
    C_INFO,
    C_MUTED,
    C_OK,
    C_WARN,
    DARK_UI,
    IDLE_POLL_MS,
    append_log,
    apply_cookie,
    log_file,
    log_file_many,
)

from ncm.constants import get_album_url, get_artist_url, headers

# 音乐库里算得上「一首歌」的扩展名。这里自己定义一份、不从 gui_common 引，
# 是为了让本页签不依赖「全局去重」那套代码 —— 两个功能各占一个分支，
# 谁先合进去都能单独工作。
AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.ogg', '.oga', '.wav', '.ape', '.wma')


def match_key(text):
    """归一化用于比对的文本：忽略大小写、首尾空白和连续空格。

    「这两个名字算不算同一个」：专辑/歌手名一个来自本地标签、一个来自远端
    接口，大小写和多余空格对不上是常态。
    """
    return ' '.join(str(text or '').casefold().split())


# 列表里最多铺多少行。几千行时 Treeview 的 insert 还是毫秒级的，上万行开始
# 明显卡顿；「全选」等操作仍然作用在**全部匹配项**上，只是不全部画出来。
RENDER_CAP = 2000

SEARCH_URL = 'https://music.163.com/api/cloudsearch/pc'

MODE_ALBUM = 'album'
MODE_ARTIST = 'artist'

# ---------------------------------------------------------------- 勾选图标
# 微软雅黑（本机 UI 字体）里没有 ☑ U+2611 / ☐ U+2610 / ✓ U+2713，实测三者
# 渲染出来的墨迹和 notdef 完全一致，也就是豆腐块。所以不用文字符号，改用
# Pillow 现画 RGBA 图标挂到 Treeview 的行首（实测 alpha 通道会被尊重，
# 透明底在选中行高亮上也不会露出方块）。Pillow 是 gui_login 已在用的可选
# 依赖，真缺了就退回纯文字标记，功能不受影响。
_CHECK_CACHE = {}


def _check_icon(checked, size, dark):
    """画一个复选框图标；同一个 (状态, 尺寸, 主题) 只画一次"""
    key = (bool(checked), int(size), bool(dark))
    hit = _CHECK_CACHE.get(key)
    if hit is not None:
        return hit
    from PIL import Image, ImageDraw, ImageTk
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(2, int(round(size / 8.0)))
    box = [lw // 2, lw // 2, size - 1 - lw // 2, size - 1 - lw // 2]
    if checked:
        d.rectangle(box, fill='#8ab4f8' if dark else '#1a73e8')
        d.line([(size * 0.26, size * 0.52), (size * 0.43, size * 0.71),
                (size * 0.75, size * 0.31)],
               fill='#10161f' if dark else '#ffffff',
               width=max(2, int(round(size / 7.0))))
    else:
        d.rectangle(box, outline='#6b7280' if dark else '#9aa0a6', width=lw)
    photo = ImageTk.PhotoImage(img)
    _CHECK_CACHE[key] = photo
    return photo

STATE_LABEL = {
    'idle': '待解析',
    'run': '解析中…',
    'ok': '就绪',
    'missing': '未找到',
    'mismatch': '疑同名',
}
STATE_TAG = {
    'run': 'run',
    'ok': 'ok',
    'missing': 'fail',
    'mismatch': 'warn',
}


# --------------------------------------------------------------------------- #
# 目标
# --------------------------------------------------------------------------- #
class Target(object):
    """列表里的一行：一个要补完的专辑或歌手。

    key 是稳定的去重/选择标识（由归一化后的名字拼成），界面上的名字单独存，
    这样两首同名但在不同专辑里的曲目不会被合并，显示时又能保留原始大小写。
    """

    __slots__ = ('key', 'name', 'artist', 'have', 'total', 'state', 'note',
                 'remote_id', 'resolved_name')

    def __init__(self, key, name, artist, have):
        self.key = key
        self.name = name
        self.artist = artist
        self.have = have          # 音乐库里已有几首
        self.total = 0            # 解析到远端几首（0 = 还没解析）
        self.state = 'idle'
        self.note = ''
        self.remote_id = None
        self.resolved_name = ''

    def state_text(self):
        base = STATE_LABEL.get(self.state, self.state)
        return '%s %s' % (base, self.note) if self.note else base

    def state_tag(self):
        return STATE_TAG.get(self.state, '')


class Hit(object):
    """一次解析的结果"""

    __slots__ = ('state', 'remote_id', 'name', 'artist', 'size', 'note')

    def __init__(self, state, remote_id=None, name='', artist='', size=0, note=''):
        self.state = state
        self.remote_id = remote_id
        self.name = name
        self.artist = artist
        self.size = size
        self.note = note

    @property
    def ok(self):
        return self.state == 'ok'


# --------------------------------------------------------------------------- #
# 扫库
# --------------------------------------------------------------------------- #
class LibraryCatalog(object):
    """把音乐库读成「专辑清单 + 歌手清单」。

    只读标签，不改文件。专辑按 (专辑名, 专辑歌手) 归并 —— 不同歌手各自出一张
    同名专辑（如《Greatest Hits》）是常事，只按专辑名归并会把它们搅在一起。
    没有 albumartist 标签时退回该曲目的 artist，编译盘因此仍能归到一张专辑。
    """

    def __init__(self, root):
        self.root = root
        self.albums = []
        self.artists = []
        self.read = 0             # 成功读到标签的文件数
        self.unreadable = 0

    @staticmethod
    def _first(audio, key):
        v = audio.get(key) or ['']
        if isinstance(v, list):
            v = v[0] if v else ''
        return str(v or '').strip()

    @staticmethod
    def _all(audio, key):
        v = audio.get(key) or []
        if not isinstance(v, list):
            v = [v] if v else []
        return [str(x).strip() for x in v if str(x or '').strip()]

    def _collect_paths(self, stop=None):
        out = []
        for dirpath, _dirnames, filenames in os.walk(self.root):
            if stop is not None and stop():
                return out
            for fn in filenames:
                if fn.lower().endswith(AUDIO_EXTS):
                    out.append(os.path.join(dirpath, fn))
        return out

    def build(self, progress=None, stop=None):
        from mutagen import File as MutagenFile
        paths = self._collect_paths(stop)
        total = len(paths)
        albums = {}
        artists = {}
        # 无 albumartist 时用「出现最多的 primary artist」当主歌手，供搜索用
        album_prim = {}
        for i, path in enumerate(paths):
            if stop is not None and stop():
                break
            if progress is not None and (i % 50 == 0 or i == total - 1):
                progress(i + 1, total, os.path.basename(path))
            try:
                audio = MutagenFile(path, easy=True)
            except Exception:
                audio = None
            if audio is None or getattr(audio, 'tags', None) is None:
                self.unreadable += 1
                continue
            self.read += 1

            album = self._first(audio, 'album')
            album_artist = self._first(audio, 'albumartist')
            names = self._all(audio, 'artist')
            primary = names[0] if names else ''

            if album and match_key(album) not in ('', 'unknown'):
                # 有 albumartist 标签时它是权威的（用户明确区分过同名专辑），
                # 但库里的文件绝大多数没有这个标签 —— 实测 D:\NewMusicDown 的
                # 883 个文件无一例外。那种情况下若退回 primary artist，同一张
                # 专辑会因为里面一首合唱而被拆成两个目标（实测多出 31 张），
                # 所以退路是「只按专辑名」归并，主歌手取出现最多的那个。
                if album_artist:
                    akey = (match_key(album), match_key(album_artist))
                else:
                    akey = (match_key(album), '')
                e = albums.get(akey)
                if e is None:
                    e = albums[akey] = Target('%s|%s' % akey, album, album_artist, 0)
                e.have += 1
                if not album_artist and primary:
                    cnt = album_prim.setdefault(akey, {})
                    cnt[primary] = cnt.get(primary, 0) + 1

            seen = set()
            for name in names:
                nk = match_key(name)
                if not nk or nk in seen or nk == 'unknown':
                    continue
                seen.add(nk)
                e = artists.get(nk)
                if e is None:
                    e = artists[nk] = Target('artist|' + nk, name, '', 0)
                e.have += 1

        for akey, cnt in album_prim.items():
            albums[akey].artist = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]

        self.albums = sorted(albums.values(), key=lambda t: (t.name.lower(), t.artist.lower()))
        self.artists = sorted(artists.values(), key=lambda t: t.name.lower())
        return len(self.albums), len(self.artists)


# --------------------------------------------------------------------------- #
# 远端解析
# --------------------------------------------------------------------------- #
def _search(session, kw, mode, limit=10):
    """cloudsearch 搜专辑（type=10）或歌手（type=100）"""
    type_ = 10 if mode == MODE_ALBUM else 100
    try:
        j = session.post(SEARCH_URL, data={'s': kw, 'type': type_, 'limit': int(limit),
                                           'offset': 0, 'total': 'true'},
                         timeout=25).json()
    except Exception as e:
        log_file('cloudsearch(%r) 失败: %s' % (kw, e))
        return []
    res = j.get('result') or {}
    return res.get('albums' if mode == MODE_ALBUM else 'artists') or []


def _name_hit(found, want):
    """名字命中强度：2 完全相同 / 1 包含 / 0 不沾边"""
    a, b = match_key(found), match_key(want)
    if not a or not b:
        return 0
    if a == b:
        return 2
    return 1 if (a in b or b in a) else 0


def resolve_album(session, name, artist):
    """把「专辑名 + 歌手」解析成网易云的专辑 id。

    只接受歌名完全相同的候选；歌手对不上但专辑名一致时标成「疑同名」而不是
    直接下 —— 同名不同歌手的两张专辑内容完全不同，猜错的代价是把整张错的专辑
    塞进音乐库。
    """
    kw = ('%s %s' % (name, artist)).strip()
    cands = _search(session, kw, MODE_ALBUM, 10)
    if not cands and artist:
        # 专辑名本身很长（或含 feat. 括号）时，拼上歌手反而搜不到
        cands = _search(session, name, MODE_ALBUM, 10)
    if not cands:
        return Hit('missing', note='· 没搜到')

    best, best_score = None, None
    for c in cands:
        nh = _name_hit(c.get('name'), name)
        if nh < 2:
            continue
        # 合辑/多歌手专辑的专辑级歌手可能只是其中一位，所以候选自己的
        # artist 与 artists[] 都要比一遍，不能只比第一个
        names = [((c.get('artist') or {}).get('name') or '')]
        names += [((x or {}).get('name') or '') for x in (c.get('artists') or [])]
        ah = max([_name_hit(x, artist) for x in names if x] or [0]) if artist else 0
        score = (nh, ah, int(c.get('size') or 0))
        if best_score is None or score > best_score:
            best, best_score = c, score
    if best is None:
        return Hit('missing', note='· 没有同名专辑')

    rid = best.get('id')
    rname = best.get('name') or name
    rartist = ((best.get('artist') or {}).get('name') or '')
    size = int(best.get('size') or 0)
    if artist and best_score[1] == 0:
        return Hit('mismatch', rid, rname, rartist, size,
                   note='· 实为 %s' % (rartist or '未知歌手'))
    return Hit('ok', rid, rname, rartist, size)


def resolve_artist(session, name):
    """把歌手名解析成网易云的歌手 id（同名时取作品最多的那个）"""
    cands = _search(session, name, MODE_ARTIST, 10)
    if not cands:
        return Hit('missing', note='· 没搜到')

    best, best_score = None, None
    for c in cands:
        nh = _name_hit(c.get('name'), name)
        if nh < 2:
            # 别名（alias）也算命中，否则 "M83" 这类中英混排的会漏
            for alias in (c.get('alias') or []):
                if _name_hit(alias, name) == 2:
                    nh = 2
                    break
        if nh < 2:
            continue
        score = (nh, int(c.get('musicSize') or 0), int(c.get('albumSize') or 0))
        if best_score is None or score > best_score:
            best, best_score = c, score
    if best is None:
        return Hit('missing', note='· 没有同名歌手')
    return Hit('ok', best.get('id'), best.get('name') or name, '',
               int(best.get('musicSize') or 0))


def fetch_album_songs(session, album_id):
    """整张专辑的曲目。返回 (曲目列表, 专辑名, 歌手名)"""
    j = session.get(get_album_url(album_id), timeout=25).json()
    alb = j.get('album') or {}
    songs = alb.get('songs') or []
    # 优先用专辑自带的歌手字段。拿首曲的 artists[0] 当专辑歌手会错 ——
    # 合辑里第一首如果是合唱，名字会变成那位客串歌手。
    artist = ((alb.get('artist') or {}).get('name') or '')
    if not artist:
        for x in (alb.get('artists') or []):
            artist = ((x or {}).get('name') or '')
            if artist:
                break
    if not artist and songs:
        names = songs[0].get('artists') or []
        artist = names[0].get('name') if names else ''
    return songs, (alb.get('name') or ''), artist


def fetch_hot_songs(session, artist_id):
    """歌手热门 50 首。返回 (曲目列表, 歌手名)"""
    j = session.get(get_artist_url(artist_id), timeout=25).json()
    songs = j.get('hotSongs') or []
    # 同理：hot50 首曲常是合唱（实测 AJR 的第一首是 Daisy the Greet 的联名），
    # 歌手名必须从 artist 字段取，不能从曲目推。
    artist = ((j.get('artist') or {}).get('name') or '')
    if not artist and songs:
        names = songs[0].get('artists') or []
        artist = names[0].get('name') if names else ''
    return songs, artist


# --------------------------------------------------------------------------- #
# 标签页
# --------------------------------------------------------------------------- #
class CompleteTab(ttk.Frame):

    COLUMNS = (('name', '名称', 320), ('artist', '歌手', 200),
               ('have', '库内', 64), ('total', '曲目', 64), ('state', '状态', 210))

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.settings = app.settings
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False

        self.targets = []          # 当前模式下的全部目标
        self._catalog = None       # 最近一次扫描的结果（切模式时复用，不必重读标签）
        self._sel = set()          # 勾选的 key（跨过滤、跨排序保持）
        self._anchor = None        # Shift 连选的锚点
        self._visible = []         # 当前过滤后的目标（未按 RENDER_CAP 截断）
        self._sort = ('name', True)
        self.mode = MODE_ALBUM

        self._build_ui()
        self.after(120, self._drain)
        self.log('补完模式：先「扫描音乐库」把专辑/歌手列出来，选好目标再「解析并加入队列」。')
        self.log('提示：点一行勾选一行，Shift 连点可勾选一段，Ctrl+A 全选，搜索框里输入关键词即时过滤。')

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        self.var_mode = tk.StringVar(value=MODE_ALBUM)
        self.var_filter = tk.StringVar()
        self.var_interval = tk.StringVar(value='0.5')
        self.var_auto_start = tk.BooleanVar(value=False)

        # ---------------- ① 模式与扫描 ----------------
        top = ttk.LabelFrame(self, text=' ① 补完模式 ')
        top.pack(fill='x', **pad)

        r = ttk.Frame(top)
        r.pack(fill='x', padx=10, pady=(8, 2))
        ttk.Radiobutton(r, text='专辑补完', value=MODE_ALBUM, variable=self.var_mode,
                        command=self._on_mode_change).pack(side='left')
        ttk.Label(r, text='把音乐库里出现过的每张专辑补全，任何一首歌都能整张听',
                  foreground=C_MUTED).pack(side='left', padx=(6, 18))
        ttk.Radiobutton(r, text='歌手补完', value=MODE_ARTIST, variable=self.var_mode,
                        command=self._on_mode_change).pack(side='left')
        ttk.Label(r, text='把每位歌手的热门 50 首补齐', foreground=C_MUTED).pack(side='left', padx=6)

        r = ttk.Frame(top)
        r.pack(fill='x', padx=10, pady=(2, 8))
        self.btn_scan = ttk.Button(r, text='扫描音乐库', width=14, command=self.on_scan,
                                   style='Primary.TButton')
        self.btn_scan.pack(side='left')
        self.lbl_scan = ttk.Label(r, text='尚未扫描', foreground=C_MUTED)
        self.lbl_scan.pack(side='left', padx=10)
        ttk.Label(r, text='音乐库目录在「在线下载 → 设置」里改', foreground=C_MUTED).pack(side='right')

        # ---------------- ② 目标列表 ----------------
        mid = ttk.LabelFrame(self, text=' ② 目标列表 ')
        mid.pack(fill='both', expand=True, **pad)

        bar = ttk.Frame(mid)
        bar.pack(fill='x', padx=8, pady=(6, 2))
        ttk.Label(bar, text='搜索:', foreground=C_MUTED).pack(side='left')
        ent = ttk.Entry(bar, textvariable=self.var_filter, width=24)
        ent.pack(side='left', padx=6, fill='x', expand=True)
        ent.bind('<KeyRelease>', lambda e: self._render())
        ent.bind('<Escape>', lambda e: (self.var_filter.set(''), self._render()))
        self.lbl_count = ttk.Label(bar, text='', foreground=C_MUTED)
        self.lbl_count.pack(side='right', padx=(8, 0))

        bar2 = ttk.Frame(mid)
        bar2.pack(fill='x', padx=8, pady=(0, 4))
        ttk.Button(bar2, text='全选', width=8,
                   command=self.on_select_all).pack(side='left')
        ttk.Button(bar2, text='反选', width=8,
                   command=self.on_invert).pack(side='left', padx=4)
        ttk.Button(bar2, text='清空选择', width=10,
                   command=self.on_clear_sel).pack(side='left')
        self.lbl_sel = ttk.Label(bar2, text='已选 0 项', foreground=C_INFO)
        self.lbl_sel.pack(side='left', padx=12)

        wrap = ttk.Frame(mid)
        wrap.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLUMNS],
                                 show='tree headings', height=10, selectmode='none')
        # 第 0 列空出来放复选框图标。选不选中完全由我们自己管：Treeview 原生
        # 的 extended 多选很容易误触（点一下空白整片选择就没了、拖动一下全变），
        # 换成「点哪行勾哪行」。
        self._icon_px = self._pick_icon_size()
        self._icon_ok = True
        self.tree.heading('#0', text='')
        self.tree.column('#0', width=self._icon_px + 20,
                         minwidth=self._icon_px + 20, stretch=False, anchor='center')
        for c, t, w in self.COLUMNS:
            self.tree.heading(c, text=t, command=lambda k=c: self.on_sort(k))
            self.tree.column(c, width=w, anchor='w',
                             stretch=(c in ('name', 'artist', 'state')))
        self.tree.column('have', anchor='e')
        self.tree.column('total', anchor='e')
        vs = ttk.Scrollbar(wrap, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')
        self.tree.tag_configure('ok', foreground=C_OK)
        self.tree.tag_configure('fail', foreground=C_BAD)
        self.tree.tag_configure('warn', foreground=C_WARN)
        self.tree.tag_configure('run', foreground=C_INFO)
        self.tree.bind('<Button-1>', self._on_click)
        self.tree.bind('<space>', self._on_space)
        self.tree.bind('<Control-a>', self._on_ctrl_a)

        # ---------------- ③ 操作 ----------------
        act = ttk.Frame(self)
        act.pack(fill='x', **pad)
        self.btn_apply = ttk.Button(act, text='解析并加入队列', width=17,
                                    command=self.on_apply, style='Primary.TButton')
        self.btn_apply.pack(side='left')
        self.btn_stop = ttk.Button(act, text='停止', width=8,
                                   command=self.on_stop, state='disabled')
        self.btn_stop.pack(side='left', padx=6)
        ttk.Checkbutton(act, text='解析后立即开始下载',
                        variable=self.var_auto_start).pack(side='left', padx=(4, 12))
        ttk.Label(act, text='解析间隔:', foreground=C_MUTED).pack(side='left')
        ttk.Spinbox(act, from_=0, to=30, increment=0.5, width=5,
                    textvariable=self.var_interval).pack(side='left', padx=4)
        ttk.Label(act, text='秒', foreground=C_MUTED).pack(side='left')
        ttk.Button(act, text='去下载页', width=10,
                   command=lambda: self.app.select_tab('download')).pack(side='right')
        self.lbl_stat = ttk.Label(act, text='就绪')
        self.lbl_stat.pack(side='right', padx=8)

        # ---------------- ④ 日志 ----------------
        logf = ttk.LabelFrame(self, text=' ④ 日志 ')
        logf.pack(fill='both', expand=True, **pad)
        self.txt = tk.Text(logf, height=8, wrap='word', state='disabled',
                           background=gui_theme.LOG_BG, foreground=gui_theme.LOG_FG,
                           insertbackground=gui_theme.LOG_FG, relief='flat',
                           highlightthickness=1, highlightbackground=gui_theme.BORDER,
                           font=gui_theme.FONT_MONO, spacing1=2, spacing3=2)
        ls = ttk.Scrollbar(logf, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=ls.set)
        self.txt.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        ls.pack(side='right', fill='y', pady=6)

    # ------------------------------------------------------------ 设置往返
    def restore(self):
        mode = self.settings.get('complete_mode', MODE_ALBUM)
        self.var_mode.set(MODE_ARTIST if mode == MODE_ARTIST else MODE_ALBUM)
        self.mode = self.var_mode.get()
        try:
            self.var_interval.set(str(self.settings.get('complete_interval', 0.5)))
        except Exception:
            self.var_interval.set('0.5')

    def collect(self):
        self.settings['complete_mode'] = self.var_mode.get()
        try:
            self.settings['complete_interval'] = max(0.0, float(self.var_interval.get()))
        except Exception:
            self.settings['complete_interval'] = 0.5

    def _read_interval(self):
        try:
            v = float(self.var_interval.get())
        except Exception:
            v = 0.5
        return max(0.0, min(30.0, v))

    # ----------------------------------------------------------------- 日志
    def log(self, msg):
        self.log_many([msg])

    def log_many(self, msgs):
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
        state = 'disabled' if busy else 'normal'
        for b in (self.btn_scan, self.btn_apply):
            try:
                b.configure(state=state)
            except Exception:
                pass
        try:
            self.btn_stop.configure(state='normal' if busy else 'disabled')
        except Exception:
            pass

    def on_stop(self):
        self.stop_event.set()
        self.log('收到停止请求，正在中断…')

    # ------------------------------------------------------------ 模式切换
    def _on_mode_change(self):
        self.mode = self.var_mode.get()
        self.collect()
        self.app.persist()
        if self.targets:
            # 已经扫过一次：直接换清单，不用重新读一遍标签
            self._load_targets(self._catalog, self.mode)
            self.log('已切换到%s模式，共 %d 项'
                     % ('专辑' if self.mode == MODE_ALBUM else '歌手', len(self.targets)))

    # ---------------------------------------------------------------- 扫描
    def on_scan(self):
        if self.busy:
            return
        root = (self.settings.get('download_dir') or '').strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning('音乐库目录无效',
                                   '当前音乐库目录不存在：\n%s\n\n请先在「在线下载 → 设置」里指定。' % root)
            return
        self.collect()
        self.app.persist()
        self.stop_event.clear()
        self.set_busy(True)
        self.lbl_stat.configure(text='扫描中…')
        self.lbl_scan.configure(text='扫描中…', foreground=C_INFO)
        threading.Thread(target=self._scan_worker, args=(root,), daemon=True).start()

    def _scan_worker(self, root):
        t0 = time.time()
        try:
            self.q.put(('log', '扫描音乐库：%s' % root))
            cat = LibraryCatalog(root)
            a, b = cat.build(progress=lambda d, t, n: self.q.put(('scan_progress', d, t)),
                             stop=self.stop_event.is_set)
            self.q.put(('log', '读完 %d 个文件（%d 个读不出标签），'
                               '得到专辑 %d 张、歌手 %d 位，用时 %.1fs'
                        % (cat.read, cat.unreadable, a, b, time.time() - t0)))
            self.q.put(('catalog', cat))
        except Exception as e:
            self.q.put(('log', '× 扫描出错: %s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('scanned',))

    def _load_targets(self, cat, mode):
        self._catalog = cat
        self.targets = list(cat.albums if mode == MODE_ALBUM else cat.artists)
        self._sel.clear()
        self._sort_targets()
        self._render()
        self._update_sel_label()

    # ------------------------------------------------------------ 过滤/排序
    def _filtered(self):
        kw = match_key(self.var_filter.get())
        if not kw:
            return list(self.targets)
        out = []
        for t in self.targets:
            if kw in match_key(t.name) or (t.artist and kw in match_key(t.artist)):
                out.append(t)
        return out

    def _sort_targets(self):
        field, asc = self._sort
        keyfn = {
            'name': lambda t: (t.name.lower(), t.artist.lower()),
            'artist': lambda t: (t.artist.lower(), t.name.lower()),
            'have': lambda t: t.have,
            'total': lambda t: t.total,
            'state': lambda t: (t.state, t.name.lower()),
        }.get(field, lambda t: t.name.lower())
        self.targets.sort(key=keyfn, reverse=not asc)

    def on_sort(self, col):
        field, asc = self._sort
        self._sort = (col, (not asc) if field == col else True)
        self._sort_targets()
        self._render()

    def _heading_text(self, col, text):
        field, asc = self._sort
        if field != col:
            return text
        return '%s %s' % (text, '▲' if asc else '▼')

    # ---------------------------------------------------------------- 渲染
    def _render(self):
        for c, t, _w in self.COLUMNS:
            try:
                self.tree.heading(c, text=self._heading_text(c, t))
            except Exception:
                pass

        self._visible = self._filtered()
        shown = self._visible[:RENDER_CAP]

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for t in shown:
            self.tree.insert('', 'end', iid=t.key,
                             image=self._icon_for(t.key in self._sel),
                             values=(t.name, t.artist, t.have or '',
                                     t.total or '', t.state_text()),
                             tags=(t.state_tag(),))
        self._update_counts()

    def _update_counts(self):
        total = len(self.targets)
        match = len(self._visible)
        shown = min(match, RENDER_CAP)
        bits = ['共 %d 项' % total]
        if match != total:
            bits.append('匹配 %d 项' % match)
        if shown < match:
            bits.append('仅显示前 %d 项，请细化关键词' % shown)
        self.lbl_count.configure(text=' · '.join(bits))

    def _update_sel_label(self):
        n = len(self._sel)
        self.lbl_sel.configure(text='已选 %d 项' % n,
                               foreground=C_INFO if n else C_MUTED)

    def _pick_icon_size(self):
        """按屏幕 DPI 算图标像素尺寸 —— 程序开了 DPI 感知，写死 16 会偏小"""
        try:
            dpi = float(self.winfo_fpixels('1i'))
        except Exception:
            dpi = 96.0
        return max(14, int(round(16.0 * dpi / 96.0)))

    def _icon_for(self, checked):
        if not self._icon_ok:
            return ''
        try:
            return _check_icon(checked, self._icon_px, DARK_UI)
        except Exception:
            self._icon_ok = False       # Pillow 缺失或 Tk 图片出错：退回无图标
            return ''

    def _paint_row(self, key):
        """只改一行的勾选图标，不重建整张表"""
        if self.tree.exists(key):
            self.tree.item(key, image=self._icon_for(key in self._sel))

    def _set_rows(self, keys, want):
        for k in keys:
            if want:
                self._sel.add(k)
            else:
                self._sel.discard(k)
            self._paint_row(k)

    def _on_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region not in ('tree', 'cell'):
            return                  # 表头交给排序，分隔线不管
        row = self.tree.identify_row(event.y)
        if not row:
            return
        vis = list(self.tree.get_children())
        want = row not in self._sel         # 以点中这一行为准
        if (event.state & 0x0001) and self._anchor in vis:
            i0, i1 = vis.index(self._anchor), vis.index(row)
            if i0 > i1:
                i0, i1 = i1, i0
            self._set_rows(vis[i0:i1 + 1], want)
        else:
            self._set_rows([row], want)
        self._anchor = row
        try:
            self.tree.focus(row)        # 让空格键作用于刚点的那行
        except Exception:
            pass
        self._update_sel_label()
        return 'break'

    def _on_space(self, _event=None):
        row = self.tree.focus()
        if row:
            self._set_rows([row], row not in self._sel)
            self._update_sel_label()
        return 'break'

    def _on_ctrl_a(self, _event=None):
        self.on_select_all()
        return 'break'

    def on_select_all(self):
        # 作用在**全部匹配项**上，不只是画出来的那 2000 行 —— 否则页面显示
        # 「匹配 8000 项」却只能全选 2000 项，很难理解
        if not self._visible:
            return
        self._sel |= {t.key for t in self._visible}
        self._render()
        self._update_sel_label()
        self.log('已选中当前过滤条件下的 %d 项' % len(self._sel))

    def on_invert(self):
        if not self._visible:
            return
        for t in self._visible:
            if t.key in self._sel:
                self._sel.discard(t.key)
            else:
                self._sel.add(t.key)
        self._render()
        self._update_sel_label()
        self.log('反选完成，当前选中 %d 项' % len(self._sel))

    def on_clear_sel(self):
        self._sel.clear()
        self._render()
        self._update_sel_label()

    def _selected_targets(self):
        if not self._sel:
            return []
        return [t for t in self.targets if t.key in self._sel]

    # ---------------------------------------------------------------- 解析
    def on_apply(self):
        if self.busy:
            return
        if not self.targets:
            messagebox.showwarning('列表为空', '请先「扫描音乐库」。')
            return
        picked = self._selected_targets()
        if not picked:
            messagebox.showwarning('没有选中目标',
                                   '请在列表里选中要补完的专辑/歌手（Shift 连选、Ctrl 点选、Ctrl+A 全选）。')
            return
        dl = self.app.download_tab
        if dl.busy:
            messagebox.showwarning('正在下载',
                                   '「在线下载」页有任务在跑，等它结束或点停止后再来。')
            return

        self.collect()
        self.app.persist()
        mode = self.var_mode.get()
        interval = self._read_interval()
        auto = bool(self.var_auto_start.get())
        cookie = (self.settings.get('cookie') or '').strip()

        est = sum(t.total or t.have for t in picked)
        self.log('开始解析 %d 个目标（约 %d 首，间隔 %.1fs）…' % (len(picked), est, interval))
        self.stop_event.clear()
        self.set_busy(True)
        self.lbl_stat.configure(text='解析中…')
        threading.Thread(target=self._apply_worker,
                         args=(picked, mode, interval, cookie, auto), daemon=True).start()

    def _apply_worker(self, picked, mode, interval, cookie, auto):
        try:
            apply_cookie(cookie)
            import requests
            session = requests.Session()
            session.headers.update(headers)

            tracks, have_ids = [], set()
            ok = miss = 0
            for i, t in enumerate(picked):
                if self.stop_event.is_set():
                    self.q.put(('log', '已停止解析。'))
                    break
                if i and interval > 0:
                    if self.stop_event.wait(interval):
                        self.q.put(('log', '已停止解析。'))
                        break

                self.q.put(('target_state', t.key, 'run', '', '解析中…'))
                try:
                    if mode == MODE_ALBUM:
                        hit = resolve_album(session, t.name, t.artist)
                    else:
                        hit = resolve_artist(session, t.name)

                    if hit.state == 'mismatch':
                        miss += 1
                        self.q.put(('target_state', t.key, 'mismatch', '', hit.note))
                        self.q.put(('log', '  × [%d/%d] %s — %s'
                                    % (i + 1, len(picked), t.name, hit.note.strip('· '))))
                        continue
                    if not hit.ok:
                        miss += 1
                        self.q.put(('target_state', t.key, 'missing', '', hit.note))
                        self.q.put(('log', '  × [%d/%d] %s — %s'
                                    % (i + 1, len(picked), t.name, hit.note.strip('· '))))
                        continue

                    if mode == MODE_ALBUM:
                        songs, rname, rartist = fetch_album_songs(session, hit.remote_id)
                        coll = '%s - album' % (rname or hit.name or t.name)
                        label = '%s — %s' % (rname or t.name, rartist or '')
                    else:
                        songs, rartist = fetch_hot_songs(session, hit.remote_id)
                        coll = '%s - hot50' % (rartist or hit.name or t.name)
                        label = rartist or t.name

                    if not songs:
                        miss += 1
                        self.q.put(('target_state', t.key, 'missing', 0, '· 远端无曲目'))
                        self.q.put(('log', '  × [%d/%d] %s — 远端无曲目'
                                    % (i + 1, len(picked), t.name)))
                        continue

                    new = 0
                    for s in songs:
                        sid = s.get('id')
                        if sid is None or sid in have_ids:
                            continue
                        have_ids.add(sid)
                        new += 1
                        tracks.append({'song': s, 'is_program': False,
                                       'collection': coll, 'source': coll,
                                       'failed': False})
                    ok += 1
                    self.q.put(('target_state', t.key, 'ok', len(songs), ''))
                    self.q.put(('log', '  √ [%d/%d] %s → %d 首（新 %d）'
                                % (i + 1, len(picked), label.strip(' —'), len(songs), new)))
                except Exception as e:
                    miss += 1
                    self.q.put(('target_state', t.key, 'missing', '', '· 出错'))
                    self.q.put(('log', '  × [%d/%d] %s — 解析出错：%s'
                                % (i + 1, len(picked), t.name, e)))
                    self.q.put(('log', traceback.format_exc()))

            self.q.put(('log', '—— 解析结束：成功 %d，未解析 %d，累计曲目 %d 首 ——'
                        % (ok, miss, len(tracks))))
            if tracks or self.stop_event.is_set():
                self.q.put(('resolved', tracks, auto))
        except Exception as e:
            self.q.put(('log', '× 解析线程异常: %s' % e))
            self.q.put(('log', traceback.format_exc()))
        finally:
            self.q.put(('applied',))

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
                flush()
                if kind == 'scan_progress':
                    _, done, total = msg
                    self.lbl_scan.configure(text='已读 %d / %d 个文件' % (done, total),
                                            foreground=C_INFO)
                    self.lbl_stat.configure(text='扫描中… %d/%d' % (done, total))
                elif kind == 'catalog':
                    cat = msg[1]
                    self._load_targets(cat, self.var_mode.get())
                    self.lbl_scan.configure(
                        text='音乐库：%d 个文件 · 专辑 %d 张 · 歌手 %d 位'
                             % (cat.read, len(cat.albums), len(cat.artists)),
                        foreground=C_MUTED)
                    self.lbl_stat.configure(text='扫描完成')
                elif kind == 'scanned':
                    self.set_busy(False)
                elif kind == 'target_state':
                    _, key, state, total, note = msg
                    t = self._by_key(key)
                    if t is not None:
                        t.state = state
                        if total:
                            t.total = total
                        t.note = note or ''
                        if self.tree.exists(key):
                            self.tree.item(key, values=(t.name, t.artist,
                                                        t.have or '', t.total or '',
                                                        t.state_text()),
                                           tags=(t.state_tag(),))
                elif kind == 'resolved':
                    _, tracks, auto = msg
                    self._hand_off(tracks, auto)
                elif kind == 'applied':
                    self.set_busy(False)
                    self.lbl_stat.configure(text='解析完成')
        except queue.Empty:
            flush()
        self.after(BUSY_POLL_MS if (got or self.busy) else IDLE_POLL_MS, self._drain)

    def _by_key(self, key):
        for t in self.targets:
            if t.key == key:
                return t
        return None

    def _hand_off(self, tracks, auto):
        """把解析结果并入下载队列（主线程执行 —— 会碰 Tk）"""
        if not tracks:
            self.log('没有解析到曲目，没有并入队列。')
            return
        added = self.app.download_tab.enqueue_tracks(tracks)
        self.app.select_tab('download')
        if not added:
            self.log('这些曲目都已在下载队列里，没有新增。')
            return
        self.log('已并入下载队列 %d 首，已切到「在线下载」页。' % added)
        if auto:
            self.log('按设置立即开始下载。')
            self.app.download_tab.on_start()
        else:
            self.log('确认无误后，在「在线下载」页点「开始下载」。')
