"""Read the technical metadata of audio files from their HEADERS only (never the audio data).

WAV / BWF / RF64 / BW64 and AIFF / AIFC are parsed natively by walking their chunks with seeks:
over a slow network share only a few kilobytes are read per file. Anything else goes through
ffprobe (which also reads headers). Missing or broken fields are left out, never guessed.
"""
import json
import re
import struct
import subprocess

BEXT_FIELDS = 256 + 32 + 32 + 10 + 8 + 8          # Description .. TimeReference
IXML_MAX = 65536                                   # iXML is text; real ones are a few KB
IXML_TAGS = ("PROJECT", "SCENE", "TAKE", "TAPE", "NOTE", "CIRCLED", "FILE_UID")


class HeaderError(Exception):
    pass


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip() if raw else ""


def _ixml(text: str) -> dict:
    out = {}
    for tag in IXML_TAGS:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S | re.I)
        if m and m.group(1).strip():
            out[tag.lower()] = re.sub(r"\s+", " ", m.group(1)).strip()[:500]
    return out


def read_wav(f) -> dict:
    """RIFF/WAVE, RF64 and BW64. f: a binary file object positioned at 0."""
    head = f.read(12)
    if len(head) < 12 or head[8:12] != b"WAVE" or head[:4] not in (b"RIFF", b"RF64", b"BW64"):
        raise HeaderError("not a WAVE file")
    info = {"container": head[:4].decode().strip().lower().replace("riff", "wav")}
    big_data = None                                    # RF64: real data size from ds64
    data_size = None
    pos = 12
    while True:
        f.seek(pos)
        ch = f.read(8)
        if len(ch) < 8:
            break
        cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
        body = pos + 8
        if cid == b"ds64":
            d = f.read(min(size, 28))
            if len(d) >= 16:
                big_data = struct.unpack("<Q", d[8:16])[0]
        elif cid == b"fmt ":
            d = f.read(min(size, 40))
            if len(d) >= 16:
                tag, ch_n, rate, _, align, bits = struct.unpack("<HHIIHH", d[:16])
                if tag == 0xFFFE and len(d) >= 40:          # WAVE_FORMAT_EXTENSIBLE: real format in the GUID
                    tag = struct.unpack("<H", d[24:26])[0]
                info.update({"codec": {1: "pcm", 3: "float", 6: "alaw", 7: "ulaw", 0x55: "mp3"}.get(tag, f"0x{tag:04x}"),
                             "channels": ch_n, "sample_rate": rate, "bits": bits, "_align": align})
        elif cid == b"data":
            data_size = big_data if (size == 0xFFFFFFFF and big_data is not None) else size
        elif cid == b"bext":
            d = f.read(min(size, BEXT_FIELDS))
            if len(d) >= 256:
                info["bext_description"] = _text(d[:256])
            if len(d) >= 288:
                info["bext_originator"] = _text(d[256:288])
            if len(d) >= 338:
                date, time = _text(d[320:330]), _text(d[330:338])
                if date:
                    info["bext_date"] = f"{date} {time}".strip()
            if len(d) >= 346:
                info["time_reference"] = struct.unpack("<Q", d[338:346])[0]
        elif cid == b"iXML":
            info["has_ixml"] = True
            info.update({f"ixml_{k}": v for k, v in _ixml(f.read(min(size, IXML_MAX)).decode("utf-8", "replace")).items()})
        elif cid == b"cue ":
            d = f.read(4)
            if len(d) == 4:
                info["markers"] = struct.unpack("<I", d)[0]
        if cid == b"data" and size == 0xFFFFFFFF and big_data is not None:
            size = big_data
        pos = body + size + (size & 1)                  # chunks are word-aligned
    align = info.pop("_align", 0)
    if data_size is not None and align:
        info["frames"] = data_size // align
        if info.get("sample_rate"):
            info["duration_s"] = round(info["frames"] / info["sample_rate"], 3)
    return info


def _extended(b: bytes) -> float:
    """80-bit IEEE 754 extended (AIFF sample rate)."""
    exp = ((b[0] & 0x7F) << 8 | b[1]) - 16383 - 63
    mant = int.from_bytes(b[2:10], "big")
    return mant * 2.0 ** exp * (-1 if b[0] & 0x80 else 1)


def read_aiff(f) -> dict:
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"FORM" or head[8:12] not in (b"AIFF", b"AIFC"):
        raise HeaderError("not an AIFF file")
    info = {"container": head[8:12].decode().lower()}
    pos = 12
    while True:
        f.seek(pos)
        ch = f.read(8)
        if len(ch) < 8:
            break
        cid, size = ch[:4], struct.unpack(">I", ch[4:])[0]
        if cid == b"COMM":
            d = f.read(min(size, 22))
            if len(d) >= 18:
                ch_n, frames, bits = struct.unpack(">HIH", d[:8])
                info.update({"channels": ch_n, "frames": frames, "bits": bits,
                             "sample_rate": int(round(_extended(d[8:18]))),
                             "codec": d[18:22].decode("latin-1").strip().lower() if len(d) >= 22 else "pcm"})
                if info["sample_rate"]:
                    info["duration_s"] = round(frames / info["sample_rate"], 3)
        elif cid in (b"NAME", b"ANNO", b"(c) "):
            text = f.read(min(size, 2000)).decode("latin-1", "replace").strip("\0 ")
            if text and cid != b"(c) ":
                info.setdefault("bext_description", text[:500])
        elif cid == b"MARK":
            d = f.read(2)
            if len(d) == 2:
                info["markers"] = struct.unpack(">H", d)[0]
        pos += 8 + size + (size & 1)
    return info


def ffprobe(path: str, timeout: int = 60) -> dict:
    """Other audio formats and video: container, codecs, duration, audio layout."""
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise HeaderError((out.stderr or "ffprobe failed").strip().splitlines()[-1][:200])
    d = json.loads(out.stdout or "{}")
    fmt, streams = d.get("format", {}), d.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1]
    info = {"container": (fmt.get("format_name") or "").split(",")[0]}
    if fmt.get("duration"):
        info["duration_s"] = round(float(fmt["duration"]), 3)
    if audio:
        a = audio[0]
        info.update({"codec": a.get("codec_name"), "channels": a.get("channels"),
                     "sample_rate": int(a["sample_rate"]) if a.get("sample_rate") else None,
                     "bits": a.get("bits_per_raw_sample") and int(a["bits_per_raw_sample"]) or a.get("bits_per_sample") or None,
                     "audio_streams": len(audio)})
    if video:
        v = video[0]
        info.update({"video_codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
                     "frame_rate": v.get("avg_frame_rate") or v.get("r_frame_rate")})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for key in ("comment", "description", "title"):
        if tags.get(key):
            info.setdefault("bext_description", str(tags[key])[:500])
    return {k: v for k, v in info.items() if v not in (None, "", 0) or k == "channels"}


def read_headers(path: str) -> dict:
    """Header metadata of one file; raises HeaderError when nothing can be read."""
    low = path.lower()
    if low.endswith((".wav", ".bwf", ".rf64", ".w64")):
        try:
            with open(path, "rb") as f:
                return read_wav(f)
        except HeaderError:
            pass                                        # e.g. a mislabelled file: try ffprobe
    elif low.endswith((".aif", ".aiff", ".aifc")):
        try:
            with open(path, "rb") as f:
                return read_aiff(f)
        except HeaderError:
            pass
    return ffprobe(path)
