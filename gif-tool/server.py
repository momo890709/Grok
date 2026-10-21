#!/usr/bin/env python3
"""本地视频转 GIF 小工具。

两种入口：
  1. 粘贴视频路径（可以多行，一行一个）
  2. 粘贴 .pptx 路径，自动列出里面嵌入/链接的所有视频，勾选后批量转 GIF

只依赖 Python 3 标准库和本机的 ffmpeg / ffprobe。
运行：python3 server.py   然后浏览器会自动打开 http://127.0.0.1:8765
"""

import argparse
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
CACHE_DIR = Path(tempfile.gettempdir()) / "gif-tool-cache"

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".mpg", ".mpeg", ".mkv",
    ".webm", ".3gp", ".flv", ".asf", ".ts", ".mts",
}
DITHERS = {"bayer", "sierra2_4a", "floyd_steinberg", "none"}

NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# id -> {"path": 源视频, "name": 展示名, "outdir": 默认输出目录, "stem": 输出文件名}
REGISTRY = {}
# id -> 生成的 gif 路径
OUTPUTS = {}
LOCK = threading.Lock()


# ───────────────────────── 工具函数 ─────────────────────────

def clean_path(raw):
    """处理从 Finder / 终端粘贴过来的各种奇怪路径写法。"""
    p = raw.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
        p = p[1:-1]
    if p.startswith("file://"):
        p = unquote(urlparse(p).path)
    p = re.sub(r"\\(.)", r"\1", p)  # 终端拖拽出来的 "视频\ 1.mp4"
    return Path(os.path.expanduser(p))


def register(path, name, outdir, stem, extra=None):
    key = hashlib.sha1(str(path).encode()).hexdigest()[:12]
    with LOCK:
        REGISTRY[key] = {"path": str(path), "name": name, "outdir": str(outdir), "stem": stem}
    info = probe_video(path)
    item = {"id": key, "name": name, "path": str(path), "size": path.stat().st_size, **info}
    if extra:
        item.update(extra)
    return item


def probe_video(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        return {
            "duration": float(data.get("format", {}).get("duration") or 0),
            "width": stream.get("width"),
            "height": stream.get("height"),
        }
    except Exception:
        return {"duration": 0, "width": None, "height": None}


def safe_stem(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip(" _") or "video"


# ───────────────────────── PPTX 解析 ─────────────────────────

def _rels(zf, part):
    """读取某个 part 对应的 .rels，返回 [(rId, type, target, external)]。"""
    d, f = posixpath.split(part)
    rels_name = posixpath.join(d, "_rels", f + ".rels")
    if rels_name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(rels_name))
    out = []
    for r in root.findall(f"{{{NS_REL}}}Relationship"):
        external = r.get("TargetMode") == "External"
        target = r.get("Target", "")
        if not external:
            target = posixpath.normpath(posixpath.join(d, target)).lstrip("/")
        out.append((r.get("Id"), r.get("Type", ""), target, external))
    return out


def scan_pptx(pptx_path):
    pptx_path = pptx_path.resolve()
    with zipfile.ZipFile(pptx_path) as zf:
        names = set(zf.namelist())

        # 按真实放映顺序给幻灯片编号（slideN.xml 的 N 不一定是顺序）
        slide_order = {}
        pres = "ppt/presentation.xml"
        if pres in names:
            rid_to_target = {rid: t for rid, _, t, _ in _rels(zf, pres)}
            root = ET.fromstring(zf.read(pres))
            lst = root.find(f"{{{NS_P}}}sldIdLst")
            for i, sld in enumerate(lst if lst is not None else [], 1):
                t = rid_to_target.get(sld.get(f"{{{NS_R}}}id"))
                if t:
                    slide_order[t] = i

        # 视频 -> 出现在哪些页
        media_slides = {}
        external = {}
        for part in names:
            if not re.fullmatch(r"ppt/slides/slide\d+\.xml", part):
                continue
            idx = slide_order.get(part) or int(re.search(r"(\d+)", part.rsplit("/", 1)[1]).group(1))
            for _, rtype, target, is_ext in _rels(zf, part):
                is_video_rel = rtype.endswith("/video") or rtype.endswith("/media")
                if is_ext:
                    if is_video_rel:
                        external.setdefault(target, set()).add(idx)
                elif Path(target).suffix.lower() in VIDEO_EXTS or (is_video_rel and target.startswith("ppt/media/")):
                    media_slides.setdefault(target, set()).add(idx)

        # 没被任何页引用、但躺在 media 里的视频也列出来
        for n in names:
            if n.startswith("ppt/media/") and Path(n).suffix.lower() in VIDEO_EXTS:
                media_slides.setdefault(n, set())

        digest = hashlib.sha1(f"{pptx_path}:{pptx_path.stat().st_mtime}".encode()).hexdigest()[:10]
        extract_dir = CACHE_DIR / f"{safe_stem(pptx_path.stem)}_{digest}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        outdir = pptx_path.parent / f"{pptx_path.stem}_gifs"

        items = []
        for member in sorted(media_slides, key=lambda m: (min(media_slides[m] or {9999}), m)):
            dest = extract_dir / posixpath.basename(member)
            if not dest.exists():
                with zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            slides = sorted(media_slides[member])
            prefix = f"第{slides[0]:02d}页_" if slides else "未引用_"
            items.append(register(
                dest, posixpath.basename(member), outdir, prefix + Path(member).stem,
                {"slides": slides, "source": "embedded"},
            ))

        for target, slides in sorted(external.items(), key=lambda kv: min(kv[1])):
            p = clean_path(target)
            if not p.is_absolute():
                p = (pptx_path.parent / p).resolve()
            if p.exists() and p.suffix.lower() in VIDEO_EXTS:
                slides = sorted(slides)
                items.append(register(
                    p, p.name, outdir, f"第{slides[0]:02d}页_{p.stem}",
                    {"slides": slides, "source": "linked"},
                ))
    return items, str(outdir)


# ───────────────────────── ffmpeg ─────────────────────────

def build_filter(opts):
    parts = []
    speed = float(opts.get("speed") or 1)
    if abs(speed - 1) > 1e-6:
        parts.append(f"setpts=PTS/{speed}")
    parts.append(f"fps={float(opts.get('fps') or 6):g}")
    width = int(opts.get("width") or 0)
    if width > 0:
        parts.append(f"scale={width}:-1:flags=lanczos")
    colors = max(2, min(256, int(opts.get("colors") or 128)))
    dither = opts.get("dither") if opts.get("dither") in DITHERS else "bayer"
    use = f"paletteuse=dither={dither}"
    if dither == "bayer":
        use += ":bayer_scale=3"
    return (",".join(parts)
            + f",split[a][b];[a]palettegen=max_colors={colors}:stats_mode=diff[p];[b][p]{use}")


def convert(item_id, opts):
    with LOCK:
        meta = REGISTRY.get(item_id)
    if not meta:
        raise ValueError("找不到这个视频，刷新列表再试一次")

    outdir = Path(os.path.expanduser(opts.get("outdir") or meta["outdir"]))
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{safe_stem(meta['stem'])}.gif"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    start = float(opts.get("start") or 0)
    end = float(opts.get("end") or 0)
    if start > 0:
        cmd += ["-ss", f"{start:g}"]
    if end > start:
        cmd += ["-t", f"{end - start:g}"]
    cmd += ["-i", meta["path"], "-vf", build_filter(opts), "-loop", "0", str(out)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(proc.stderr.strip()[-800:] or "ffmpeg 转换失败")
    with LOCK:
        OUTPUTS[item_id] = str(out)
    return {"output": str(out), "size": out.stat().st_size}


# ───────────────────────── HTTP ─────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "gif-tool"

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + fmt % args + "\n")

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype=None):
        """支持 Range，Safari 的 <video> 没有 Range 不肯播。"""
        path = Path(path)
        if not path.exists():
            return self._json({"error": "file not found"}, 404)
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and size:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = max(0, end - start + 1)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            return self._file(HERE / "index.html", "text/html; charset=utf-8")
        if url.path == "/api/health":
            return self._json({
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "ffprobe": bool(shutil.which("ffprobe")),
                "platform": sys.platform,
            })
        if url.path == "/media":
            meta = REGISTRY.get((q.get("id") or [""])[0])
            return self._file(meta["path"]) if meta else self._json({"error": "unknown id"}, 404)
        if url.path == "/gif":
            out = OUTPUTS.get((q.get("id") or [""])[0])
            return self._file(out, "image/gif") if out else self._json({"error": "not converted"}, 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)

        path = urlparse(self.path).path
        try:
            if path == "/api/videos":
                items, errors = [], []
                for line in str(body.get("paths", "")).splitlines():
                    if not line.strip():
                        continue
                    p = clean_path(line)
                    if not p.exists():
                        errors.append(f"不存在：{p}")
                    elif p.suffix.lower() == ".pptx":
                        errors.append(f"这是 PPT，请切到 PPT 页签：{p.name}")
                    else:
                        p = p.resolve()
                        items.append(register(p, p.name, p.parent, p.stem))
                return self._json({"items": items, "errors": errors})

            if path == "/api/pptx":
                p = clean_path(str(body.get("path", "")))
                if not p.exists():
                    return self._json({"error": f"找不到文件：{p}"}, 400)
                if p.suffix.lower() == ".ppt":
                    return self._json({"error": "老格式 .ppt 读不了，用 PowerPoint/Keynote 另存为 .pptx 再来"}, 400)
                if not zipfile.is_zipfile(p):
                    return self._json({"error": "这不是有效的 .pptx 文件"}, 400)
                items, outdir = scan_pptx(p)
                return self._json({"items": items, "outdir": outdir})

            if path == "/api/convert":
                return self._json(convert(body.get("id"), body.get("options") or {}))

            if path == "/api/reveal":
                target = OUTPUTS.get(body.get("id")) or body.get("path")
                if target and Path(target).exists():
                    if sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", target])
                    elif sys.platform.startswith("win"):
                        subprocess.Popen(["explorer", "/select,", target])
                    else:
                        subprocess.Popen(["xdg-open", str(Path(target).parent)])
                    return self._json({"ok": True})
                return self._json({"error": "文件不存在"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="本地视频 / PPT 视频转 GIF")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("⚠️  没找到 ffmpeg。Mac 上先跑：brew install ffmpeg")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"GIF 工具已启动：{url}   (Ctrl+C 退出)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n拜拜")


if __name__ == "__main__":
    main()
