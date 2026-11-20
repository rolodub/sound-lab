"""Pyramix media files (.pmf, Merging Technologies), read without Pyramix.

A .pmf is a small compound file ("WOAM Compound File"): named streams stored in blocks, a block
table per stream, and a directory near the end of the file.
  - A_<n>     the audio of track n: little-endian PCM, blocks of whole samples
  - MH_       the media header: sample rate, bits, length in samples
  - TT_       track names;  W_<n>_<level>  waveform overviews;  MD_  a small marker
Directory entry: u32 flags, UTF-16 name (512 bytes), 3 FILETIMEs, u64 size, u64 offset of the
stream's block table. Block table: u32 count, then per block 16 bytes (u32, u32 offset low,
u32 offset high, u32 used length) — the count sits in the first u32 of the first entry.
Layout worked out on Roland's files (2026-11-20); every value is checked before use.
"""
import re
import struct

NAME = re.compile(r"^(A_\d+|MH_|TT_|MD_|W_\d+_\d+)$")
TAIL = 4 << 20                  # the directory sits in the last few KB; read up to 4 MiB
MH_RATE, MH_BITS, MH_FRAMES = 0x490, 0x494, 0x4A8


class PmfError(ValueError):
    pass


def _directory(f, size: int) -> dict[str, tuple[int, int]]:
    """{stream name: (size, table offset)} found in the tail of the file."""
    start = max(0, size - TAIL)
    f.seek(start)
    tail = f.read()
    found = {}
    for m in re.finditer(rb"(?:[A-Z]\x00(?:[A-Z_0-9]\x00){1,8})\x00\x00", tail):
        pos = m.start()
        name = tail[pos:m.end() - 2].decode("utf-16-le")
        if not NAME.match(name) or pos + 0x228 > len(tail):
            continue
        ssize, table = struct.unpack_from("<QQ", tail, pos + 0x218)
        if 0 < ssize < size and 0 < table < size:
            found.setdefault(name, (ssize, table))
    return found


def _blocks(f, table: int, ssize: int, size: int) -> list[tuple[int, int]]:
    f.seek(table)
    count = struct.unpack("<I", f.read(4))[0]
    if not 0 < count < 10_000_000:
        raise PmfError(f"bad block count {count}")
    f.seek(table)
    raw = f.read(16 * count)
    blocks, total = [], 0
    for i in range(count):
        _, lo, hi, used = struct.unpack_from("<IIII", raw, 16 * i)
        off = lo | hi << 32
        if off + used > size:
            raise PmfError("block outside the file")
        blocks.append((off, used))
        total += used
    if total < ssize:
        raise PmfError(f"blocks hold {total} bytes, stream needs {ssize}")
    return blocks


def _stream(f, blocks, ssize: int, limit: int | None = None) -> bytes:
    out, want = bytearray(), ssize if limit is None else min(ssize, limit)
    for off, used in blocks:
        if len(out) >= want:
            break
        f.seek(off)
        out += f.read(min(used, want - len(out)))
    return bytes(out)


def read_pmf(path: str) -> dict:
    """Format of a .pmf (no audio read). Raises PmfError when the layout is not recognised."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        d = _directory(f, size)
        if "MH_" not in d:
            raise PmfError("no media header (MH_)")
        ssize, table = d["MH_"]
        mh = _stream(f, _blocks(f, table, ssize, size), ssize)
        if len(mh) < MH_FRAMES + 8:
            raise PmfError("media header too short")
        rate, bits = struct.unpack_from("<II", mh, MH_RATE)
        frames = struct.unpack_from("<Q", mh, MH_FRAMES)[0]
        tracks = sorted((n for n in d if n.startswith("A_")), key=lambda n: int(n[2:]))
        if rate not in (32000, 44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000) or bits not in (16, 24, 32):
            raise PmfError(f"unexpected format {rate} Hz / {bits} bits")
        if not tracks:
            raise PmfError("no audio track")
        # each track holds frames * bytes per sample (checked: a wrong layout fails here)
        bad = [n for n in tracks if d[n][0] != frames * bits // 8]
        if bad:
            raise PmfError(f"track size does not match the header: {bad}")
    return {"container": "pmf", "codec": "pcm", "sample_rate": rate, "bits": bits, "channels": len(tracks),
            "frames": frames, "duration_s": frames / rate}


def read_tracks(path: str, max_frames: int | None = None) -> tuple[dict, list[bytes]]:
    """Format and raw PCM per track (little-endian), for previews and tests."""
    info = read_pmf(path)
    width = info["bits"] // 8
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        d = _directory(f, size)
        out = []
        for n in sorted((n for n in d if n.startswith("A_")), key=lambda n: int(n[2:])):
            ssize, table = d[n]
            out.append(_stream(f, _blocks(f, table, ssize, size), ssize,
                               None if max_frames is None else max_frames * width))
    return info, out


def to_wav(src: str, dst: str, chunk_frames: int = 48000) -> dict:
    """Write the tracks of a .pmf as one interleaved WAV (same rate, bits and channel count).
    WAVE_FORMAT_EXTENSIBLE above 2 channels or 16 bits, as the WAV specification asks."""
    info, tracks = read_tracks(src)
    w, n, frames = info["bits"] // 8, info["channels"], info["frames"]
    data_size = frames * w * n
    ext = n > 2 or info["bits"] > 16
    fmt = struct.pack("<HHIIHH", 0xFFFE if ext else 1, n, info["sample_rate"], info["sample_rate"] * w * n, w * n,
                      info["bits"])
    if ext:
        sub = bytes.fromhex("0100000000001000800000aa00389b71")      # KSDATAFORMAT_SUBTYPE_PCM
        fmt += struct.pack("<HHI", 22, info["bits"], 0) + sub
    with open(dst, "wb") as out:
        out.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + data_size + (data_size & 1)) + b"WAVE")
        out.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", data_size))
        for start in range(0, frames, chunk_frames):
            end = min(frames, start + chunk_frames)
            parts = [t[start * w:end * w] for t in tracks]
            buf = bytearray((end - start) * w * n)
            for c, p in enumerate(parts):
                for k in range(w):
                    buf[c * w + k::w * n] = p[k::w]
            out.write(buf)
        if data_size & 1:
            out.write(b"\0")
    return info
