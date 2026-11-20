import io
import os
import shutil
import sqlite3
import struct

import pytest

from soundlab import headers, inventory, report


def chunk(cid: bytes, body: bytes) -> bytes:
    return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def fmt(channels=2, rate=96000, bits=24, extensible=False) -> bytes:
    align = channels * bits // 8
    base = struct.pack("<HHIIHH", 0xFFFE if extensible else 1, channels, rate, rate * align, align, bits)
    if extensible:     # cbSize, valid bits, channel mask, SubFormat GUID (PCM)
        base += struct.pack("<HHI", 22, bits, 0) + struct.pack("<H", 1) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    return chunk(b"fmt ", base)


def bext(description: str, originator="Roland", date="2009-03-14", time="10:22:05", tref=96000 * 3600) -> bytes:
    d = description.encode().ljust(256, b"\0") + originator.encode().ljust(32, b"\0") + b"".ljust(32, b"\0")
    d += date.encode().ljust(10, b"\0") + time.encode().ljust(8, b"\0") + struct.pack("<Q", tref)
    return chunk(b"bext", d.ljust(602, b"\0"))


def wav(*chunks: bytes, frames=96000, align=6, form=b"RIFF") -> bytes:
    body = b"WAVE" + b"".join(chunks) + chunk(b"data", b"\0" * frames * align)
    return form + struct.pack("<I", len(body)) + body


def test_bwf_with_description_ixml_and_markers():
    ixml = b"<BWFXML><PROJECT>Le Film</PROJECT><SCENE>42A</SCENE><TAKE>3</TAKE><NOTE>porte metal lourde</NOTE></BWFXML>"
    data = wav(fmt(), bext("DOORMetl Heavy slam, interior"), chunk(b"iXML", ixml), chunk(b"cue ", struct.pack("<I", 2) + b"\0" * 48))
    info = headers.read_wav(io.BytesIO(data))
    assert info["container"] == "wav" and info["codec"] == "pcm" and info["channels"] == 2
    assert (info["sample_rate"], info["bits"], info["frames"], info["duration_s"]) == (96000, 24, 96000, 1.0)
    assert info["bext_description"] == "DOORMetl Heavy slam, interior" and info["bext_originator"] == "Roland"
    assert info["bext_date"] == "2009-03-14 10:22:05" and info["time_reference"] == 96000 * 3600
    assert info["has_ixml"] and info["ixml_scene"] == "42A" and info["ixml_take"] == "3" and info["ixml_note"] == "porte metal lourde"
    assert info["markers"] == 2


def test_extensible_and_rf64():
    info = headers.read_wav(io.BytesIO(wav(fmt(6, 48000, 24, extensible=True), frames=480, align=18)))
    assert (info["codec"], info["channels"], info["duration_s"]) == ("pcm", 6, 0.01)
    # RF64: the data size is in ds64 (here a small file written the RF64 way)
    frames, align = 4800, 4
    ds64 = chunk(b"ds64", struct.pack("<QQQI", 0, frames * align, frames, 0))
    body = b"WAVE" + ds64 + fmt(2, 48000, 16) + b"data" + struct.pack("<I", 0xFFFFFFFF) + b"\0" * frames * align
    info = headers.read_wav(io.BytesIO(b"RF64" + struct.pack("<I", 0xFFFFFFFF) + body))
    assert info["container"] == "rf64" and info["frames"] == frames and info["duration_s"] == 0.1


def ext80(rate: float) -> bytes:
    import math
    m, e = math.frexp(rate)
    return struct.pack(">H", e - 1 + 16383) + int(m * 2 ** 64).to_bytes(8, "big")


def aiff(rate=44100, frames=44100, channels=1, bits=16, annotation=b"Wind in pines, distant") -> bytes:
    comm = struct.pack(">HIH", channels, frames, bits) + ext80(rate)
    body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm
    body += b"ANNO" + struct.pack(">I", len(annotation)) + annotation + (b"\0" if len(annotation) & 1 else b"")
    ssnd = b"\0" * 8 + b"\0" * (frames * channels * bits // 8)
    body += b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    return b"FORM" + struct.pack(">I", len(body)) + body


def test_aiff():
    info = headers.read_aiff(io.BytesIO(aiff()))
    assert (info["container"], info["sample_rate"], info["channels"], info["bits"], info["duration_s"]) == ("aiff", 44100, 1, 16, 1.0)
    assert info["bext_description"] == "Wind in pines, distant"


def test_not_a_wave_is_refused():
    with pytest.raises(headers.HeaderError):
        headers.read_wav(io.BytesIO(b"RIFF\0\0\0\0AVI LIST"))


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "SonoFilms"
    (root / "Portes" / "Metal").mkdir(parents=True)
    (root / "Rushes 2009").mkdir()
    (root / "@Recycle").mkdir()
    (root / "Sessions").mkdir()
    door = wav(fmt(), bext("Heavy metal door"))
    (root / "Portes" / "Metal" / "porte_01.wav").write_bytes(door)
    (root / "Portes" / "Metal" / "porte_01 copie.wav").write_bytes(door)          # duplicate
    (root / "Rushes 2009" / "ZOOM0012.WAV").write_bytes(wav(fmt(2, 48000, 16), frames=48000 * 3, align=4))
    (root / "Portes" / "vent.aif").write_bytes(aiff())
    (root / "Sessions" / "Bobine 3.ptx").write_bytes(b"PTX" * 40)
    (root / "Sessions" / "Bobine 3.aaf").write_bytes(b"AAF" * 40)
    (root / "@Recycle" / "old.wav").write_bytes(door)                               # never walked
    (root / "Portes" / "._porte_01.wav").write_bytes(b"\0" * 10)                     # AppleDouble
    (root / "Portes" / "notes.txt").write_text("x")
    return root


def test_inventory_report_and_resume(library, tmp_path):
    cat = str(tmp_path / "out" / "catalog.sqlite")
    totals = inventory.run([str(library)], cat, workers=2)
    assert totals == {"seen": 7, "read": 7, "errors": 0}
    db = sqlite3.connect(cat)
    rows = {r[0]: r[1:] for r in db.execute("SELECT relpath, kind, sample_rate, duration_s, bext_description FROM files")}
    assert rows["Portes/Metal/porte_01.wav"] == ("audio", 96000, 1.0, "Heavy metal door")
    assert rows["Rushes 2009/ZOOM0012.WAV"][:3] == ("audio", 48000, 3.0)
    assert rows["Portes/vent.aif"][0] == "audio" and rows["Sessions/Bobine 3.aaf"][0] == "session"
    assert rows["Portes/notes.txt"][0] == "other" and not any("Recycle" in k or "._" in k for k in rows)
    q = [r[0] for r in db.execute("SELECT quick_id FROM files WHERE relpath LIKE 'Portes/Metal/%'")]
    assert len(q) == 2 and q[0] == q[1]
    db.close()
    # Resume: nothing changed, nothing read again; a deleted file leaves the catalog.
    assert inventory.run([str(library)], cat)["read"] == 0
    os.remove(library / "Portes" / "notes.txt")
    assert inventory.run([str(library)], cat)["seen"] == 6
    md = report.build(cat)
    assert "| SonoFilms | 6 |" in md and "1 groups, 1 extra copies" in md
    assert "| .ptx | 1 | SonoFilms |" in md and "With a description" in md


@pytest.mark.skipif(not shutil.which("ffprobe") or not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_ffprobe_for_other_formats(tmp_path):
    import subprocess
    out = tmp_path / "tone.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "48000",
                    str(out)], check=True)
    info = headers.read_headers(str(out))
    assert info["codec"] == "flac" and info["sample_rate"] == 48000 and abs(info["duration_s"] - 2) < 0.05
