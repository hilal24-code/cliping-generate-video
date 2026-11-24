import json, re, shutil, subprocess, sys, threading, time, uuid
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
CLIPS = BASE / "clips"
CLIPS.mkdir(exist_ok=True)
SFX_DIR = BASE / "sfx"          # taruh file suara sendiri (mp3/wav) di sini
SFX_DIR.mkdir(exist_ok=True)
MAX_SFX_BYTES = 15 * 1024 * 1024
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}

# Font teks dan emoji (dipakai yang pertama ditemukan)
TEXT_FONTS = ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"]
EMOJI_FONTS = ["C:/Windows/Fonts/seguiemj.ttf"]

app = FastAPI(title="YT Clipper")
YTDLP = [sys.executable, "-m", "yt_dlp"]


def to_seconds(value: str) -> float:
    """Terima '90', '1:30', atau '01:01:30'."""
    parts = [float(p) for p in str(value).strip().split(":")]
    if not 1 <= len(parts) <= 3:
        raise ValueError("format waktu salah")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def run(cmd: list[str], cwd=None) -> str:
    if cmd[0] in ("ffmpeg", "ffprobe") and not shutil.which(cmd[0]):
        raise HTTPException(500, f"{cmd[0]} belum terpasang atau belum ada di PATH. "
                                 "Pasang dengan: winget install Gyan.FFmpeg, lalu buka ulang terminal.")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=cwd)
    except FileNotFoundError as e:
        raise HTTPException(500, f"Program tidak ditemukan: {e}")
    if r.returncode != 0:
        raise HTTPException(400, (r.stderr or "perintah gagal").strip()[-600:])
    return r.stdout


class InfoReq(BaseModel):
    url: str


class ItemReq(BaseModel):
    text: str = ""            # teks (boleh emoji, boleh kosong kalau hanya suara)
    at: str = "0:00"          # muncul di menit:detik DARI AWAL KLIP
    dur: float = 3.0          # tampil selama N detik
    pos: str = "center"       # top | center | bottom
    sfx: str = ""             # nama efek suara (kosong = tanpa suara)


class MuteReq(BaseModel):
    at: str = "0:00"          # mulai mute, DARI AWAL KLIP (boleh pecahan, mis. 0:12.5)
    dur: float = 1.0          # lama mute (detik)
    beep: bool = False        # True = ganti dengan bunyi sensor "tiiit"


class ClipReq(BaseModel):
    url: str
    start: str
    end: str
    vertical: bool = False
    title: str = ""
    hook: str = ""
    hook_secs: float = 5.0    # teks judul hilang setelah N detik (0 = tidak hilang)
    facecam: str = "none"     # none | tl | tr | bl | br
    face_size: float = 0.25   # lebar facecam (0.1 - 0.6 dari lebar video)
    face_off: float = 0.0     # geser menjauhi tepi (0 - 0.6 dari tinggi video)
    items: List[ItemReq] = []
    mutes: List[MuteReq] = []
    sfx_vol: float = 1.0      # volume efek suara
    subs: bool = False        # subtitle otomatis (Whisper)
    sub_lang: str = "id"      # id | en | auto
    sub_model: str = "small"  # tiny | base | small | medium
    sub_style: str = "word"   # word (kata aktif menyala) | plain
    sub_pos: str = "bottom"   # bottom | center | top
    sub_color: str = "yellow" # warna kata aktif
    sub_sens: str = "normal"  # normal | max (tanpa filter VAD, tangkap semua ucapan)
    sub_terms: str = ""       # istilah/nama khusus untuk membantu AI (mis. nama karakter)
    sub_only: bool = True     # buang bagian yang bahasanya beda (dialog game), simpan suara streamer


@app.post("/api/info")
def info(req: InfoReq):
    out = run([*YTDLP, "--no-playlist", "--dump-single-json", req.url])
    d = json.loads(out)
    return {"title": d.get("title"), "duration": d.get("duration"),
            "thumbnail": d.get("thumbnail")}


# ------------------------------------------------------------- efek suara

# Efek bawaan dibuat otomatis oleh ffmpeg (suara sintetis sederhana).
BUILTIN_SFX = {
    "ding": "aevalsrc='(0.55*sin(2*PI*1320*t)+0.25*sin(2*PI*2640*t))*exp(-4*t)':d=1.2:s=44100",
    "koin": "aevalsrc='0.5*sin(2*PI*if(lt(t,0.09),988,1319)*t)*exp(-3*t)':d=0.8:s=44100",
    "pop": "aevalsrc='0.8*sin(2*PI*(900*t-3000*t*t))*exp(-22*t)':d=0.25:s=44100",
    "whoosh": "anoisesrc=d=0.9:c=white:a=0.5:r=44100,highpass=f=400,lowpass=f=3500,"
              "afade=t=in:d=0.4,afade=t=out:st=0.4:d=0.5",
    "boom": "aevalsrc='0.9*sin(2*PI*55*t)*exp(-3*t)+0.3*sin(2*PI*110*t)*exp(-6*t)':d=1.5:s=44100",
    "alarm": "aevalsrc='0.45*sin(2*PI*1000*t)*lt(mod(t,0.3),0.15)':d=0.9:s=44100",
    "salah": "aevalsrc='0.3*(2*gt(sin(2*PI*140*t),0)-1)*exp(-1.2*t)':d=0.7:s=44100",
    "sedih": "aevalsrc='0.4*sin(2*PI*if(lt(t,0.4),392,if(lt(t,0.8),370,330))*t)*exp(-1.2*t)':d=1.6:s=44100",
}


def ensure_sfx():
    if not shutil.which("ffmpeg"):
        return
    for name, src in BUILTIN_SFX.items():
        out = SFX_DIR / f"{name}.wav"
        if out.exists():
            continue
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", src,
                        "-ac", "2", "-ar", "44100", str(out)], capture_output=True)


def sfx_files() -> dict:
    ensure_sfx()
    return {p.stem: p for p in sorted(SFX_DIR.iterdir())
            if p.is_file() and p.suffix.lower() in AUDIO_EXT}


@app.get("/api/sfx")
def list_sfx():
    return [{"name": k, "file": f"/sfx/{p.name}", "builtin": k in BUILTIN_SFX}
            for k, p in sfx_files().items()]


@app.post("/api/sfx/upload")
async def upload_sfx(request: Request, name: str):
    """Upload efek suara sendiri. Isi file dikirim apa adanya sebagai body."""
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(400, "Format tidak didukung. Pakai mp3, wav, ogg, m4a, aac, atau flac.")
    data = await request.body()
    if not data:
        raise HTTPException(400, "File kosong")
    if len(data) > MAX_SFX_BYTES:
        raise HTTPException(400, "File terlalu besar (maksimal 15 MB)")

    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_")[:40] or "suara"
    if stem in BUILTIN_SFX:
        stem += "_saya"
    for old in SFX_DIR.glob(f"{stem}.*"):       # timpa versi lama dengan nama sama
        old.unlink(missing_ok=True)
    path = SFX_DIR / f"{stem}{ext}"
    path.write_bytes(data)

    try:
        has_audio = bool(run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                              str(path)]).strip())
    except HTTPException:
        has_audio = False
    if not has_audio:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "File ini tidak berisi audio yang bisa dibaca")
    return {"name": stem, "file": f"/sfx/{path.name}", "builtin": False}


@app.delete("/api/sfx/{name}")
def delete_sfx(name: str):
    if name in BUILTIN_SFX:
        raise HTTPException(400, "Efek bawaan tidak bisa dihapus")
    path = sfx_files().get(name)
    if not path:
        raise HTTPException(404, "Efek suara tidak ditemukan")
    path.unlink(missing_ok=True)
    return {"ok": True}


# ----------------------------------------------------- teks (judul + custom)

_EM_BASE = (r"(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2B1B\u2B1C"
            r"\u231A\u231B\u23E9-\u23FA\u2934\u2935\u00A9\u00AE\u203C\u2049\u2122\u2139])")
_EM_MOD = r"[\uFE0F\U0001F3FB-\U0001F3FF]*"
EMOJI_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2}"                 # bendera
    r"|[0-9#*]\uFE0F?\u20E3"                         # keycap
    rf"|{_EM_BASE}{_EM_MOD}(?:\u200D{_EM_BASE}{_EM_MOD})*)")


def _first_existing(paths):
    for p in paths:
        if Path(p).exists():
            return p
    return None


def make_text_png(text: str, out: Path, band_w: int = 1000, scale: float = 1.0,
                  fit: bool = False) -> int:
    """Gambar kotak putih berisi teks (emoji berwarna) ke PNG. Return tinggi kotak.
    fit=True: lebar kotak menyesuaikan isi (maksimal band_w)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("Pillow belum terpasang. Jalankan: python -m pip install pillow")

    tpath = _first_existing(TEXT_FONTS)
    epath = _first_existing(EMOJI_FONTS)
    if not tpath:
        raise RuntimeError("Font teks tidak ditemukan. Atur TEXT_FONTS di main.py.")
    try:
        efont = ImageFont.truetype(epath, 109) if epath else None
    except OSError:
        efont = None
    cache = {}

    def emoji_img(seq, f):
        key = (seq, f)
        if key in cache:
            return cache[key]
        w = max(int(efont.getlength(seq)) + 6, 100)
        big = Image.new("RGBA", (w, 170), (0, 0, 0, 0))
        ImageDraw.Draw(big).text((2, 130), seq, font=efont, embedded_color=True, anchor="ls")
        k = f * 1.15 / 109
        img = big.resize((max(1, int(w * k)), max(1, int(170 * k))), Image.LANCZOS)
        cache[key] = (img, int(130 * k))
        return cache[key]

    def runs_of(word):
        res, pos = [], 0
        for m in EMOJI_RE.finditer(word):
            if m.start() > pos:
                res.append(("t", word[pos:m.start()]))
            res.append(("e", m.group()))
            pos = m.end()
        if pos < len(word):
            res.append(("t", word[pos:]))
        if not efont:
            res = [r for r in res if r[0] == "t"]
        return res

    def width_of(runs, tf, f):
        total = 0.0
        for kind, val in runs:
            total += tf.getlength(val) if kind == "t" else emoji_img(val, f)[0].width
        return total

    words = [w for w in text.upper().split() if w]
    if not words:
        words = [" "]
    for base in (54, 48, 42, 36):
        fsz = max(10, int(base * scale))
        tf = ImageFont.truetype(tpath, fsz)
        space = tf.getlength(" ")
        maxw = band_w - 80 * scale
        lines, cur, curw = [], [], 0.0
        for w in words:
            rw = runs_of(w)
            ww = width_of(rw, tf, fsz)
            if cur and curw + space + ww > maxw:
                lines.append(cur); cur, curw = [], 0.0
            curw = curw + (space if cur else 0) + ww
            cur.append(rw)
        if cur:
            lines.append(cur)
        if len(lines) <= 3:
            break

    lh, pad = int(fsz * 1.22), int(24 * scale)
    band_h = len(lines) * lh + 2 * pad

    def line_w(line):
        return sum(width_of(r, tf, fsz) for r in line) + space * (len(line) - 1)

    bw = band_w
    if fit:
        padx = int(34 * scale)
        bw = min(band_w, int(max(line_w(l) for l in lines) + 2 * padx))
        bw = max(bw, int(band_h * 0.9))          # jangan lebih sempit dari tingginya
    img = Image.new("RGBA", (bw, band_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, bw - 1, band_h - 1), radius=min(int(22 * scale), band_h // 2),
                        fill=(255, 255, 255, 242))
    for i, line in enumerate(lines):
        base_y = pad + i * lh + int(fsz * 0.95)
        x = (bw - line_w(line)) / 2
        for rw in line:
            for kind, val in rw:
                if kind == "t":
                    d.text((x, base_y), val, font=tf, fill=(0, 0, 0, 255), anchor="ls")
                    x += tf.getlength(val)
                else:
                    em, eb = emoji_img(val, fsz)
                    img.alpha_composite(em, (int(x), base_y - eb))
                    x += em.width
            x += space
    img.save(out)
    return band_h


def base_graph(req: ClipReq):
    """Bagian dasar: crop/susun video. Hasil akhirnya berlabel [v0]."""
    top = 700  # tinggi panel facecam (kecilkan = facecam lebih tajam)
    if not req.vertical:
        return "[0:v]null[v0]", 0
    if req.facecam in ("tl", "tr", "bl", "br"):
        fs = min(max(req.face_size, 0.1), 0.6)
        off = min(max(req.face_off, 0), 0.6)
        x = "0" if req.facecam[1] == "l" else "iw-ow"
        y = f"ih*{off}" if req.facecam[0] == "t" else f"ih-oh-ih*{off}"
        return (f"[0:v]split=2[a][b];"
                f"[a]crop=iw*{fs}:iw*{fs}*{top}/1080:{x}:{y},scale=1080:{top}:flags=lanczos[face];"
                f"[b]crop=ih*1080/{1920-top}:ih,scale=1080:{1920-top}:flags=lanczos[game];"
                f"[face][game]vstack[v0]"), top
    return "[0:v]crop=ih*9/16:ih,scale=1080:1920:flags=lanczos[v0]", 300


def place_y(pos: str, H: int, bh: int) -> int:
    if pos == "top":
        return int(H * 0.07)
    if pos == "bottom":
        return H - bh - int(H * 0.13)
    return (H - bh) // 2


def encode(req: ClipReq, items: list, mutes: list, jid: str, raw: Path, final: Path,
           src: str, on_enc, temp: list, subs=None):
    """Susun video akhir: crop, teks judul, teks custom, efek suara."""
    sw, sh = (int(v) for v in src.split("x")[:2])
    W, H = (1080, 1920) if req.vertical else (sw, sh)
    scale = W / 1080
    band_w = int(W * 0.926)
    dur = max(0.1, to_seconds(req.end) - to_seconds(req.start))

    g, center = base_graph(req)
    inputs, n_in, overlays, sfx_in = [], 1, [], []

    if req.vertical and req.hook.strip():
        name = f"{jid}_hook.png"
        temp.append(name)
        bh = make_text_png(req.hook.strip(), CLIPS / name, band_w, scale)
        en = f"between(t,0,{req.hook_secs})" if req.hook_secs > 0 else ""
        overlays.append((n_in, center - bh // 2, en))
        inputs += ["-i", name]
        n_in += 1

    for k, it in enumerate(items):
        if it["text"].strip():
            name = f"{jid}_it{k}.png"
            temp.append(name)
            bh = make_text_png(it["text"].strip(), CLIPS / name, band_w, scale, fit=True)
            en = f"between(t,{it['at']},{it['at'] + it['dur']})"
            overlays.append((n_in, place_y(it["pos"], H, bh), en))
            inputs += ["-i", name]
            n_in += 1
        if it["sfx"]:
            inputs += ["-i", str(it["sfx"])]
            sfx_in.append((n_in, it["at"]))
            n_in += 1

    has_audio = bool(run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=index", "-of", "csv=p=0",
                          str(raw)]).strip())

    cur = "v0"
    for k, (idx, y, en) in enumerate(overlays):
        nxt = f"o{k}"
        enable = f":enable='{en}'" if en else ""
        g += f";[{cur}][{idx}:v]overlay=x=(W-w)/2:y={y}{enable}[{nxt}]"
        cur = nxt
    if subs:
        g += f";[{cur}]ass={subs['ass']}:fontsdir={subs['fonts']}[v]"
    else:
        g += f";[{cur}]null[v]"

    audio_map = "0:a?"
    if has_audio and (sfx_in or mutes):
        # 1) bisukan suara asli di bagian yang diminta (efek suara tambahan tidak ikut dibisukan)
        a_main = "[0:a]"
        if mutes:
            mexpr = ",".join(f"volume=0:enable='between(t,{m['at']},{m['at'] + m['dur']})'"
                             for m in mutes)
            g += f";[0:a]{mexpr}[am]"
            a_main = "[am]"
        labels = []
        # 2) efek suara tambahan
        vol = min(max(req.sfx_vol, 0.0), 3.0)
        for k, (idx, at) in enumerate(sfx_in):
            ms = int(at * 1000)
            g += (f";[{idx}:a]adelay={ms}:all=1,apad=whole_dur={dur + 1:.2f},"
                  f"volume={vol}[s{k}]")
            labels.append(f"[s{k}]")
        # 3) bunyi sensor (beep) menggantikan bagian yang dibisukan
        for k, m in enumerate([m for m in mutes if m["beep"]]):
            ms = int(m["at"] * 1000)
            fd = min(0.01, m["dur"] / 4)
            g += (f";sine=frequency=1000:duration={m['dur']:.3f}:sample_rate=44100,"
                  f"volume=2.4,afade=t=in:d={fd:.3f},afade=t=out:st={m['dur'] - fd:.3f}:d={fd:.3f},"
                  f"adelay={ms}:all=1,apad=whole_dur={dur + 1:.2f}[b{k}]")
            labels.append(f"[b{k}]")
        if labels:
            n = 1 + len(labels)
            g += (f";{a_main}{''.join(labels)}amix=inputs={n}:duration=first:"
                  f"dropout_transition=0,volume={n},alimiter=limit=0.95[a]")
            audio_map = "[a]"
        else:
            audio_map = a_main

    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart"]
    head = ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-i", raw.name]

    if req.vertical or overlays or subs or ((sfx_in or mutes) and has_audio):
        cmd = [*head, *inputs, "-filter_complex", g,
               "-map", "[v]", "-map", audio_map, *enc, final.name]
    else:
        cmd = [*head, *enc, final.name]
    stream(cmd, cwd=CLIPS, on_line=on_enc)


# ------------------------------------------------------- subtitle otomatis

WHISPER_MODELS = {}
SUB_COLORS = {"yellow": (255, 229, 0), "green": (61, 255, 107),
              "pink": (255, 77, 166), "cyan": (0, 229, 255)}


def decode_audio_16k(path: Path):
    """Ekstrak suara video jadi data mono 16 kHz lewat ffmpeg (tanpa PyAV)."""
    import numpy as np
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg belum terpasang atau belum ada di PATH.")
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn",
                        "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0 or len(p.stdout) < 4 * 1600:
        raise RuntimeError("Video ini tidak punya suara untuk dibuat subtitle.")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def _lang_model(WhisperModel, size: str):
    """Model kecil untuk mengenali bahasa (cepat). Dipakai ulang kalau sudah ada."""
    key = size if size in ("tiny", "base", "small") else "base"
    m = WHISPER_MODELS.get(key)
    if m is None:
        m = WhisperModel(key, device="cpu", compute_type="int8")
        WHISPER_MODELS[key] = m
    return m


def transcribe_words(path: Path, lang: str, size: str, dur: float, on_model, on_progress,
                     sens: str = "normal", terms: str = "", only_lang: bool = False,
                     on_filter=None):
    """Dengarkan suara dengan Whisper (lokal).
    Return (kata-kata, bagian yang dibuang, catatan)."""
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as ex:
        if getattr(ex, "name", "") == "faster_whisper":
            raise RuntimeError("faster-whisper belum terpasang. Jalankan: "
                               "python -m pip install faster-whisper, lalu restart server.")
        raise RuntimeError(f"Komponen faster-whisper tidak lengkap ({ex}). "
                           "Coba: python -m pip install --upgrade faster-whisper")
    except ImportError as ex:
        raise RuntimeError(f"faster-whisper terpasang tapi gagal dimuat: {ex}")

    audio = decode_audio_16k(path)      # dibaca dulu, supaya error "tanpa suara" muncul sebelum unduh model
    model = WHISPER_MODELS.get(size)
    if model is None:
        on_model()
        real = "large-v3-turbo" if size == "turbo" else size
        try:
            model = WhisperModel(real, device="cpu", compute_type="int8")
        except ValueError as ex:
            raise RuntimeError(f"Model {size} tidak tersedia di faster-whisper kamu ({ex}). "
                               "Pilih model lain, atau update: python -m pip install -U faster-whisper")
        WHISPER_MODELS[size] = model

    kw = dict(language=None if lang == "auto" else lang,
              word_timestamps=True, beam_size=5, condition_on_previous_text=False,
              no_speech_threshold=None)          # jangan buang segmen yang diragukan
    if sens == "max":
        kw["vad_filter"] = False                 # tangkap semua suara, termasuk yang tertutup suara game
    else:
        kw["vad_filter"] = True                  # filter VAD dibuat longgar
        kw["vad_parameters"] = dict(threshold=0.25, min_silence_duration_ms=1000,
                                    speech_pad_ms=400)
    if terms.strip():
        kw["hotwords"] = terms.strip()[:200]
    try:
        segments, _ = model.transcribe(audio, **kw)
    except TypeError:                            # faster-whisper lama tanpa hotwords
        kw.pop("hotwords", None)
        segments, _ = model.transcribe(audio, **kw)

    segs = []
    for seg in segments:
        on_progress(min(max(seg.end / dur, 0.0), 1.0))
        ws = []
        for w in (seg.words or []):
            t = (w.word or "").strip()
            if t:
                ws.append({"raw": t, "start": float(w.start), "end": float(w.end)})
        if ws:
            segs.append({"start": float(seg.start), "end": float(seg.end), "words": ws})

    dropped, note = [], ""
    if only_lang and lang != "auto" and segs:
        try:
            lid = _lang_model(WhisperModel, size)
            total, kept = len(segs), []
            secs = len(audio) / 16000
            for i, sg in enumerate(segs):
                if on_filter:
                    on_filter(i, total)
                a0 = int(max(0.0, sg["start"] - 0.2) * 16000)
                a1 = int(min(secs, sg["end"] + 0.2) * 16000)
                chunk = audio[a0:a1]
                if len(chunk) < 16000 * 0.8:     # terlalu pendek untuk dinilai: simpan
                    kept.append(sg)
                    continue
                top, ptop, allp = lid.detect_language(audio=chunk)[:3]
                pt = dict(allp).get(lang, 0.0)
                if top != lang and pt < 0.3 and ptop >= 0.5:
                    dropped.append({"start": sg["start"], "lang": top,
                                    "text": " ".join(w["raw"] for w in sg["words"])})
                else:
                    kept.append(sg)
            segs = kept
        except Exception as ex:                  # penyaringan gagal: pakai semua, beri tahu
            note = f"Penyaringan bahasa gagal ({ex}); semua ucapan dipakai."
    words = [w for sg in segs for w in sg["words"]]
    return words, dropped, note


def transcript_text(words, dropped=None, note: str = "") -> str:
    """Teks hasil dengar AI dengan penanda waktu, untuk dicek pengguna."""
    def stamp(t):
        return f"[{int(t // 60)}:{int(t % 60):02d}]"
    lines, cur, t0 = [], [], 0.0
    for w in words:
        if not cur:
            t0 = w["start"]
        cur.append(w["raw"])
        if len(cur) >= 12 or w["raw"][-1:] in ".?!":
            lines.append(f"{stamp(t0)} " + " ".join(cur))
            cur = []
    if cur:
        lines.append(f"{stamp(t0)} " + " ".join(cur))
    if dropped:
        lines.append("")
        lines.append("--- Dibuang (terdengar bukan bahasa streamer, kemungkinan suara game) ---")
        for d in dropped:
            lines.append(f"{stamp(d['start'])} ({d['lang']}) {d['text']}")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _bgr(rgb) -> str:
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def font_family(path: str) -> str:
    try:
        from PIL import ImageFont
        return ImageFont.truetype(path, 20).getname()[0]
    except Exception:
        return "Arial"


def build_ass(words, W, H, vertical, style, pos, color, mutes, family, dur) -> str:
    """Susun file subtitle ASS: kelompok 2-4 kata, kata aktif menyala."""
    if vertical:
        fsize, max_chars, mside = int(80 * W / 1080), 18, int(60 * W / 1080)
    else:
        fsize, max_chars, mside = int(58 * H / 1080), 34, int(80 * H / 1080)
    outline = max(2, round(fsize * 0.07))
    align, mv = {"center": (5, 0), "top": (8, int(H * 0.10))}.get(pos, (2, int(H * 0.18)))
    hl = _bgr(SUB_COLORS.get(color, SUB_COLORS["yellow"]))

    toks = []
    for w in words:
        raw = w["raw"]
        t = raw.strip(",.;:\"'()[]…-–—").upper().replace("{", "").replace("}", "").replace("\\", "")
        if not t:
            continue
        wd = max(w["end"] - w["start"], 0.05)
        if any(min(w["end"], m["at"] + m["dur"]) - max(w["start"], m["at"]) > 0.5 * wd
               for m in mutes):
            t = "***"      # kata yang (sebagian besar) dibisukan disensor juga di subtitle
        toks.append({"t": t, "start": w["start"], "end": max(w["end"], w["start"] + 0.05),
                     "brk": raw[-1:] in ".?!"})

    groups, cur = [], []
    for w in toks:
        if cur:
            joined = len(" ".join(x["t"] for x in cur)) + 1 + len(w["t"])
            if (joined > max_chars or len(cur) >= 4 or cur[-1]["brk"]
                    or w["start"] - cur[-1]["end"] > 0.6):
                groups.append(cur); cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\n"
            f"WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{family},{fsize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            f"-1,0,0,0,100,100,0,0,1,{outline},2,{align},{mside},{mside},{mv},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")

    white = "&H00FFFFFF&"
    lines = []
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else dur
        g_end = min(g[-1]["end"] + 0.2, nxt, dur)
        if style == "plain":
            lines.append(f"Dialogue: 0,{ass_time(g[0]['start'])},{ass_time(max(g_end, g[0]['start'] + 0.1))},"
                         f"Default,,0,0,0,,{' '.join(x['t'] for x in g)}")
            continue
        for i, w in enumerate(g):
            start = w["start"]
            end = g[i + 1]["start"] if i + 1 < len(g) else g_end
            end = max(end, start + 0.08)
            text = " ".join(
                (f"{{\\c{hl}}}{x['t']}{{\\c{white}}}" if j == i else x["t"]) for j, x in enumerate(g))
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(min(end, dur + 0.5))},"
                         f"Default,,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


def make_subs(jid, req: ClipReq, raw: Path, src: str, dur: float, mutes: list,
              setp, temp: list, t0: float):
    """Buat file subtitle untuk klip ini. Return info untuk filter ffmpeg, atau None."""
    sw, sh = (int(v) for v in src.split("x")[:2])
    W, H = (1080, 1920) if req.vertical else (sw, sh)
    size = req.sub_model if req.sub_model in ("tiny", "base", "small", "medium", "turbo") else "small"

    def on_model():
        setp(t0, "Menyiapkan model AI",
             f"model '{size}': pertama kali harus mengunduh dulu (sekali saja), bisa beberapa menit")

    def on_prog(frac):
        setp(t0 + frac * (58 - t0), "Membuat subtitle (AI mendengarkan)",
             f"{frac * dur:.0f} dari {dur:.0f} detik")

    setp(t0, "Membuat subtitle (AI mendengarkan)", "memuat model...")
    def on_filter(i, total):
        setp(58, "Menyaring suara streamer", f"bagian {i + 1} dari {total}")

    words, dropped, note = transcribe_words(raw, req.sub_lang, size, dur, on_model, on_prog,
                                            req.sub_sens, req.sub_terms, req.sub_only, on_filter)
    if not words:
        setp(58, "Tidak ada ucapan terdeteksi", "klip dibuat tanpa subtitle")
        return {"empty": True, "transcript": transcript_text([], dropped, note)}
    tpath = _first_existing(TEXT_FONTS)
    if not tpath:
        raise RuntimeError("Font teks tidak ditemukan. Atur TEXT_FONTS di main.py.")
    fdir = f"fonts_{jid}"
    (CLIPS / fdir).mkdir(exist_ok=True)
    temp.append(fdir)
    shutil.copy(tpath, CLIPS / fdir / Path(tpath).name)
    name = f"{jid}.ass"
    temp.append(name)
    (CLIPS / name).write_text(
        build_ass(words, W, H, req.vertical, req.sub_style, req.sub_pos, req.sub_color,
                  mutes, font_family(tpath), dur), encoding="utf-8")
    return {"ass": name, "fonts": fdir, "transcript": transcript_text(words, dropped, note), "count": len(words)}


# ---------------------------------------------------------------- proses klip

JOBS: dict = {}
JOB_LOCK = threading.Lock()
PROGRESS_LINE = re.compile(
    r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|"
    r"out_time|dup_frames|drop_frames|speed|progress)=")


def stream(cmd, cwd=None, on_line=None):
    """Jalankan perintah dan baca outputnya baris demi baris."""
    if cmd[0] in ("ffmpeg", "ffprobe") and not shutil.which(cmd[0]):
        raise RuntimeError(f"{cmd[0]} belum terpasang atau belum ada di PATH.")
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        if on_line and on_line(line):
            continue
        tail = (tail + [line])[-15:]
    if p.wait() != 0:
        raise RuntimeError("\n".join(tail)[-600:] or "perintah gagal")


def run_job(jid, req: ClipReq, s: float, e: float, items: list, mutes: list):
    job = JOBS[jid]
    dur = e - s
    raw = CLIPS / f"{jid}_raw.mp4"
    final = CLIPS / f"{jid}.mp4"
    temp = []
    dl_end = 40 if req.subs else 55      # bagian bar progres untuk unduhan

    def setp(pct, stage, detail=""):
        job.update(percent=round(min(pct, 100), 1), stage=stage, detail=detail)

    def on_dl(line):
        m = re.search(r"\btime=(\d+):(\d+):([\d.]+)", line)
        if m:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            setp(2 + min(t / dur, 1) * (dl_end - 2), "Mengunduh potongan dari YouTube",
                 f"{t:.0f} dari {dur:.0f} detik")
            return True
        if not line.startswith("PROG|"):
            return False
        try:
            parts = line.split("|") + [""] * 5
            down, pct_s, speed, eta = parts[1:5]
            try:
                done = float(down)
            except ValueError:
                done = 0.0
            pm = re.search(r"([\d.]+)%", pct_s)
            p = float(pm.group(1)) if pm else min(97.0, done / (dur * 700_000) * 100)
            bits = [f"{done / 1e6:.1f} MB"]
            if speed.strip() not in ("", "NA", "Unknown B/s"):
                bits.append(speed.strip())
            if eta.strip() not in ("", "NA", "Unknown"):
                bits.append("sisa " + eta.strip())
            setp(2 + p * (dl_end - 2) / 100, "Mengunduh potongan dari YouTube", " · ".join(bits))
        except Exception:
            pass
        return True

    def on_enc(line):
        if line.startswith("out_time_us="):
            v = line.split("=", 1)[1]
            if v.isdigit():
                t = int(v) / 1e6
                setp(58 + min(t / dur, 1) * 41, "Memproses video (crop, teks, suara)",
                     f"{t:.0f} dari {dur:.0f} detik")
            return True
        return bool(PROGRESS_LINE.match(line))

    try:
        setp(0, "Menunggu antrean")
        with JOB_LOCK:
            setp(2, "Mengunduh potongan dari YouTube", "Menghubungi YouTube...")
            dl_cmd = [*YTDLP, "--no-playlist", "--newline",
                      "--progress-template",
                      "PROG|%(progress.downloaded_bytes)s|%(progress._percent_str)s|"
                      "%(progress._speed_str)s|%(progress._eta_str)s",
                      "--remote-components", "ejs:github",
                      "-f", "bv*[height<=1080]+ba/b",
                      "-S", "res:1080,fps",
                      "--download-sections", f"*{s}-{e}",
                      "--force-keyframes-at-cuts",
                      "--merge-output-format", "mp4",
                      "-o", str(raw), req.url]
            for attempt in range(1, 4):
                try:
                    stream(dl_cmd, on_line=on_dl)
                    break
                except RuntimeError as ex:
                    if "403" not in str(ex) or attempt == 3:
                        raise
                    raw.unlink(missing_ok=True)
                    setp(2, "YouTube menolak (403), mencoba ulang",
                         f"percobaan {attempt + 1} dari 3, tunggu sebentar...")
                    time.sleep(5 * attempt)

            setp(dl_end, "Memeriksa video")
            src = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=width,height",
                       "-of", "csv=p=0:s=x", str(raw)]).strip()

            subs, transcript = None, None
            if req.subs:
                subs = make_subs(jid, req, raw, src, dur, mutes, setp, temp, dl_end + 2)
                transcript = subs["transcript"] or "(tidak ada ucapan terdeteksi)"
                if subs.get("empty"):
                    subs = None
            setp(58, "Memproses video (crop, teks, suara)")
            encode(req, items, mutes, jid, raw, final, src, on_enc, temp, subs)

        meta = {"id": jid, "title": req.title or f"Klip {jid}", "url": req.url,
                "start": req.start, "end": req.end, "vertical": req.vertical,
                "src": src, "file": f"/clips/{jid}.mp4"}
        if transcript is not None:
            meta["transcript"] = transcript
        (CLIPS / f"{jid}.json").write_text(json.dumps(meta))
        job.update(status="done", result=meta)
        setp(100, "Selesai")
    except Exception as ex:
        final.unlink(missing_ok=True)
        msg = getattr(ex, "detail", None) or str(ex)
        if "403" in msg:
            msg = ("YouTube menolak akses video (403 Forbidden) setelah 3 kali percobaan. "
                   "Tunggu 5-10 menit lalu coba lagi.")
        job.update(status="error", error=msg[-400:])
    finally:
        raw.unlink(missing_ok=True)
        for name in temp:
            p = CLIPS / name
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


@app.post("/api/clip")
def clip(req: ClipReq):
    try:
        s, e = to_seconds(req.start), to_seconds(req.end)
    except ValueError:
        raise HTTPException(400, "Format waktu: detik, mm:ss, atau hh:mm:ss")
    if e <= s:
        raise HTTPException(400, "Waktu selesai harus lebih besar dari waktu mulai")
    if e - s > 600:
        raise HTTPException(400, "Maksimal durasi klip 10 menit")

    if len(req.items) > 12:
        raise HTTPException(400, "Maksimal 12 teks/efek per klip")
    sfx = sfx_files()
    items = []
    for n, it in enumerate(req.items, 1):
        try:
            at = to_seconds(it.at)
        except ValueError:
            raise HTTPException(400, f"Teks #{n}: format waktu muncul salah (pakai mm:ss)")
        if not 0 <= at < e - s:
            raise HTTPException(400, f"Teks #{n}: waktu muncul ({it.at}) di luar durasi klip "
                                     f"({e - s:.0f} detik). Waktu dihitung dari awal klip.")
        if it.sfx and it.sfx not in sfx:
            raise HTTPException(400, f"Teks #{n}: efek suara '{it.sfx}' tidak ditemukan")
        if not it.text.strip() and not it.sfx:
            continue
        items.append({"text": it.text, "at": at,
                      "dur": min(max(it.dur, 0.5), 60.0),
                      "pos": it.pos if it.pos in ("top", "center", "bottom") else "center",
                      "sfx": sfx.get(it.sfx) if it.sfx else None})

    if len(req.mutes) > 30:
        raise HTTPException(400, "Maksimal 30 mute per klip")
    mutes = []
    for n, m in enumerate(req.mutes, 1):
        try:
            at = to_seconds(m.at)
        except ValueError:
            raise HTTPException(400, f"Mute #{n}: format waktu salah (pakai mm:ss, boleh pecahan seperti 0:12.5)")
        if not 0 <= at < e - s:
            raise HTTPException(400, f"Mute #{n}: waktu ({m.at}) di luar durasi klip "
                                     f"({e - s:.0f} detik). Waktu dihitung dari awal klip.")
        d = min(max(m.dur, 0.1), 30.0, (e - s) - at)
        mutes.append({"at": at, "dur": d, "beep": bool(m.beep)})

    if req.sub_lang not in ("id", "en", "auto"):
        req.sub_lang = "id"
    if req.sub_sens not in ("normal", "max"):
        req.sub_sens = "normal"
    if req.sub_pos not in ("bottom", "center", "top"):
        req.sub_pos = "bottom"
    if req.sub_style not in ("word", "plain"):
        req.sub_style = "word"

    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "percent": 0, "stage": "Memulai",
                 "detail": "", "error": None, "result": None}
    threading.Thread(target=run_job, args=(jid, req, s, e, items, mutes), daemon=True).start()
    return {"job": jid}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    return job


# ---------------------------------------------------------------- daftar klip

@app.get("/api/clips")
def list_clips():
    return [json.loads(p.read_text()) for p in
            sorted(CLIPS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]


@app.delete("/api/clips/{cid}")
def delete_clip(cid: str):
    if not re.fullmatch(r"[0-9a-f]{10}", cid):
        raise HTTPException(400, "id tidak valid")
    for ext in ("mp4", "json"):
        (CLIPS / f"{cid}.{ext}").unlink(missing_ok=True)
    return {"ok": True}


app.mount("/clips", StaticFiles(directory=CLIPS), name="clips")
app.mount("/sfx", StaticFiles(directory=SFX_DIR), name="sfx")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
