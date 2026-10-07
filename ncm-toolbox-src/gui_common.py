# -*- coding: utf-8 -*-
"""
公共模块：设置持久化、链接解析、元数据处理、标签写入、解码器发现。
被 gui_download.py / gui_decode.py / ncm_gui.py 共用。
"""

import ctypes
import json
import os
import re

from ncm.constants import headers

# 上游 ncm/constants.py 在 headers 里硬编码了 'Host': 'music.163.com'。
# 对 music.163.com 的 API 调用这没问题（requests 本来就会按 URL 自动填 Host），
# 但音频/封面地址在 m801.music.126.net 这类 CDN 上 —— 带着错误的 Host 去请求，
# CDN 会返回 gzip 过的 {"code":404,"message":"接口未找到！"}，正好 43 字节。
# 上游 CLI 用的是裸 requests.get（不带 session 头）所以没暴露；GUI 复用 session
# 下载就会踩上，表现为"下载成功但文件只有几十字节"。
# 直接去掉，让 requests 自己按目标 URL 填正确的 Host。
headers.pop('Host', None)

APP_TITLE = '网易云音乐工具箱'

# 主线程消费后台队列的轮询间隔：有任务时快，空闲时放慢，
# 否则窗口开着什么都不干也在每秒唤醒。
BUSY_POLL_MS = 60
IDLE_POLL_MS = 400


def _resolve_app_dir():
    """配置目录。可用 NCM_GUI_HOME 覆盖 —— 自动化测试必须指向临时目录，
    否则会读到用户的真实 gui.json，甚至把登录态覆盖掉。"""
    override = os.environ.get('NCM_GUI_HOME')
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.expanduser('~'), '.ncm')


APP_DIR = _resolve_app_dir()
GUI_PREFS = os.path.join(APP_DIR, 'gui.json')
LOG_FILE = os.path.join(APP_DIR, 'gui.log')
DEFAULT_DIR = os.path.join(APP_DIR, 'download')

# 保存一份原始匿名 Cookie，用户清空输入时回退
_ORIG_COOKIE = headers.get('Cookie', '')


# --------------------------------------------------------------------------- #
# 语义色
#
# 原来这些颜色是写死的浅色主题值（#5f6368 等）。在开了深色模式的 Win11 上，
# 系统会把本进程看到的控件底色反成近黑（实测 GetSysColor(COLOR_BTNFACE)=(0,0,0)），
# 那套灰字对比度只有 3.5:1，状态栏和提示行几乎看不见 —— 实测截图里状态栏那条带子
# 一个文字像素都没有。所以按实际底色分两套给值，浅色主题沿用原值不变。
# --------------------------------------------------------------------------- #
def _sys_rgb(idx):
    try:
        v = ctypes.windll.user32.GetSysColor(idx) & 0xFFFFFF
        return (v & 255, (v >> 8) & 255, (v >> 16) & 255)
    except Exception:
        return (240, 240, 240)          # 判不出来就按浅色处理

def _rel_lum(c):
    def ch(x):
        x = x / 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


DARK_UI = _rel_lum(_sys_rgb(15)) < 0.5          # 15 = COLOR_BTNFACE

C_MUTED = '#a4aab3' if DARK_UI else '#5f6368'   # 次要说明文字
C_INFO = '#8ab4f8' if DARK_UI else '#1a73e8'    # 进行中 / 提示
C_OK = '#81c995' if DARK_UI else '#137333'      # 成功
C_WARN = '#f0a35e' if DARK_UI else '#b06000'    # 需要留意
C_BAD = '#f28b82' if DARK_UI else '#c5221f'     # 失败


def contrast_ratio(c1, c2):
    l1, l2 = _rel_lum(c1), _rel_lum(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _hex_rgb(h):
    h = h.lstrip('#')
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


class StopRequested(Exception):
    pass


# --------------------------------------------------------------------------- #
# 在线下载相关常量
# --------------------------------------------------------------------------- #
QUALITIES = [
    ('标准 128k', 'standard'),
    ('较高 192k', 'higher'),
    ('极高 320k', 'exhigh'),
    ('无损 FLAC', 'lossless'),
    ('Hi-Res 无损', 'hires'),
    ('沉浸环绕声', 'jyeffect'),
    ('高清臻音', 'sky'),
    ('超清母带', 'jymaster'),
    ('杜比全景声', 'dolby'),
]
LEVEL_ORDER = [lv for _, lv in QUALITIES]
FLAC_LEVELS = {'lossless', 'hires', 'jyeffect', 'sky', 'jymaster'}

PLAY_FAIL_REASON = {
    -110: '需要登录：这是付费/VIP 曲目，请在 Cookie 框填入你自己账号的登录 Cookie',
    -200: '该曲无版权或已下架',
    200: '服务器未返回地址（多半无版权或已下架）',
}

KIND_LABEL = {
    'auto': '自动识别',
    'song': '单曲',
    'album': '专辑',
    'playlist': '歌单',
    'artist': '歌手热门',
    'radio': '电台',
    'program': '播客单集',
}



DEFAULT_SETTINGS = {
    # 在线下载
    'download_dir': DEFAULT_DIR,
    'name_type': 1,      # 1: 歌名  2: 歌手 - 歌名  3: 歌名 - 歌手
    'folder_type': 1,    # 1: 不分类  2: 按歌手  3: 按歌手/专辑
    'quality': 'exhigh',
    'cookie': '',
    'skip_existing': True,
    'download_lyrics': True,     # 抓歌词并存成同名 .lrc
    'embed_lyrics': True,        # 同时写进音频标签（MP3=USLT / FLAC=LYRICS）
    'resume_partial': True,      # 断点续传：.part 存在时从断点继续
    'download_interval': 1.0,    # 每首之间的间隔（秒）
                                 # 批量下载时短时间高频请求是风控最敏感的模式，
                                 # 默认留 1 秒缓冲，别把它调成 0 去跑几百首。
    # NCM 解码（内置解密，只解容器不转码）
    'decode_concurrency': 4,
    'decode_cover': True,
    'decode_lrc': True,
}


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
MAX_NAME_LEN = 180


def safe_name(s):
    """替换 Windows 非法字符，并限制长度。

    NTFS 单个文件名上限 255 字符，但 '歌手 - 歌名' 这种拼接加上扩展名、
    再加可能的 ' (n)' 去重后缀很容易超；超了会直接写文件失败。
    这里统一截到 180，留出余量。
    """
    out = re.sub(r'[\\/:*?"<>|\t\r\n]', ' ', str(s)).strip().strip('.')
    if len(out) > MAX_NAME_LEN:
        out = out[:MAX_NAME_LEN].rstrip()
    return out or 'untitled'


def human_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit)
        n /= 1024


def log_file_many(msgs):
    """一批日志一次 open。逐行开文件在长批量任务里是白扔的 IO。"""
    if isinstance(msgs, str):
        msgs = [msgs]
    msgs = [m for m in msgs if m]
    if not msgs:
        return
    try:
        if not os.path.isdir(APP_DIR):
            os.makedirs(APP_DIR, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(''.join(m + '\n' for m in msgs))
    except Exception:
        pass


def log_file(msg):
    log_file_many([msg])


MAX_LOG_LINES = 1500


def append_log(widget, lines, keep=MAX_LOG_LINES):
    """往日志控件追加若干行；超过上限就丢弃前半段。

    接受 str 或 list。批量很重要：逐行调用时 `see('end')` 每行触发一次滚动重绘，
    实测 3000 行里 93% 的耗时（1.16s / 1.25s）都花在这一下，一次插入一次滚动就没了。
    裁剪也保留 —— 之前只 insert 不裁剪，几千行后 Text 会明显拖慢界面。
    """
    if isinstance(lines, str):
        lines = [lines]
    lines = [l for l in lines if l]
    if not lines:
        return
    widget.configure(state='normal')
    widget.insert('end', ''.join(l + '\n' for l in lines))
    try:
        total = int(widget.index('end-1c').split('.')[0])
        if total > keep:
            widget.delete('1.0', '%d.0' % (total - keep))
    except Exception:
        pass
    widget.see('end')
    widget.configure(state='disabled')


def same_file(a, b):
    """判断两个文件是否内容一致（先比大小，再比首尾各 1MB）。

    用于"重复解码同一个文件"时跳过，避免音乐库里堆出 X (1).mp3、(2)… 
    整文件哈希对 40MB 的无损文件太慢，首尾各 1MB 已足够区分。
    """
    try:
        sa, sb = os.path.getsize(a), os.path.getsize(b)
        if sa != sb:
            return False
        chunk = 1 << 20
        with open(a, 'rb') as fa, open(b, 'rb') as fb:
            if fa.read(chunk) != fb.read(chunk):
                return False
            if sa > 2 * chunk:
                fa.seek(-chunk, os.SEEK_END)
                fb.seek(-chunk, os.SEEK_END)
                if fa.read(chunk) != fb.read(chunk):
                    return False
        return True
    except OSError:
        return False


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(GUI_PREFS, 'r', encoding='utf-8') as f:
            s.update(json.load(f))
    except Exception:
        pass
    if not s.get('download_dir'):
        s['download_dir'] = DEFAULT_DIR
    return s


def save_settings(s):
    try:
        if not os.path.isdir(APP_DIR):
            os.makedirs(APP_DIR, exist_ok=True)
        with open(GUI_PREFS, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_file('save_settings failed: %s' % e)


def parse_cookie(s):
    """把 cookie 字符串拆成 dict（容忍换行、多余分号、前后空白）"""
    d = {}
    for part in (s or '').replace('\r', '').replace('\n', ';').split(';'):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            k, v = k.strip(), v.strip()
            if k:
                d[k] = v
    return d


def apply_cookie(cookie):
    """把用户自己的登录态合并进请求头，写回 ncm.constants.headers。

    注意是"合并"而不是"替换"：匿名头里的 appver / _ntes_nuid / NMTID 等客户端
    标识一旦丢掉，部分接口会返回空数据或异常码。用户的键覆盖同名匿名键。
    """
    base = parse_cookie(_ORIG_COOKIE)
    c = (cookie or '').strip()
    if c:
        if '=' not in c:
            c = 'MUSIC_U=' + c          # 只填了 MUSIC_U 的值
        base.update(parse_cookie(c))
    # 网页端标识，缺失时部分接口会挑剔
    base.setdefault('os', 'pc')
    base.setdefault('appver', '2.10.6')
    headers['Cookie'] = '; '.join('%s=%s' % (k, v) for k, v in base.items())


def has_login(cookie):
    """判断用户是否真的填了登录态（光填个昵称之类的没用）"""
    return bool(parse_cookie(cookie).get('MUSIC_U'))


def fetch_account(session, timeout=20):
    """查登录状态。返回 profile dict；未登录返回 None。"""
    try:
        j = session.get('http://music.163.com/api/nuser/account/get',
                        timeout=timeout).json()
        return j.get('profile') or None
    except Exception as e:
        log_file('fetch_account failed: %s' % e)
        return None


def fetch_vip_info(session, timeout=20):
    """查会员信息；未登录返回 None"""
    try:
        j = session.get('http://music.163.com/api/music-vip-membership/client/vip/info',
                        timeout=timeout).json()
        if j.get('code') != 200:
            return None
        return j.get('data') or None
    except Exception as e:
        log_file('fetch_vip_info failed: %s' % e)
        return None


# vipType 大致对应关系；未知值原样显示，不做臆测
VIP_TYPE_LABEL = {
    0: '非会员',
    10: '音乐包',
    11: '黑胶 VIP',
    100: '黑胶 SVIP',
}


# --------------------------------------------------------------------------- #
# 链接解析
# --------------------------------------------------------------------------- #
def parse_target(text, force_kind='auto'):
    """从链接或纯 ID 里解析出 (kind, id)"""
    text = (text or '').strip()
    if not text:
        return None, None
    if text.isdigit():
        return ('song' if force_kind == 'auto' else force_kind), text
    if not text.lower().startswith('http'):
        m = re.match(r'^(\w+)\s*[:=]\s*(\d+)$', text)
        if m:
            return m.group(1), m.group(2)
        return None, None

    # 网易云把真实 query 塞在 # 后面，urlparse 要分别看 query 和 fragment
    from urllib.parse import parse_qs, urlparse
    u = urlparse(text)
    q = parse_qs(u.query) if u.query else parse_qs(urlparse(u.fragment).query)
    tid = (q.get('id') or [None])[0]
    if not tid:
        m = re.search(r'[?&#]id=(\d+)', text)
        tid = m.group(1) if m else None
    if not tid:
        return None, None

    kind = force_kind
    if kind == 'auto':
        low = text.lower()
        if 'djradio' in low or '/radio' in low:
            kind = 'radio'
        elif 'program' in low or '/dj' in low:
            kind = 'program'
        elif 'album' in low:
            kind = 'album'
        elif 'artist' in low:
            kind = 'artist'
        elif 'playlist' in low or 'toplist' in low:
            kind = 'playlist'
        elif 'song' in low:
            kind = 'song'
        else:
            kind = 'song'
    return kind, tid


def fetch_song_details(session, ids, chunk=200):
    """批量取歌曲详情，比一首一首请求快得多"""
    out = []
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        url = 'http://music.163.com/api/song/detail/?ids=[%s]' % ','.join(str(x) for x in part)
        try:
            j = session.get(url, timeout=30).json()
            out.extend(j.get('songs') or [])
        except Exception as e:
            log_file('fetch_song_details failed: %s' % e)
    return out


def cover_url_of(song, is_program):
    if is_program:
        return song.get('coverUrl') or \
            ((song.get('mainSong') or {}).get('album') or {}).get('picUrl')
    alb = song.get('album') or {}
    # picUrl 是清晰封面，blurPicUrl 是模糊占位图，优先用前者
    return alb.get('picUrl') or alb.get('blurPicUrl')


def song_meta(song, is_program):
    """返回 (title, artist, album, track)"""
    title = (song.get('name') or 'unknown').strip()
    if is_program:
        dj = song.get('dj') or {}
        artist = dj.get('nickname') or 'unknown'
        album = dj.get('brand') or 'unknown'
        track = ''
    else:
        artists = song.get('artists') or []
        artist = artists[0]['name'] if artists else 'unknown'
        alb = song.get('album') or {}
        album = alb.get('name') or 'unknown'
        no = song.get('no')
        size = alb.get('size')
        track = '%s/%s' % (no, size) if no and size else ''
    return title, artist, album, track


# --------------------------------------------------------------------------- #
# 歌词
# --------------------------------------------------------------------------- #
LYRIC_HOSTS = ('https://interface3.music.163.com/api/song/lyric',
               'https://music.163.com/api/song/lyric')


def fetch_lyric(session, song_id):
    """取歌词，返回 {'lrc','tlyric','klyric','romalrc'}（都可能是空串）。

    网易云把歌词放在 interface3 上，music.163.com 也有同一接口，互为备份。
    """
    empty = {'lrc': '', 'tlyric': '', 'klyric': '', 'romalrc': ''}
    for url in LYRIC_HOSTS:
        try:
            j = session.post(url, data={'id': int(song_id), 'lv': -1, 'kv': -1,
                                        'tv': -1, 'rv': -1}, timeout=20).json()
            if j.get('code') != 200:
                continue
            out = {}
            for key in empty:
                out[key] = ((j.get(key) or {}).get('lyric') or '')
            if out['lrc'] or out['tlyric']:
                return out
            return empty
        except Exception as e:
            log_file('fetch_lyric(%s) 失败: %s' % (url.split('/')[2], e))
    return empty


def build_lrc_text(lrc, tlyric=None):
    """合并原文与翻译。

    网易云的翻译是**独立时间轴**，直接拼接会让播放器只认最后一段，所以按时间戳
    合并成一条：同时间戳的译文接在原文下一行（绝大多数播放器都认这个约定）。
    """
    if not lrc:
        return tlyric or ''
    if not tlyric:
        return lrc

    def parse(text):
        out = {}
        order = []
        for line in text.splitlines():
            m = re.match(r'^((?:\[[^\]]*\])+)(.*)$', line)
            if not m:
                continue
            tags, content = m.group(1), m.group(2).strip()
            if not content:
                continue
            for t in re.findall(r'\[([^\]]*)\]', tags):
                out[t] = content
                if t not in order:
                    order.append(t)
        return out, order

    en, en_order = parse(lrc)
    zh, _ = parse(tlyric)
    if not zh:
        return lrc
    merged = []
    for t in en_order:
        merged.append('[%s]%s' % (t, en[t]))
        if t in zh and zh[t] != en[t]:
            merged.append('[%s]%s' % (t, zh[t]))
    return '\n'.join(merged)


def write_lrc_file(folder, basename, text):
    """把歌词写成与音频同名的 .lrc"""
    if not text:
        return None
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, basename + '.lrc')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return path
    except OSError as e:
        log_file('写歌词文件失败: %s' % e)
        return None


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #
SEARCH_URL = 'https://music.163.com/api/cloudsearch/pc'


def normalize_search_song(so):
    """把搜索结果的字段映射成下载流程惯用的形状。

    cloudsearch 返回的是新版结构（ar/al），而下载/写标签那条链路用的是
    旧版结构（artists/album），这里做一次转换，免得下游到处判断两种格式。
    """
    al = so.get('al') or {}
    return {
        'id': so.get('id'),
        'name': so.get('name') or '',
        'artists': [{'name': (a or {}).get('name') or ''} for a in (so.get('ar') or [])],
        'album': {'name': al.get('name') or '', 'picUrl': al.get('picUrl') or '',
                  'blurPicUrl': al.get('picUrl') or ''},
        'fee': so.get('fee'),
        'duration': so.get('dt') or 0,
        'no': so.get('no') or 1,
        '_from_search': True,
    }


def search_songs(session, keywords, limit=30, offset=0):
    """按关键词搜歌。返回 (歌曲列表, 总数)。"""
    try:
        j = session.post(SEARCH_URL, data={
            's': keywords, 'type': 1, 'limit': int(limit),
            'offset': int(offset), 'total': 'true'}, timeout=25).json()
        res = j.get('result') or {}
        songs = [normalize_search_song(s) for s in (res.get('songs') or [])]
        return songs, int(res.get('songCount') or len(songs))
    except Exception as e:
        log_file('search_songs(%r) 失败: %s' % (keywords, e))
        return [], 0


# --------------------------------------------------------------------------- #
# 标签写入
# --------------------------------------------------------------------------- #
def write_mp3_tags(path, cover_path, title, artist, album, track, lyrics=None):
    from mutagen.mp3 import MP3, HeaderNotFoundError
    from mutagen.id3 import ID3, APIC, TPE1, TIT2, TALB, TRCK, USLT, error
    try:
        audio = MP3(path, ID3=ID3)
    except HeaderNotFoundError:
        return False, '不是有效的 MP3 文件（可能下回来的是 HTML 错误页）'
    except Exception as e:
        return False, str(e)
    if audio.tags is None:
        try:
            audio.add_tags()
            audio.save()
        except error as e:
            return False, '写入 ID3 失败: %s' % e
    id3 = ID3(path)
    if id3.getall('APIC'):
        id3.delall('APIC')
    if cover_path and os.path.exists(cover_path):
        with open(cover_path, 'rb') as f:
            id3.add(APIC(encoding=0, mime='image/jpeg', type=3, data=f.read()))
    id3.add(TPE1(encoding=3, text=artist))
    id3.add(TIT2(encoding=3, text=title))
    id3.add(TALB(encoding=3, text=album))
    if track:
        id3.add(TRCK(encoding=3, text=track))
    # 歌词：ID3 用 USLT（未同步歌词）。播放器普遍认这个帧，
    # 带时间戳的 LRC 直接整段塞进去即可，多数播放器能自行解析。
    for old in id3.getall('USLT'):
        id3.delall('USLT')
        break
    if lyrics:
        id3.add(USLT(encoding=3, lang='chi', desc='', text=lyrics))
    id3.save(v2_version=3)
    return True, ''


def write_flac_tags(path, cover_path, title, artist, album, track, lyrics=None):
    from mutagen.flac import FLAC, Picture
    try:
        audio = FLAC(path)
        audio.delete()
        audio['TITLE'] = title
        audio['ARTIST'] = artist
        audio['ALBUM'] = album
        if track:
            audio['TRACKNUMBER'] = track.split('/')[0]
        # FLAC 用 LYRICS 注释字段（Vorbis comment 约定）
        if lyrics:
            audio['LYRICS'] = lyrics
        if cover_path and os.path.exists(cover_path):
            with open(cover_path, 'rb') as f:
                pic = Picture()
                pic.type = 3
                pic.mime = 'image/jpeg'
                pic.desc = 'Cover'
                pic.data = f.read()
                audio.add_picture(pic)
        audio.save()
        return True, ''
    except Exception as e:
        return False, str(e)


def write_tags(path, cover_path, title, artist, album, track, lyrics=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == '.flac':
        return write_flac_tags(path, cover_path, title, artist, album, track, lyrics)
    if ext == '.mp3':
        return write_mp3_tags(path, cover_path, title, artist, album, track, lyrics)
    return False, '未知格式 %s，跳过标签写入' % ext


# --------------------------------------------------------------------------- #
# 拖放支持（tkinterdnd2 可选，缺失时自动降级为按钮选择）
# --------------------------------------------------------------------------- #
def dnd_available():
    try:
        import tkinterdnd2  # noqa: F401
        return True
    except Exception:
        return False


def make_root():
    """优先用支持拖放的 Tk 根窗口，失败则退回普通 Tk"""
    import tkinter as tk
    try:
        from tkinterdnd2 import TkinterDnD
        return TkinterDnD.Tk(), True
    except Exception:
        return tk.Tk(), False


def enable_drop(widget, callback):
    """给控件注册文件拖放；返回是否成功"""
    try:
        from tkinterdnd2 import DND_FILES
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind('<<Drop>>', callback)
        return True
    except Exception:
        return False


def split_drop_paths(widget, data):
    r"""把拖放事件里的路径串拆成列表。

    不能用 tk.splitlist()：它是 Tcl 列表解析，会把 Windows 路径里的反斜杠
    当转义符吃掉（D:\plain\c.ncm → D:plainc.ncm）。这里按 DND_FILES 的实际
    格式手工解析：含空格的路径用 {} 包裹，其余以空白分隔。
    """
    data = (data or '').strip()
    if not data:
        return []
    out = []
    i, n = 0, len(data)
    while i < n:
        ch = data[i]
        if ch == '{':
            j = data.find('}', i)
            if j == -1:                      # 没有收尾括号，剩下的整体当一项
                out.append(data[i + 1:])
                break
            out.append(data[i + 1:j])
            i = j + 1
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < n and not data[j].isspace():
                j += 1
            out.append(data[i:j])
            i = j
    return [p for p in (x.strip() for x in out) if p]
