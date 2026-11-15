# -*- coding: utf-8 -*-
import os
import sys
import csv
import time
import shutil
import subprocess
import threading
import queue
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageOps

# ---------------------------
# Optional drag & drop support
# ---------------------------
DND_AVAILABLE = False
try:
    # pip install tkinterdnd2
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

# Prefix added to the output folder name and to the name of every file that
# was actually compressed (copied files keep their original name).
COMPRESSED_PREFIX = "压缩_"

APP_VERSION = "2.0.1"


@dataclass
class Preset:
    name: str
    max_side: int
    jpg_quality: int
    webp_quality: int


PRESETS = [
    Preset("PPT 最稳（推荐） 1600 + Q78", 1600, 78, 80),
    Preset("更清晰（大厅屏/投影） 1920 + Q80", 1920, 80, 82),
    Preset("更小（文件很大/电脑较老） 1280 + Q75", 1280, 75, 76),
    Preset("论文/高保真 3000 + Q88", 3000, 88, 88),
    Preset("自定义", 1600, 78, 80),
]


def output_suffix_for(suf: str) -> str:
    """The suffix of the file we actually write for a given input suffix.

    This is the single source of truth: the worker uses it to predict the
    output path (for incremental checks) and compress_image uses it to write.
    BMP is converted to JPEG because the BMP format itself cannot compress.
    """
    if suf in (".jpg", ".jpeg", ".bmp"):
        return ".jpg"
    return suf


def suggest_output_dir(src: Path) -> Path:
    """The parallel folder used when no output folder is chosen.

    It sits next to the source folder and is named after it with a prefix:
    ".../photos" becomes ".../压缩_photos".
    """
    try:
        resolved = src.resolve()
    except Exception:
        resolved = src
    parent = resolved.parent
    if parent == resolved or str(parent) in ("", "."):
        # Source is a filesystem root: there is no sibling to create.
        return resolved / f"{COMPRESSED_PREFIX}输出"
    return parent / f"{COMPRESSED_PREFIX}{resolved.name}"


def plan_output_paths(
    files: list[Path],
    src: Path,
    dst: Path,
    rename_compressed: bool = True,
) -> dict[Path, Path]:
    """Map each source file to the output path it will be written to.

    Compressed images get a "压缩_" prefix on their name so they can be told
    apart from the files that were merely copied; copied files keep their
    original name.

    Converting a format (.bmp -> .jpg) can also make two different sources
    target the same output name ("photo.jpg" and "photo.bmp" both want
    "压缩_photo.jpg"). The source that already uses that extension keeps the
    plain name; the other one gets its original extension appended
    ("压缩_photo_bmp.jpg").
    """
    planned: dict[Path, Path] = {}
    taken: set[Path] = set()

    def target_name(f: Path) -> Path:
        """The preferred output name for f, before collision resolution."""
        rel = f.relative_to(src)
        suf = f.suffix.lower()
        if suf not in IMG_EXTS:
            return rel
        stem = f"{COMPRESSED_PREFIX}{rel.stem}" if rename_compressed else rel.stem
        return rel.with_name(f"{stem}{output_suffix_for(suf)}")

    images = [f for f in files if f.suffix.lower() in IMG_EXTS]
    others = [f for f in files if f.suffix.lower() not in IMG_EXTS]

    # Files that are only copied keep their original name.
    for f in others:
        out = dst / f.relative_to(src)
        planned[f] = out
        taken.add(out)

    # Images keep their own extension first; converted ones then take a
    # disambiguated name so two sources never write to the same file.
    def is_native(f: Path) -> bool:
        suf = f.suffix.lower()
        return output_suffix_for(suf) == suf

    native = sorted((f for f in images if is_native(f)), key=lambda p: str(p))
    converted = sorted((f for f in images if not is_native(f)), key=lambda p: str(p))

    for f in native:
        out = dst / target_name(f)
        planned[f] = out
        taken.add(out)

    for f in converted:
        out = dst / target_name(f)
        if out in taken:
            stem = out.stem
            tag = f.suffix.lower().lstrip(".")
            cand = out.with_name(f"{stem}_{tag}{out.suffix}")
            n = 2
            while cand in taken:
                cand = out.with_name(f"{stem}_{tag}{n}{out.suffix}")
                n += 1
            out = cand
        planned[f] = out
        taken.add(out)

    return planned


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n/1024:.1f} KB"
    if n < 1024**3:
        return f"{n/1024/1024:.2f} MB"
    return f"{n/1024/1024/1024:.2f} GB"


def open_in_file_manager(path: Path) -> None:
    """Open a directory in the OS file manager (Windows Explorer / macOS Finder / Linux file manager)."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def resize_keep_aspect(img: Image.Image, max_side: int) -> Image.Image:
    if max_side <= 0:
        return img
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    scale = max_side / float(longest)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return img.resize((new_w, new_h), resample=Image.LANCZOS)


def extract_exif(img: Image.Image) -> bytes | None:
    """Return the EXIF payload of an image as bytes, or None if it has none.

    Orientation is dropped (the pixels are already transposed) and the pixel
    dimension tags are refreshed so they describe the resized output.
    """
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None

    # 274 = Orientation: pixels are transposed, so the tag would make viewers
    # rotate the image a second time.
    exif.pop(274, None)
    # 40962/40963 = PixelXDimension/PixelYDimension
    exif[40962] = img.size[0]
    exif[40963] = img.size[1]

    try:
        data = exif.tobytes()
    except Exception:
        return None
    return data or None


def save_jpeg(img: Image.Image, out_path: Path, quality: int, exif: bytes | None = None):
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    kw = {"quality": quality, "optimize": True, "progressive": True}
    if exif:
        kw["exif"] = exif
    img.save(out_path, format="JPEG", **kw)


def save_png(img: Image.Image, out_path: Path, exif: bytes | None = None):
    # For photos-to-PPT: optimize only (no quantize to avoid banding risk)
    kw = {"optimize": True, "compress_level": 9}
    if exif:
        kw["exif"] = exif
    img.save(out_path, format="PNG", **kw)


def save_webp(img: Image.Image, out_path: Path, quality: int, exif: bytes | None = None):
    kw = {"quality": quality, "method": 6}
    if exif:
        kw["exif"] = exif
    img.save(out_path, format="WEBP", **kw)


def save_gif(img: Image.Image, out_path: Path):
    """Save a GIF. Animated sources keep their frames and timing."""
    if img.mode not in ("P", "L"):
        img = img.convert("RGBA").convert("P", palette=Image.ADAPTIVE)
    img.save(out_path, format="GIF", optimize=True)


def save_tiff(img: Image.Image, out_path: Path, exif: bytes | None = None):
    """Save a TIFF with deflate compression.

    Pillow's TIFF writer cannot serialise the GPS sub-IFD: it writes a bogus
    GPSIFDOffset and the save fails (libtiff also prints a warning straight
    to stderr). So the GPS tag is dropped up front rather than triggering a
    failed write; the rest of the EXIF is kept.
    """
    kw = {"compression": "tiff_deflate"}
    if exif:
        try:
            trimmed = Image.Exif()
            trimmed.load(exif)
            if 34853 in trimmed:  # GPSInfo - not writable to TIFF by Pillow
                trimmed.pop(34853, None)
                trimmed.pop(34665, None)  # Exif IFD
            img.save(out_path, format="TIFF", exif=trimmed.tobytes(), **kw)
            return
        except Exception:
            pass
    img.save(out_path, format="TIFF", **kw)


def compress_animated_gif(in_path: Path, out_path: Path, max_side: int) -> Path:
    """Resize every frame of an animated GIF and write it back."""
    with Image.open(in_path) as im:
        frames = []
        durations = []
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            durations.append(im.info.get("duration", 100))
            frame = im.convert("RGBA")
            frame = resize_keep_aspect(frame, max_side)
            frames.append(frame)

        outp = out_path.with_suffix(".gif")
        frames[0].save(
            outp,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=im.info.get("loop", 0),
            disposal=2,
            optimize=True,
        )
        return outp






def compress_image(
    in_path: Path,
    out_path: Path,
    max_side: int,
    jpg_quality: int,
    webp_quality: int = 80,
) -> Path:
    """Compress one image to out_path.

    out_path is the final destination (its suffix decides the output format);
    the caller resolves it via plan_output_paths so that format conversions
    cannot make two sources overwrite each other.
    """
    out_suf = out_path.suffix.lower()

    with Image.open(in_path) as im:
        # Animated GIF: resize frame by frame, keep animation and timing.
        if out_suf == ".gif" and getattr(im, "n_frames", 1) > 1:
            return compress_animated_gif(in_path, out_path, max_side)

        im = ImageOps.exif_transpose(im)
        im = resize_keep_aspect(im, max_side)
        # getexif() still works after a resize, and Orientation was already
        # dropped by exif_transpose; extract_exif() refreshes the size tags.
        exif = extract_exif(im)

        if out_suf == ".jpg":
            save_jpeg(im, out_path, jpg_quality, exif)
            return out_path

        if out_suf == ".png":
            save_png(im, out_path, exif)
            return out_path

        if out_suf == ".webp":
            save_webp(im, out_path, webp_quality, exif)
            return out_path

        if out_suf == ".gif":
            save_gif(im, out_path)
            return out_path

        if out_suf in (".tif", ".tiff"):
            save_tiff(im, out_path, exif)
            return out_path

    return out_path


def iter_all_files(src: Path, recursive: bool, exclude: list[Path] | None = None):
    """Yield files under src. Files inside any excluded dir (e.g. the output
    dir when it is nested inside src) are skipped to avoid self-ingestion."""
    excluded: list[Path] = []
    for e in (exclude or []):
        try:
            excluded.append(e.resolve())
        except Exception:
            pass

    def is_excluded(p: Path) -> bool:
        if not excluded:
            return False
        try:
            rp = p.resolve()
        except Exception:
            return False
        return any(rp == ex or ex in rp.parents for ex in excluded)

    if recursive:
        for p in src.rglob("*"):
            if p.is_file() and not is_excluded(p):
                yield p
    else:
        for p in src.iterdir():
            if p.is_file() and not is_excluded(p):
                yield p


def should_skip_incremental(src_file: Path, dst_file: Path) -> bool:
    """
    Incremental rule:
    - if dst exists and dst mtime >= src mtime and dst size > 0 => skip
    """
    if not dst_file.exists():
        return False
    try:
        if dst_file.stat().st_size <= 0:
            return False
        return dst_file.stat().st_mtime >= src_file.stat().st_mtime
    except Exception:
        return False


def decide_action(src_file: Path, dst_file: Path, incremental: bool, overwrite: bool) -> tuple[str, str]:
    """
    Decide what to do with a file whose destination may already exist.

    Returns (action, reason) where action is "process" or "skip".

    Semantics:
      - overwrite on            -> always redo the file (force), ignore incremental
      - overwrite off, incr on  -> keep the output in sync with the source:
                                   redo only files whose source is newer
      - overwrite off, incr off -> never touch an existing output
    """
    if not dst_file.exists():
        return "process", "new"

    if overwrite:
        return "process", "overwrite(existing)"

    if incremental:
        if should_skip_incremental(src_file, dst_file):
            return "skip", "skip(incremental: 已是最新)"
        # Source is newer (or output is empty) -> refresh it.
        return "process", "refresh(源文件已更新)"

    return "skip", "skip(exists, 未允许覆盖)"


class BaseApp:
    pass


if DND_AVAILABLE:
    class Root(TkinterDnD.Tk):  # type: ignore
        pass
else:
    class Root(tk.Tk):  # fallback
        pass


class App(Root, BaseApp):
    def __init__(self):
        super().__init__()
        self.title(f"文件夹批量压缩（图片压缩 + 非图片复制） v{APP_VERSION}")
        self.geometry("980x640")
        self.minsize(920, 600)

        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_flag = threading.Event()

        # Vars
        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()

        self.recursive_var = tk.BooleanVar(value=True)
        self.process_images_var = tk.BooleanVar(value=True)
        self.copy_others_var = tk.BooleanVar(value=True)

        self.incremental_var = tk.BooleanVar(value=True)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.rename_var = tk.BooleanVar(value=True)

        self.preset_var = tk.StringVar(value=PRESETS[0].name)
        self.max_side_var = tk.IntVar(value=PRESETS[0].max_side)
        self.jpg_quality_var = tk.IntVar(value=PRESETS[0].jpg_quality)
        self.webp_quality_var = tk.IntVar(value=PRESETS[0].webp_quality)

        self.progress_var = tk.DoubleVar(value=0)
        self.status_var = tk.StringVar(value="就绪")
        self.current_file_var = tk.StringVar(value="")

        # Stats
        self.total_count = 0
        self.done_count = 0

        self._build_ui()
        self._poll()

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # Drop hint (optional)
        hint = ttk.Frame(self)
        hint.pack(fill="x", **pad)
        msg = "提示：可拖拽文件夹到输入框" if DND_AVAILABLE else "提示：拖拽功能未启用（可选装：pip install tkinterdnd2）"
        ttk.Label(hint, text=msg).pack(side="left")

        # Paths
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        r1 = ttk.Frame(top); r1.pack(fill="x", pady=4)
        ttk.Label(r1, text="输入目录：", width=10).pack(side="left")
        self.src_entry = ttk.Entry(r1, textvariable=self.src_var)
        self.src_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r1, text="选择…", command=self.pick_src).pack(side="left")

        r2 = ttk.Frame(top); r2.pack(fill="x", pady=4)
        ttk.Label(r2, text="输出目录：", width=10).pack(side="left")
        self.dst_entry = ttk.Entry(r2, textvariable=self.dst_var)
        self.dst_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r2, text="选择…", command=self.pick_dst).pack(side="left")

        # Live hint: where the output will land when no folder is chosen.
        self.auto_hint_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.auto_hint_var, foreground="#666").pack(
            side="left", padx=10, pady=(0, 4))

        # Drag & drop bindings
        if DND_AVAILABLE:
            for ent, setter in [(self.src_entry, self.src_var), (self.dst_entry, self.dst_var)]:
                ent.drop_target_register(DND_FILES)
                ent.dnd_bind("<<Drop>>", lambda e, s=setter: self._on_drop(e, s))

        # Settings
        opt = ttk.LabelFrame(self, text="处理与压缩设置")
        opt.pack(fill="x", padx=10, pady=6)

        rowA = ttk.Frame(opt); rowA.pack(fill="x", padx=10, pady=6)
        ttk.Checkbutton(rowA, text="递归子文件夹", variable=self.recursive_var).pack(side="left")
        ttk.Checkbutton(rowA, text="处理图片（压缩/缩放）", variable=self.process_images_var).pack(side="left", padx=12)
        ttk.Checkbutton(rowA, text="复制非图片文件", variable=self.copy_others_var).pack(side="left", padx=12)

        rowB = ttk.Frame(opt); rowB.pack(fill="x", padx=10, pady=6)
        ttk.Checkbutton(rowB, text="增量模式（只重处理已修改的文件）", variable=self.incremental_var).pack(side="left")
        ttk.Checkbutton(rowB, text="强制覆盖（忽略增量，全部重做）", variable=self.overwrite_var).pack(side="left", padx=12)

        rowB2 = ttk.Frame(opt); rowB2.pack(fill="x", padx=10, pady=6)
        ttk.Checkbutton(rowB2, text="压缩文件加“压缩_”前缀（复制文件保持原名）",
                        variable=self.rename_var).pack(side="left")

        rowC = ttk.Frame(opt); rowC.pack(fill="x", padx=10, pady=6)
        ttk.Label(rowC, text="规格：").pack(side="left")
        preset_box = ttk.Combobox(rowC, textvariable=self.preset_var,
                                  values=[p.name for p in PRESETS], state="readonly", width=30)
        preset_box.pack(side="left", padx=6)
        preset_box.bind("<<ComboboxSelected>>", self.on_preset)

        rowD = ttk.Frame(opt); rowD.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(rowD, text="最长边(px)：").pack(side="left")
        self.max_side_entry = ttk.Entry(rowD, textvariable=self.max_side_var, width=8)
        self.max_side_entry.pack(side="left", padx=6)

        ttk.Label(rowD, text="JPG质量(1-95)：").pack(side="left", padx=12)
        self.jpg_quality_entry = ttk.Entry(rowD, textvariable=self.jpg_quality_var, width=8)
        self.jpg_quality_entry.pack(side="left", padx=6)

        ttk.Label(rowD, text="WEBP质量(1-95)：").pack(side="left", padx=12)
        self.webp_quality_entry = ttk.Entry(rowD, textvariable=self.webp_quality_var, width=8)
        self.webp_quality_entry.pack(side="left", padx=6)

        # Controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=10, pady=6)

        self.start_btn = ttk.Button(ctrl, text="开始", command=self.start)
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(ctrl, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        self.open_btn = ttk.Button(ctrl, text="打开输出目录", command=self.open_output, state="disabled")
        self.open_btn.pack(side="left", padx=8)

        # Progress + status
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=4)

        self.pbar = ttk.Progressbar(bar, variable=self.progress_var, maximum=100)
        self.pbar.pack(side="left", fill="x", expand=True, padx=(0, 10))

        ttk.Label(bar, textvariable=self.status_var, width=20).pack(side="right")

        cur = ttk.Frame(self)
        cur.pack(fill="x", padx=10, pady=2)
        ttk.Label(cur, text="当前：", width=6).pack(side="left")
        ttk.Label(cur, textvariable=self.current_file_var).pack(side="left", fill="x", expand=True)

        # Log
        logf = ttk.LabelFrame(self, text="日志（含 report.csv 输出到目标目录）")
        logf.pack(fill="both", expand=True, padx=10, pady=6)

        self.log = tk.Text(logf, wrap="none")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

        self.on_preset()

        # Keep the "where will the output go" hint in sync with the fields.
        self.src_var.trace_add("write", self._update_auto_hint)
        self.dst_var.trace_add("write", self._update_auto_hint)
        self._update_auto_hint()

    def _on_drop(self, event, target_var: tk.StringVar):
        # event.data format differs by platform:
        #   Windows: {C:\Path With Space}
        #   macOS/Linux: /path/with space  (brace-quoted too on some builds)
        # Use Tk's own list splitter, which understands the brace convention.
        raw = (event.data or "").strip()
        if not raw:
            return

        paths: list[str] = []
        try:
            paths = [p for p in self.tk.splitlist(raw) if p]
        except Exception:
            paths = []

        # Candidate list: the whole raw string (brace-stripped) first, since an
        # unbraced macOS path containing spaces would be split apart by splitlist.
        stripped = raw[1:-1] if (raw.startswith("{") and raw.endswith("}")) else raw
        candidates = [stripped] + [p for p in paths if p != stripped]

        # Prefer the first candidate that is an existing directory
        for p in candidates:
            if Path(p).is_dir():
                target_var.set(p)
                return
        target_var.set(candidates[0] if candidates else stripped)

    def pick_src(self):
        d = filedialog.askdirectory(title="选择输入目录")
        if d:
            self.src_var.set(d)

    def pick_dst(self):
        d = filedialog.askdirectory(title="选择输出目录（建议为空目录）")
        if d:
            self.dst_var.set(d)

    def _update_auto_hint(self, *_args):
        """Show where the output will go when no folder has been chosen."""
        dst = self.dst_var.get().strip()
        if dst:
            self.auto_hint_var.set("")
            return
        src = self.src_var.get().strip()
        if not src:
            self.auto_hint_var.set("未选择输出目录时，将自动在输入目录旁创建“压缩_<目录名>”")
            return
        try:
            self.auto_hint_var.set(f"未选择输出目录 → 将输出到：{suggest_output_dir(Path(src))}")
        except Exception:
            self.auto_hint_var.set("")

    def on_preset(self, _evt=None):
        name = self.preset_var.get()
        preset = next((p for p in PRESETS if p.name == name), PRESETS[0])
        if preset.name != "自定义":
            self.max_side_var.set(preset.max_side)
            self.jpg_quality_var.set(preset.jpg_quality)
            self.webp_quality_var.set(preset.webp_quality)
            self.max_side_entry.configure(state="disabled")
            self.jpg_quality_entry.configure(state="disabled")
            self.webp_quality_entry.configure(state="disabled")
        else:
            self.max_side_entry.configure(state="normal")
            self.jpg_quality_entry.configure(state="normal")
            self.webp_quality_entry.configure(state="normal")

    # ---------- Messaging ----------
    def ui_log(self, s: str):
        self.q.put(("log", s))

    def ui_status(self, s: str):
        self.q.put(("status", s))

    def ui_current(self, s: str):
        self.q.put(("current", s))

    def ui_progress(self, done: int, total: int):
        self.q.put(("progress", (done, total)))

    def ui_done(self, payload: dict):
        self.q.put(("done", payload))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", str(payload) + "\n")
                    self.log.see("end")
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "current":
                    self.current_file_var.set(str(payload))
                elif kind == "progress":
                    done, total = payload
                    pct = (done * 100.0 / total) if total else 0.0
                    self.progress_var.set(pct)
                    self.status_var.set(f"{done}/{total}")
                elif kind == "done":
                    self._on_worker_done(payload)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    # ---------- Validation ----------
    def validate(self) -> bool:
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()

        if not src or not Path(src).is_dir():
            messagebox.showerror("错误", "请输入/选择正确的输入目录")
            return False
        if not dst:
            # No output chosen: use a parallel "压缩_<name>" folder next to
            # the source folder.
            dst = str(suggest_output_dir(Path(src)))

        # Output must not be the input dir itself: files would overwrite their
        # own sources mid-run.
        try:
            if Path(dst).resolve() == Path(src).resolve():
                messagebox.showerror("错误", "输出目录不能与输入目录相同")
                return False
        except Exception:
            pass

        if not (self.process_images_var.get() or self.copy_others_var.get()):
            messagebox.showerror("错误", "至少勾选一个：处理图片 或 复制非图片")
            return False

        try:
            max_side = int(self.max_side_var.get())
            jpg_q = int(self.jpg_quality_var.get())
            webp_q = int(self.webp_quality_var.get())
        except Exception:
            messagebox.showerror("错误", "最长边和质量必须是整数")
            return False

        if max_side < 0:
            messagebox.showerror("错误", "最长边不能为负数（0 表示不缩放）")
            return False
        if not (1 <= jpg_q <= 95):
            messagebox.showerror("错误", "JPG质量范围建议 1-95")
            return False
        if not (1 <= webp_q <= 95):
            messagebox.showerror("错误", "WEBP质量范围建议 1-95")
            return False

        return True

    # ---------- Controls ----------
    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if not self.validate():
            return

        self.stop_flag.clear()
        self.progress_var.set(0)
        self.status_var.set("0/0")
        self.current_file_var.set("")
        self.log.delete("1.0", "end")
        self.open_btn.configure(state="disabled")

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        self.worker_thread = threading.Thread(target=self.worker, daemon=True)
        self.worker_thread.start()

    def stop(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_flag.set()
            self.ui_log("收到停止请求…将尽快停止。")

    def open_output(self):
        dst = self.dst_var.get().strip()
        if not dst:
            # Same fallback as the worker: the auto-generated parallel folder.
            src = self.src_var.get().strip()
            if not src:
                return
            dst = str(suggest_output_dir(Path(src)))
        if dst and Path(dst).exists():
            try:
                open_in_file_manager(Path(dst))
            except Exception as e:
                messagebox.showerror("错误", f"无法打开目录：{e}")

    # ---------- Worker ----------
    def worker(self):
        t0 = time.time()

        src = Path(self.src_var.get().strip())
        dst_text = self.dst_var.get().strip()
        # No output folder chosen -> parallel "压缩_<name>" folder beside src.
        dst = Path(dst_text) if dst_text else suggest_output_dir(src)
        recursive = bool(self.recursive_var.get())

        do_img = bool(self.process_images_var.get())
        do_copy = bool(self.copy_others_var.get())

        incremental = bool(self.incremental_var.get())
        overwrite = bool(self.overwrite_var.get())

        max_side = int(self.max_side_var.get())
        jpg_q = int(self.jpg_quality_var.get())
        webp_q = int(self.webp_quality_var.get())

        ensure_dir(dst)
        report_path = dst / "report.csv"

        # If the output dir sits inside the input dir, exclude it from the
        # scan; otherwise each run would copy its own output into itself.
        exclude: list[Path] = []
        try:
            src_r, dst_r = src.resolve(), dst.resolve()
            if dst_r != src_r and src_r in dst_r.parents:
                exclude.append(dst_r)
        except Exception:
            pass

        files = list(iter_all_files(src, recursive, exclude))
        total = len(files)
        # Resolve every output path up front so that format conversions
        # (.bmp -> .jpg) cannot make two sources write to the same file.
        rename = bool(self.rename_var.get())
        planned = plan_output_paths(files, src, dst, rename_compressed=rename)

        self.ui_log(f"开始处理：{src}")
        if dst_text:
            self.ui_log(f"输出目录：{dst}")
        else:
            self.ui_log(f"输出目录（自动生成）：{dst}")
        self.ui_log(f"递归：{'是' if recursive else '否'} | 处理图片：{'是' if do_img else '否'} | 复制非图片：{'是' if do_copy else '否'}")
        self.ui_log(f"增量：{'是' if incremental else '否'} | 覆盖：{'是' if overwrite else '否'} | 压缩文件加前缀：{'是' if rename else '否'}")
        self.ui_log(f"参数：最长边={max_side} | JPG质量={jpg_q} | WEBP质量={webp_q}")
        self.ui_log(f"待处理文件：{total}")
        if exclude:
            self.ui_log(f"已排除输出目录（位于输入目录内部）：{exclude[0]}")
        self.ui_log(f"report.csv 将输出到：{report_path}")

        # Stats
        done = 0
        img_ok = 0
        copy_ok = 0
        skipped = 0
        errors = 0

        in_bytes = 0
        out_bytes = 0

        # Write report
        with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["relative_path", "output_path", "type", "action",
                        "input_bytes", "output_bytes", "message"])

            for in_path in files:
                if self.stop_flag.is_set():
                    self.ui_log("已停止。")
                    break

                done += 1
                rel = in_path.relative_to(src)
                self.ui_current(str(rel))
                self.ui_progress(done, total)

                in_size = 0
                out_size = 0
                try:
                    in_size = in_path.stat().st_size
                    in_bytes += in_size

                    suf = in_path.suffix.lower()
                    is_img = suf in IMG_EXTS

                    out_path = dst / rel
                    ensure_dir(out_path.parent)

                    if is_img and do_img:
                        # The planned output path (which resolves format
                        # conversions and name collisions) is the real target.
                        expected_out = planned.get(in_path) or out_path.with_suffix(output_suffix_for(suf))

                        action, reason = decide_action(in_path, expected_out, incremental, overwrite)
                        if action == "skip":
                            skipped += 1
                            out_size = expected_out.stat().st_size if expected_out.exists() else 0
                            out_bytes += out_size
                            self.ui_log(f"[SKIP][IMG] {rel} ({reason})")
                            w.writerow([str(rel), str(expected_out.relative_to(dst)), "image", "skip",
                                        in_size, out_size, reason])
                            continue

                        ensure_dir(expected_out.parent)
                        real_out = compress_image(in_path, expected_out, max_side=max_side,
                                                  jpg_quality=jpg_q, webp_quality=webp_q)
                        out_size = real_out.stat().st_size
                        out_bytes += out_size
                        img_ok += 1
                        note = "" if real_out.name == in_path.name else f", 输出为 {real_out.name}"
                        self.ui_log(f"[IMG] {rel}  {in_size/1024:.1f}KB -> {out_size/1024:.1f}KB ({reason}{note})")
                        w.writerow([str(rel), str(real_out.relative_to(dst)), "image", "compress",
                                    in_size, out_size, reason])

                    elif (not is_img) and do_copy:
                        # Copy other files
                        action, reason = decide_action(in_path, out_path, incremental, overwrite)
                        if action == "skip":
                            skipped += 1
                            out_size = out_path.stat().st_size if out_path.exists() else 0
                            out_bytes += out_size
                            self.ui_log(f"[SKIP][CPY] {rel} ({reason})")
                            w.writerow([str(rel), str(out_path.relative_to(dst)), "other", "skip",
                                        in_size, out_size, reason])
                            continue

                        shutil.copy2(in_path, out_path)
                        out_size = out_path.stat().st_size
                        out_bytes += out_size
                        copy_ok += 1
                        self.ui_log(f"[CPY] {rel}  {in_size/1024:.1f}KB ({reason})")
                        w.writerow([str(rel), str(out_path.relative_to(dst)), "other", "copy",
                                    in_size, out_size, reason])

                    else:
                        # Either skipped due to options
                        skipped += 1
                        kind = "image" if is_img else "other"
                        self.ui_log(f"[SKIP][{kind.upper()}] {rel} (按设置不处理)")
                        w.writerow([str(rel), "", kind, "skip", in_size, 0, "disabled by option"])

                except Exception as e:
                    errors += 1
                    self.ui_log(f"[ERR] {rel}  {e}")
                    w.writerow([str(rel), "", "unknown", "error", in_size, out_size, str(e)])

        t1 = time.time()
        spent = t1 - t0

        saved_bytes = max(0, in_bytes - out_bytes)
        ratio = (out_bytes / in_bytes * 100.0) if in_bytes else 0.0

        self.ui_done({
            "total": total,
            "done": done,
            "img_ok": img_ok,
            "copy_ok": copy_ok,
            "skipped": skipped,
            "errors": errors,
            "in_bytes": in_bytes,
            "out_bytes": out_bytes,
            "saved_bytes": saved_bytes,
            "ratio": ratio,
            "spent": spent,
            "report": str(report_path),
        })

    def _on_worker_done(self, st: dict):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.open_btn.configure(state="normal")

        self.ui_current("")
        self.progress_var.set(100 if st.get("total", 0) else 0)

        self.log.insert("end", "\n=== Summary ===\n")
        self.log.insert("end", f"Total files:     {st['total']}\n")
        self.log.insert("end", f"Processed:       {st['done']}\n")
        self.log.insert("end", f"Images compressed: {st['img_ok']}\n")
        self.log.insert("end", f"Others copied:   {st['copy_ok']}\n")
        self.log.insert("end", f"Skipped:         {st['skipped']}\n")
        self.log.insert("end", f"Errors:          {st['errors']}\n")
        self.log.insert("end", f"Input:           {fmt_bytes(st['in_bytes'])}\n")
        self.log.insert("end", f"Output:          {fmt_bytes(st['out_bytes'])}\n")
        self.log.insert("end", f"Saved:           {fmt_bytes(st['saved_bytes'])}\n")
        self.log.insert("end", f"Ratio:           {st['ratio']:.1f}% (smaller is better)\n")
        self.log.insert("end", f"Time:            {st['spent']:.1f} s\n")
        self.log.insert("end", f"Report:          {st['report']}\n")
        self.log.see("end")

        messagebox.showinfo(
            "完成",
            f"处理完成！\n\n节省：{fmt_bytes(st['saved_bytes'])}\n输出比例：{st['ratio']:.1f}%\nreport.csv 已输出到目标目录。"
        )


if __name__ == "__main__":
    app = App()
    app.mainloop()
