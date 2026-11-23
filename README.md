# YT Clipper

Potong bagian dari video YouTube jadi file MP4 (landscape atau vertikal 9:16).
Pakai hanya untuk video milikmu atau yang berlisensi bebas / diizinkan pemiliknya.

## Install
1. Python 3.10+
2. Install ffmpeg (`sudo apt install ffmpeg` / `brew install ffmpeg` / winget install ffmpeg)
3. `pip install -r requirements.txt`

## Jalankan
    uvicorn main:app --reload
Buka http://localhost:8000

Hasil klip tersimpan di folder `clips/`.
Kalau yt-dlp error, update dulu: `pip install -U yt-dlp`
