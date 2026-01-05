# -*- coding: utf-8 -*-
import os
import csv
import time
import shutil
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

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Preset:
    name: str
    max_side: int
    jpg_quality: int


PRESETS = [
    Preset("PPT 最稳（推荐） 1600 + Q78", 1600, 78),
    Preset("更清晰（大厅屏/投影） 1920 + Q80", 1920, 80),
    Preset("更小（文件很大/电脑较老） 1280 + Q75", 1280, 75),
    Preset("论文/高保真 3000 + Q88", 3000, 88),
    Preset("自定义", 1600, 78),
]


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


def save_jpeg(img: Image.Image, out_path: Path, quality: int):
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.save(out_path, format="JPEG", quality=quality, optimize=True, progressive=True)


def save_png(img: Image.Image, out_path: Path):
    # For photos-to-PPT: optimize only (no quantize to avoid banding risk)
    img.save(out_path, format="PNG", optimize=True, compress_level=9)


def save_webp(img: Image.Image, out_path: Path, quality: int):
    img.save(out_path, format="WEBP", quality=quality, method=6)


def compress_image(in_path: Path, out_path: Path, max_side: int, jpg_quality: int, webp_quality: int = 80) -> Path:
    suf = in_path.suffix.lower()
    with Image.open(in_path) as im:
        im = ImageOps.exif_transpose(im)
        im = resize_keep_aspect(im, max_side)

        if suf in (".jpg", ".jpeg"):
            outp = out_path.with_suffix(".jpg")
            save_jpeg(im, outp, jpg_quality)
            return outp

        if suf == ".png":
            outp = out_path.with_suffix(".png")
            save_png(im, outp)
            return outp

        if suf == ".webp":
            outp = out_path.with_suffix(".webp")
            save_webp(im, outp, webp_quality)
            return outp

    return out_path


def iter_all_files(src: Path, recursive: bool):
    if recursive:
        for p in src.rglob("*"):
            if p.is_file():
                yield p
    else:
        for p in src.iterdir():
            if p.is_file():
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
        self.title("文件夹批量压缩（图片压缩 + 非图片复制）")
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

        self.preset_var = tk.StringVar(value=PRESETS[0].name)
        self.max_side_var = tk.IntVar(value=PRESETS[0].max_side)
        self.jpg_quality_var = tk.IntVar(value=PRESETS[0].jpg_quality)

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
        ttk.Checkbutton(rowB, text="增量模式（目标更“新”则跳过）", variable=self.incremental_var).pack(side="left")
        ttk.Checkbutton(rowB, text="允许覆盖（更危险）", variable=self.overwrite_var).pack(side="left", padx=12)

        rowC = ttk.Frame(opt); rowC.pack(fill="x", padx=10, pady=6)
        ttk.Label(rowC, text="规格：").pack(side="left")
        preset_box = ttk.Combobox(rowC, textvariable=self.preset_var,
                                  values=[p.name for p in PRESETS], state="readonly", width=32)
        preset_box.pack(side="left", padx=6)
        preset_box.bind("<<ComboboxSelected>>", self.on_preset)

        ttk.Label(rowC, text="最长边(px)：").pack(side="left", padx=8)
        self.max_side_entry = ttk.Entry(rowC, textvariable=self.max_side_var, width=8)
        self.max_side_entry.pack(side="left", padx=6)

        ttk.Label(rowC, text="JPG质量(1-95)：").pack(side="left", padx=8)
        self.jpg_quality_entry = ttk.Entry(rowC, textvariable=self.jpg_quality_var, width=8)
        self.jpg_quality_entry.pack(side="left", padx=6)

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

    def _on_drop(self, event, target_var: tk.StringVar):
        # event.data may contain braces {C:\Path With Space}
        data = event.data.strip()
        if data.startswith("{") and data.endswith("}"):
            data = data[1:-1]
        # accept first path if multiple
        if " " in data and data.count("{") == 0 and data.count("}") == 0:
            # could be multiple paths; take first token if it's a directory-like
            pass
        target_var.set(data)

    def pick_src(self):
        d = filedialog.askdirectory(title="选择输入目录")
        if d:
            self.src_var.set(d)

    def pick_dst(self):
        d = filedialog.askdirectory(title="选择输出目录（建议为空目录）")
        if d:
            self.dst_var.set(d)

    def on_preset(self, _evt=None):
        name = self.preset_var.get()
        preset = next((p for p in PRESETS if p.name == name), PRESETS[0])
        if preset.name != "自定义":
            self.max_side_var.set(preset.max_side)
            self.jpg_quality_var.set(preset.jpg_quality)
            self.max_side_entry.configure(state="disabled")
            self.jpg_quality_entry.configure(state="disabled")
        else:
            self.max_side_entry.configure(state="normal")
            self.jpg_quality_entry.configure(state="normal")

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
            messagebox.showerror("错误", "请输入/选择输出目录")
            return False

        if not (self.process_images_var.get() or self.copy_others_var.get()):
            messagebox.showerror("错误", "至少勾选一个：处理图片 或 复制非图片")
            return False

        try:
            max_side = int(self.max_side_var.get())
            jpg_q = int(self.jpg_quality_var.get())
        except Exception:
            messagebox.showerror("错误", "最长边和质量必须是整数")
            return False

        if max_side < 0:
            messagebox.showerror("错误", "最长边不能为负数（0 表示不缩放）")
            return False
        if not (1 <= jpg_q <= 95):
            messagebox.showerror("错误", "JPG质量范围建议 1-95")
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
        if dst and Path(dst).exists():
            os.startfile(dst)  # Windows

    # ---------- Worker ----------
    def worker(self):
        t0 = time.time()

        src = Path(self.src_var.get().strip())
        dst = Path(self.dst_var.get().strip())
        recursive = bool(self.recursive_var.get())

        do_img = bool(self.process_images_var.get())
        do_copy = bool(self.copy_others_var.get())

        incremental = bool(self.incremental_var.get())
        overwrite = bool(self.overwrite_var.get())

        max_side = int(self.max_side_var.get())
        jpg_q = int(self.jpg_quality_var.get())

        ensure_dir(dst)
        report_path = dst / "report.csv"

        files = list(iter_all_files(src, recursive))
        total = len(files)

        self.ui_log(f"开始处理：{src}")
        self.ui_log(f"输出目录：{dst}")
        self.ui_log(f"递归：{'是' if recursive else '否'} | 处理图片：{'是' if do_img else '否'} | 复制非图片：{'是' if do_copy else '否'}")
        self.ui_log(f"增量：{'是' if incremental else '否'} | 覆盖：{'是' if overwrite else '否'}")
        self.ui_log(f"参数：最长边={max_side} | JPG质量={jpg_q}")
        self.ui_log(f"待处理文件：{total}")
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
            w.writerow(["relative_path", "type", "action", "input_bytes", "output_bytes", "message"])

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
                        # decide real output path (jpg/jpeg -> .jpg; others keep)
                        expected_out = out_path.with_suffix(".jpg") if suf in (".jpg", ".jpeg") else out_path.with_suffix(suf)

                        if not overwrite and expected_out.exists():
                            if incremental and should_skip_incremental(in_path, expected_out):
                                skipped += 1
                                out_size = expected_out.stat().st_size
                                out_bytes += out_size
                                msg = "skip(incremental)"
                                self.ui_log(f"[SKIP][IMG] {rel}")
                                w.writerow([str(rel), "image", "skip", in_size, out_size, msg])
                                continue
                            else:
                                # if not overwrite, and not eligible skip, we still avoid overwrite
                                skipped += 1
                                msg = "exists(not overwrite)"
                                self.ui_log(f"[SKIP][IMG] {rel} (目标已存在，未允许覆盖)")
                                w.writerow([str(rel), "image", "skip", in_size, 0, msg])
                                continue

                        real_out = compress_image(in_path, out_path, max_side=max_side, jpg_quality=jpg_q)
                        out_size = real_out.stat().st_size
                        out_bytes += out_size
                        img_ok += 1
                        self.ui_log(f"[IMG] {rel}  {in_size/1024:.1f}KB -> {out_size/1024:.1f}KB")
                        w.writerow([str(rel), "image", "compress", in_size, out_size, "ok"])

                    elif (not is_img) and do_copy:
                        # Copy other files
                        if not overwrite and out_path.exists():
                            if incremental and should_skip_incremental(in_path, out_path):
                                skipped += 1
                                out_size = out_path.stat().st_size
                                out_bytes += out_size
                                self.ui_log(f"[SKIP][CPY] {rel}")
                                w.writerow([str(rel), "other", "skip", in_size, out_size, "skip(incremental)"])
                                continue
                            else:
                                skipped += 1
                                self.ui_log(f"[SKIP][CPY] {rel} (目标已存在，未允许覆盖)")
                                w.writerow([str(rel), "other", "skip", in_size, 0, "exists(not overwrite)"])
                                continue

                        shutil.copy2(in_path, out_path)
                        out_size = out_path.stat().st_size
                        out_bytes += out_size
                        copy_ok += 1
                        self.ui_log(f"[CPY] {rel}  {in_size/1024:.1f}KB")
                        w.writerow([str(rel), "other", "copy", in_size, out_size, "ok"])

                    else:
                        # Either skipped due to options
                        skipped += 1
                        kind = "image" if is_img else "other"
                        self.ui_log(f"[SKIP][{kind.upper()}] {rel} (按设置不处理)")
                        w.writerow([str(rel), kind, "skip", in_size, 0, "disabled by option"])

                except Exception as e:
                    errors += 1
                    self.ui_log(f"[ERR] {rel}  {e}")
                    w.writerow([str(rel), "unknown", "error", in_size, out_size, str(e)])

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
