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
    elif low.endswith(".sd2"):                          # data fork = raw PCM, format in the resource fork
        import os
        info = read_unknown(path, os.path.getsize(path))
        if info:
            return info
        raise HeaderError("Sound Designer II without its resource fork")
    return ffprobe(path)


# --- Files without a usable extension (classic Mac OS era) -----------------------------------
# Split-mono channel suffixes of Pro Tools / Sound Designer II sessions ("Door 3.L", "Amb.R").
CHANNEL_SUFFIX = {".l", ".r", ".c", ".ls", ".rs", ".lfe", ".s", ".m", ".lc", ".rc", ".cs",
                  ".p1", ".p2", ".p3", ".p4", ".p5", ".p6", ".p7", ".p8", ".w", ".x", ".y", ".z"}


def sniff(head: bytes) -> str | None:
    """Audio container from the first bytes, whatever the name says."""
    if len(head) >= 12 and head[:4] in (b"RIFF", b"RF64", b"BW64") and head[8:12] == b"WAVE":
        return "wav"
    if len(head) >= 12 and head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"caff":
        return "caf"
    if head[:4] == b".snd":
        return "au"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


APPLEDOUBLE_MAGIC = 0x00051607


def resource_fork(appledouble: bytes) -> bytes | None:
    """The resource fork stored in an AppleDouble file (entry id 2), or None."""
    if len(appledouble) < 26 or struct.unpack(">I", appledouble[:4])[0] != APPLEDOUBLE_MAGIC:
        return None
    n = struct.unpack(">H", appledouble[24:26])[0]
    for i in range(n):
        off = 26 + 12 * i
        if off + 12 > len(appledouble):
            break
        eid, start, length = struct.unpack(">III", appledouble[off:off + 12])
        if eid == 2 and length:
            return appledouble[start:start + length]
    return None


def sd2_from_resources(fork: bytes) -> dict:
    """Sound Designer II keeps its format in 'STR ' resources named sample-rate, sample-size
    and channels (values as text, e.g. '48000.000'). Returns what it finds."""
    if len(fork) < 16:
        return {}
    data_off, map_off = struct.unpack(">II", fork[:8])
    if map_off + 30 > len(fork):
        return {}
    types_off = map_off + struct.unpack(">H", fork[map_off + 24:map_off + 26])[0]
    names_off = map_off + struct.unpack(">H", fork[map_off + 26:map_off + 28])[0]
    n_types = struct.unpack(">H", fork[types_off:types_off + 2])[0] + 1
    found = {}
    for t in range(n_types):
        e = types_off + 2 + 8 * t
        rtype, count, ref_off = fork[e:e + 4], struct.unpack(">H", fork[e + 4:e + 6])[0] + 1, struct.unpack(">H", fork[e + 6:e + 8])[0]
        if rtype != b"STR ":
            continue
        for r in range(count):
            ref = types_off + ref_off + 12 * r
            name_rel = struct.unpack(">H", fork[ref + 2:ref + 4])[0]
            doff = int.from_bytes(fork[ref + 5:ref + 8], "big")
            if name_rel == 0xFFFF:
                continue
            nl = fork[names_off + name_rel]
            name = fork[names_off + name_rel + 1:names_off + name_rel + 1 + nl].decode("latin-1")
            d = data_off + doff
            ln = struct.unpack(">I", fork[d:d + 4])[0]
            raw = fork[d + 4:d + 4 + ln]
            value = raw[1:1 + raw[0]].decode("latin-1") if raw else ""
            found[name.lower()] = value.strip()
    out = {}
    try:
        if "sample-rate" in found:
            out["sample_rate"] = int(round(float(found["sample-rate"])))
        if "sample-size" in found:
            out["bits"] = int(found["sample-size"]) * 8
        if "channels" in found:
            out["channels"] = int(found["channels"])
    except ValueError:
        pass
    return out


def appledouble_path(path: str) -> str | None:
    """Where a Mac file server keeps the resource fork of `path`, if it exists."""
    import os
    d, name = os.path.split(path)
    for cand in (os.path.join(d, "._" + name), os.path.join(d, ".AppleDouble", name)):
        if os.path.isfile(cand):
            return cand
    return None


def read_unknown(path: str, size: int) -> dict | None:
    """A file whose extension says nothing: sniff its content; else, a Sound Designer II data
    fork (raw PCM) described by its AppleDouble resource fork. None when it is not audio."""
    with open(path, "rb") as f:
        head = f.read(16)
    kind = sniff(head)
    if kind in ("wav", "aiff"):
        with open(path, "rb") as f:
            return (read_wav if kind == "wav" else read_aiff)(f)
    if kind:
        return ffprobe(path)
    ad = appledouble_path(path)
    if not ad:
        return None
    with open(ad, "rb") as f:
        fork = resource_fork(f.read(1 << 20))
    fmt = sd2_from_resources(fork) if fork else {}
    if not fmt.get("sample_rate"):
        return None
    info = {"container": "sd2", "codec": "pcm", **fmt}
    bps = (fmt.get("bits") or 16) // 8 * (fmt.get("channels") or 1)
    if bps:
        info["frames"] = size // bps
        info["duration_s"] = round(info["frames"] / fmt["sample_rate"], 3)
    return info
