"""生成最小但合法的虚构媒体与封面文件（无需任何第三方库）。"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def write_wav(path: Path, seconds: float = 0.5) -> int:
    """生成 8kHz 单声道静音 WAV（虚构占位音频，采集器不播放）。"""
    sample_rate = 8000
    frames = int(sample_rate * seconds)
    data = b"\x80" * frames  # 8-bit 静音
    riff = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<HHIIHH", 1, 1, sample_rate,
                                sample_rate, 1, 8)
    data_chunk = b"data" + struct.pack("<I", len(data)) + data
    content = riff + fmt + data_chunk
    path.write_bytes(content)
    return len(content)


def write_mp3_stub(path: Path) -> int:
    """生成带 MPEG 帧头的最小 MP3 占位文件（仅用于可用性与类型验证）。"""
    # MPEG1 Layer III 帧头 0xFFFB9004 + 填充字节，不构成可听内容
    frame_header = b"\xff\xfb\x90\x04"
    content = frame_header + b"\x00" * 412
    path.write_bytes(content)
    return len(content)


def write_png(path: Path, size: int = 32, rgb: tuple[int, int, int] = (47, 84, 150)) -> int:
    """生成纯色 PNG（虚构封面）。"""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b""
    row = bytes(rgb) * size
    for _ in range(size):
        raw += b"\x00" + row
    idat = zlib.compress(raw, 9)
    content = header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    path.write_bytes(content)
    return len(content)
