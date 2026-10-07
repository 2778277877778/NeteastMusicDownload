# -*- coding: utf-8 -*-
r"""
NCM 容器解密（纯 Python，不依赖任何外部程序）。

算法与常量照搬 taurusxin/ncmdump 的 src/ncmcrypt.cpp（MIT），
那是 anonymous5l/ncmdump 原始 C++ 版的跨平台移植。逐段对应关系：

    isNcmFile()      -> 文件头 8 字节必须是 CTENFDAM
    构造函数 key 段   -> u32 长度 + 数据，逐字节 ^0x64，AES-128-ECB(coreKey)，
                        明文前 17 字节是固定前缀，之后才是音频 key
    buildKeyBox()    -> 用音频 key 洗牌出 256 字节 box
    构造函数 meta 段  -> u32 长度 + 数据，逐字节 ^0x63，丢掉 22 字节 "163 Key Info:"
                        前缀，base64 解码，AES-128-ECB(modifyKey)，再丢掉 "music:"，
                        剩下是 JSON（musicName/artist/album/bitrate/duration/format…）
    构造函数封面段    -> skip 5(crc32+图片类型) + u32 frame 总长 + u32 图片长度，
                        读图片后跳过 frame 剩余部分
    Dump()           -> 音频段每字节 XOR box[(box[j] + box[(box[j]+j)&0xff])&0xff]，
                        j=(i+1)&0xff。C++ 用的是块内下标，因为块大小 0x8000 是 256
                        的整数倍，块内与全局下标同余；这里显式传 start 保持同一规则。

只做容器解密，不做转码：输出就是封在里面的原始 mp3 / flac。
依赖只用得上的：pycryptodomex(AES) + 标准库(base64/json/struct)。
"""

import base64
import json
import struct

from Cryptodome.Cipher import AES

CORE_KEY = b"hzHRAmso5kInbaxW"          # sCoreKey
MODIFY_KEY = rb"#14ljk_!\]&0U<'("       # sModifyKey（raw：\] 两个字符正好对上 5C 5D）
MAGIC = b"CTENFDAM"
KEY_PREFIX_LEN = 17                     # mKeyData.c_str() + 17
META_PREFIX_LEN = 22                    # "163 Key(Don't modify):"
MUSIC_PREFIX_LEN = 6                    # "music:"
CHUNK = 0x8000


class NcmError(Exception):
    """不是 NCM 文件 / 文件截断 / 头部损坏"""


def _u32(f):
    data = f.read(4)
    if len(data) != 4:
        raise NcmError("文件截断（读长度字段）")
    return struct.unpack("<I", data)[0]


def _read(f, n, what):
    if n < 0:
        raise NcmError("%s 长度为负" % what)
    data = f.read(n)
    if len(data) != n:
        raise NcmError("%s 数据不完整（要 %d 只读到 %d）" % (what, n, len(data)))
    return data


def _unpad_ecb(blocks):
    """复刻 C++ 的 aesEcbDecrypt：不校验 PKCS7，只看最后一块末字节，>16 就当 0。"""
    out = bytearray()
    last = len(blocks) - 1
    for i, blk in enumerate(blocks):
        if i == last:
            pad = blk[15]
            if pad > 16:
                pad = 0
            out += blk[:16 - pad]
        else:
            out += blk
    return bytes(out)


def _aes_ecb_decrypt(key, data):
    if not data or len(data) % 16:
        raise NcmError("AES-ECB 输入长度不是 16 的整数倍：%d" % len(data))
    cipher = AES.new(key, AES.MODE_ECB)
    blocks = [cipher.decrypt(data[i:i + 16]) for i in range(0, len(data), 16)]
    return _unpad_ecb(blocks)


def build_key_box(key):
    """照搬 buildKeyBox()：256 项洗牌，key 下标循环取用"""
    key_len = len(key)
    if not key_len:
        raise NcmError("音频 key 为空")
    box = bytearray(range(256))
    last = 0
    off = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xFF
        off += 1
        if off >= key_len:
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return box


def keystream_xor(box, buf, start=0):
    """音频段加解密（XOR 对称，所以加密和解密共用）"""
    out = bytearray(buf)
    for i in range(len(out)):
        j = (i + 1 + start) & 0xFF
        out[i] ^= box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]
    return out


def sniff_format(head):
    if head[:3] == b"ID3":
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"                               # 没有 ID3 头的裸 MPEG 帧
    return ""


def parse(path):
    """解析头部，返回 (meta dict, 封面 bytes, 音频起始偏移, key_box)"""
    with open(path, "rb") as f:
        if _read(f, 8, "文件头") != MAGIC:
            raise NcmError("不是网易云 NCM 文件（文件头不匹配）")
        f.seek(2, 1)                                  # 版本字段

        key_len = _u32(f)
        key_raw = bytearray(_read(f, key_len, "key"))
        for i in range(len(key_raw)):
            key_raw[i] ^= 0x64
        plain = _aes_ecb_decrypt(CORE_KEY, bytes(key_raw))
        box = build_key_box(plain[KEY_PREFIX_LEN:])

        meta = {}
        meta_len = _u32(f)
        if meta_len:
            mod = bytearray(_read(f, meta_len, "meta"))
            for i in range(len(mod)):
                mod[i] ^= 0x63
            dec = _aes_ecb_decrypt(MODIFY_KEY, base64.b64decode(bytes(mod[META_PREFIX_LEN:])))
            try:
                meta = json.loads(dec[MUSIC_PREFIX_LEN:].decode("utf-8"))
            except ValueError as e:
                raise NcmError("meta JSON 解析失败：%s" % e)

        f.seek(5, 1)                                  # crc32(4) + 图片类型(1)
        frame_len = _u32(f)
        img_len = _u32(f)
        image = _read(f, img_len, "封面") if img_len else b""
        if frame_len < img_len:
            raise NcmError("封面 frame 长度异常：frame=%d 图片=%d" % (frame_len, img_len))
        f.seek(frame_len - img_len, 1)                # frame 的剩余部分
        offset = f.tell()

    return meta, image, offset, box


def decrypt(path, out_path, progress=None, stop=None):
    """解密写出裸音频文件。

    返回 (输出路径, 实际格式, meta, 封面 bytes) —— 一次解析就够，调用方还要用
    meta 和封面去写标签，别让它在外面再 parse 一遍。
    """
    meta, image, offset, box = parse(path)
    written = 0
    head = b""
    with open(path, "rb") as fin, open(out_path, "wb") as fout:
        fin.seek(offset)
        while True:
            if stop is not None and stop.is_set():
                raise NcmError("已取消")
            buf = fin.read(CHUNK)
            if not buf:
                break
            out = keystream_xor(box, buf, written)
            if not head:
                head = out[:4]
            fout.write(out)
            written += len(buf)
            if progress:
                progress(written)

    fmt = sniff_format(head) or (meta.get("format") or "").lower().strip() or "mp3"
    return out_path, fmt, meta, image


def artists(meta, sep="/"):
    """NCM 的 artist 字段是 [[名字, id], ...]，拼成 'A/B'"""
    out = []
    for a in (meta.get("artist") or []):
        if isinstance(a, (list, tuple)) and a:
            out.append(str(a[0]))
        elif a:
            out.append(str(a))
    return sep.join(out)


def normalize_lrc(raw):
    """把网易云客户端写在 .ncm 旁边的 .lrc 归一成真正的 LRC。

    那个文件名叫 .lrc，其实是混合格式：绝大多数行是标准 `[mm:ss.SSS]文本`，
    但「作词 / 作曲 / 编曲」那几行是一整行 JSON —— `{"t":152,"c":[{"tx":"作曲: "},{"tx":"Ayase"}]}`，
    `t` 是十分之一秒，`c[].tx` 按顺序拼起来就是这一句文字。
    原样塞进 USLT 会让播放器显示一堆 JSON，所以这里把 JSON 行换成等价的时间戳行，
    其余行原样保留。
    """
    out = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s.startswith("{"):
            out.append(line)
            continue
        try:
            d = json.loads(s)
            segs = d.get("c") or []
            text = "".join(str(x.get("tx", "")) for x in segs if isinstance(x, dict)).strip()
            t = float(d.get("t") or 0) / 10.0            # 分秒 → 秒
        except ValueError:
            out.append(line)                             # 不是我们认识的 JSON，原样留着
            continue
        if not text:
            continue
        m, sec = int(t // 60), t % 60
        out.append("[%02d:%06.3f]%s" % (m, sec, text))
    return "\n".join(out) + ("\n" if out else "")
