# -*- coding: utf-8 -*-
"""
桌面摸鱼阅读器 —— tkinter 版（零第三方依赖）

用你默认的 Python 就能跑：D:\environment\anaconda\envs\python3.10\python.exe reader_tk.py
用法完全一致：拖 txt 进文件夹发到本程序 / Ctrl+O 打开；老板键 Ctrl+Alt+H 隐身。
"""
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

APP_NAME = "ZhuoMianYueDuQi"
TITLE_TAG = "ZhuoMianYueDuQi"          # 用于单实例检测
ENCODINGS = ["utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"]

THEMES = {
    "深夜":   {"bg": "#1B1B1F", "fg": "#DCDCDC", "sel": "#3A6EA5", "bar": "#2A2A31"},
    "浅色":   {"bg": "#F7F7F5", "fg": "#202020", "sel": "#A8CBEE", "bar": "#E8E8E6"},
    "米黄":   {"bg": "#F2E8CE", "fg": "#3A2E1B", "sel": "#DCBE8C", "bar": "#E4D7B8"},
    "护眼绿": {"bg": "#C8EDCE", "fg": "#0F3316", "sel": "#84C792", "bar": "#B2E0BB"},
}

DEFAULT_CONFIG = {
    "file": "",
    "pos": [120, 120],
    "size": [760, 520],
    "opacity": 0.88,
    "font_size": 18,
    "font_family": "Microsoft YaHei UI",
    "theme": "深夜",
    "wrap": True,
    "line_height": 150,
    "auto_scroll": False,
    "speed": 25,          # 像素/秒
    "always_on_top": True,
    "toolbar_autohide": True,
    "edge_snap": False,
    "last_dir": "",
    "recent": [],
    "progress": {},       # 文件路径 -> float(滚动百分比)
}

CH_RE = re.compile(
    r"^\s*(第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷篇段落]"
    r"|[Cc]hapter\s+[0-9ivxIVX]+"
    r"|(序章|楔子|尾声|后记|番外)(?=$|[\s·•:：.、\-—～0-9一二三四五六七八九十]).{0,20}$)",
)


# ---------------------------------------------------------------- 基础设施
def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        p = config_path()
        if p.exists():
            for k, v in json.loads(p.read_text(encoding="utf-8")).items():
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        config_path().write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_text_file(path: str) -> str:
    raw = Path(path).read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


class Hotkeys(threading.Thread):
    """独立线程注册 Windows 全局热键，窗口隐藏后依然生效。"""

    MOD_ALT, MOD_CONTROL = 0x1, 0x2
    COMBOS = [
        ("toggle",  MOD_CONTROL | MOD_ALT, 0x48),   # Ctrl+Alt+H
        ("bigger",  MOD_CONTROL | MOD_ALT, 0x26),   # Ctrl+Alt+↑
        ("smaller", MOD_CONTROL | MOD_ALT, 0x28),   # Ctrl+Alt+↓
        ("pause",   MOD_CONTROL | MOD_ALT, 0x50),   # Ctrl+Alt+P
        ("kill",    MOD_CONTROL | MOD_ALT, 0x51),   # Ctrl+Alt+Q
    ]

    def __init__(self, out: queue.Queue):
        super().__init__(daemon=True)
        self.out = out
        self.failed = []

    def run(self):
        user32 = ctypes.windll.user32
        for i, (name, mods, vk) in enumerate(self.COMBOS):
            if not user32.RegisterHotKey(None, i + 1, mods, vk):
                self.failed.append(name)
        msg = wintypes.MSG()
        try:
            while True:
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0312 and 1 <= msg.wParam <= len(self.COMBOS):
                        self.out.put(self.COMBOS[msg.wParam - 1][0])
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.02)
        finally:
            for i in range(len(self.COMBOS)):
                user32.UnregisterHotKey(None, i + 1)


# ---------------------------------------------------------------- 主窗口
class Reader:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.hotkey_q = queue.Queue()
        self.collapsed = None
        self.saved_geom = ""
        self.search_term = ""
        self.drag = None
        self.scroll_job = None
        self.toc_win = None
        self.toc_items = []

        root.title(TITLE_TAG)
        root.configure(bg="#1B1B1F")
        root.overrideredirect(True)
        root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        root.attributes("-alpha", float(self.cfg["opacity"]))
        x, y = self.cfg["pos"]
        w, h = self.cfg["size"]
        root.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")

        self._build_ui()
        self._apply_theme()
        self._apply_font()
        self._bind_events()

        if self.cfg.get("file"):
            self.load_file(self.cfg["file"], silent=True)
        else:
            self._welcome()

        self.set_toolbar_visible(not self.cfg["toolbar_autohide"])
        self.hotkeys = Hotkeys(self.hotkey_q)
        self.hotkeys.start()
        root.after(150, self._poll_hotkeys)
        root.after(900, self._report_hotkey_failure)

    # ---------------- UI ----------------
    def _build_ui(self):
        bar_bg = THEMES[self.cfg["theme"]]["bar"]
        self.bar = tk.Frame(self.root, bg=bar_bg, height=26)
        self.bar.pack(side="bottom", fill="x")
        self.bar.pack_propagate(False)

        self.pct = tk.Label(self.bar, text="--", width=5, bg=bar_bg, fg="#DCDCDC",
                            font=("Microsoft YaHei UI", 9))
        self.pct.pack(side="left", padx=(6, 2))

        self.buttons = {}
        specs = [
            ("open",  "文件", self.open_file,        "打开 txt (Ctrl+O)"),
            ("find",  "查找", self.search,           "搜索 (Ctrl+F)，F3 下一个"),
            ("toc",   "目录", self.show_toc,         "章节目录 (Ctrl+T)"),
            ("theme", "主题", self.cycle_theme,      "切换配色 (Ctrl+G)"),
            ("wrap",  "换行", self.toggle_wrap,      "自动换行 (Ctrl+W)"),
            ("fin",   "A-",  lambda: self.bump_font(-1), "缩小字号"),
            ("fout",  "A+",  lambda: self.bump_font(1),  "放大字号"),
            ("pin",   "置顶", self.toggle_top,       "窗口置顶 (Ctrl+P)"),
            ("play",  "滚",  self.toggle_scroll,     "自动滚动 (空格)"),
            ("hide",  "隐藏", self.toggle_hidden,    "老板键 (Esc)"),
            ("quit",  "×",   self.quit_app,          "退出 (Ctrl+Q)"),
        ]
        for key, label, fn, tip in specs:
            b = tk.Button(self.bar, text=label, relief="flat", bd=0, padx=5, pady=2,
                          bg=bar_bg, fg="#DCDCDC", activebackground="#555555",
                          activeforeground="#FFFFFF", cursor="hand2",
                          font=("Microsoft YaHei UI", 9), command=fn)
            b.pack(side="left", padx=1)
            self.buttons[key] = b

        body = tk.Frame(self.root, bg="#1B1B1F")
        body.pack(side="top", fill="both", expand=True)

        self.txt = tk.Text(
            body, wrap="word", relief="flat", bd=0, highlightthickness=0,
            padx=12, pady=8, spacing1=2, spacing3=6,
            font=(self.cfg["font_family"], int(self.cfg["font_size"])),
            insertwidth=0, cursor="xterm",
            selectbackground=THEMES[self.cfg["theme"]]["sel"],
        )
        self.sb = tk.Scrollbar(body, orient="vertical", width=8,
                               relief="flat", bd=0, troughcolor="#000000",
                               activebackground="#888888")
        self.txt.configure(yscrollcommand=self._on_scroll)
        self.sb.configure(command=self.txt.yview)
        self.sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("found", background="#F2C14E", foreground="#000000")

        # 右下角缩放把手
        self.grip = tk.Label(body, text="⇘", bg=bar_bg, fg="#888888",
                             width=2, cursor="size_nw_se",
                             font=("Microsoft YaHei UI", 7))
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<B1-Motion>", self._resize)
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.lift()

    def _welcome(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0",
                        "把 txt 拖到程序图标上打开，或按 Ctrl+O 选择文件。\n\n"
                        "全局老板键 Ctrl+Alt+H —— 随时隐身。\n"
                        "Alt + 左键拖动窗口；Ctrl+滚轮改字号；Alt+滚轮改透明度。\n"
                        "右键有完整菜单。")
        self.txt.configure(state="disabled")

    # ---------------- 主题 / 字体 ----------------
    def theme(self):
        return THEMES.get(self.cfg["theme"], THEMES["深夜"])

    def _apply_theme(self):
        t = self.theme()
        self.root.configure(bg=t["bg"])
        self.txt.configure(bg=t["bg"], fg=t["fg"], selectbackground=t["sel"],
                           insertbackground=t["fg"])
        if hasattr(self, "sb"):
            self.sb.configure(bg=t["bar"], troughcolor=t["bg"])
        # 正文容器
        for child in self.root.winfo_children():
            if isinstance(child, tk.Frame) and child is not self.bar:
                child.configure(bg=t["bg"])
        if hasattr(self, "grip"):
            self.grip.configure(bg=t["bar"])
        self.bar.configure(bg=t["bar"])
        self.pct.configure(bg=t["bar"], fg=t["fg"])
        for b in self.buttons.values():
            b.configure(bg=t["bar"], fg=t["fg"], activebackground=t["sel"])
        if self.toc_win and self.toc_win.winfo_exists():
            self.toc_win.configure(bg=t["bg"])
            self.toc_list.configure(bg=t["bg"], fg=t["fg"], selectbackground=t["sel"])

    def _apply_font(self):
        size = int(self.cfg["font_size"])
        self.txt.configure(font=(self.cfg["font_family"], size))
        # 行距：百分比换算到 spacing3（段落下方留白）
        extra = max(0, int((int(self.cfg.get("line_height", 150)) - 100) / 100 * size))
        self.txt.configure(spacing3=extra + 2, spacing1=2)
        self.txt.tag_configure("found", background="#F2C14E", foreground="#000000")

    # ---------------- 事件 ----------------
    def _bind_events(self):
        r = self.root
        for w in (r, self.txt, self.bar):
            w.bind("<Enter>", lambda e: self.on_enter())
            w.bind("<Leave>", lambda e: self.on_leave())
        self.txt.bind("<MouseWheel>", self.on_wheel)
        self.txt.bind("<ButtonRelease-1>", lambda e: self.update_pct())

        self._accel("<Control-o>", self.open_file)
        self._accel("<Control-f>", self.search)
        self._accel("<F3>", self.find_next)
        self._accel("<Control-t>", self.show_toc)
        self._accel("<Control-g>", self.cycle_theme)
        self._accel("<Control-w>", self.toggle_wrap)
        self._accel("<Control-p>", self.toggle_top)
        self._accel("<Control-v>", self.open_from_clipboard)
        self._accel("<space>", self.toggle_scroll)
        self._accel("<Escape>", self.toggle_hidden)
        self._accel("<Next>", lambda: self.page(1))
        self._accel("<Prior>", lambda: self.page(-1))
        self._accel("<plus>", lambda: self.bump_font(1))
        self._accel("<equal>", lambda: self.bump_font(1))
        self._accel("<minus>", lambda: self.bump_font(-1))

        self._drag_bind()
        self.root.bind("<Button-3>", self.show_menu)

    def _accel(self, seq, fn):
        self.root.bind_all(seq, lambda e: fn())

    def _drag_bind(self):
        targets = (self.root, self.txt, self.bar, self.grip)
        for w in targets:
            w.bind("<Alt-ButtonPress-1>", self._drag_start)
            w.bind("<Alt-B1-Motion>", self._drag_move)
            w.bind("<Alt-ButtonRelease-1>", self._drag_end)

    def _drag_start(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        if self.collapsed:
            self.expand()

    def _drag_move(self, e):
        if not self.drag:
            return
        dx, dy = self.drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _drag_end(self, e):
        self.drag = None
        self.maybe_snap()

    def _resize_start(self, e):
        self._rs = (self.root.winfo_width(), self.root.winfo_height(),
                    e.x_root, e.y_root)

    def _resize(self, e):
        if not hasattr(self, "_rs"):
            return
        w0, h0, x0, y0 = self._rs
        w = max(240, w0 + (e.x_root - x0))
        h = max(120, h0 + (e.y_root - y0))
        self.root.geometry(f"{w}x{h}")

    @staticmethod
    def _modifiers():
        # 用系统 API 实时读修饰键，避免 tkinter 滚轮事件的 event.state
        # 夹带扩展位（0x20000）导致把普通滚轮误判成 Alt+滚轮
        u = ctypes.windll.user32
        ctrl = bool(u.GetAsyncKeyState(0x11) & 0x8000)   # VK_CONTROL
        alt = bool(u.GetAsyncKeyState(0x12) & 0x8000)    # VK_MENU
        return ctrl, alt

    def on_wheel(self, e):
        delta = 1 if e.delta > 0 else -1
        ctrl, alt = self._modifiers()
        if ctrl:
            self.bump_font(delta)
            return "break"
        if alt:
            self.bump_opacity(delta * 0.05)
            return "break"
        if self.scroll_job:
            self.toggle_scroll()
        self.root.after_idle(self.update_pct)

    def on_enter(self):
        if self.collapsed:
            self.expand()
        self.set_toolbar_visible(True)

    def on_leave(self):
        if self.cfg["toolbar_autohide"]:
            self.set_toolbar_visible(False)
        if self.cfg.get("edge_snap") and not self.collapsed:
            if self._near_edge():
                self.root.after(700, self._collapse_if_idle)

    # ---------------- 文件 ----------------
    def open_file(self):
        start = self.cfg.get("last_dir") or str(Path.home())
        path = filedialog.askopenfilename(
            parent=self.root, initialdir=start, title="选择 txt",
            filetypes=[("文本文件", "*.txt *.log *.md"), ("所有文件", "*.*")])
        if not path:
            return
        self.cfg["last_dir"] = str(Path(path).parent)
        if self.load_file(path):
            save_config(self.cfg)

    def open_from_clipboard(self):
        try:
            text = self.root.clipboard_get().strip().strip('"')
        except Exception:
            return
        if Path(text).exists():
            if self.load_file(text):
                save_config(self.cfg)

    def load_file(self, path, silent=False):
        if not path or not Path(path).exists():
            if not silent:
                messagebox.showwarning(APP_NAME, f"文件不存在：\n{path}")
            return False
        try:
            content = read_text_file(path)
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"读取失败：\n{ex}")
            return False
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", content)
        self.txt.configure(state="disabled")
        self.txt.yview_moveto(0.0)
        self.txt.tag_remove("found", "1.0", "end")
        self.cfg["file"] = str(Path(path).resolve())
        self.recent_add(self.cfg["file"])
        self.build_toc(content)
        self._apply_font()
        self.root.after(80, self.restore_progress)
        self.update_pct()
        return True

    def recent_add(self, path):
        rec = [p for p in self.cfg["recent"] if p != path]
        rec.insert(0, path)
        self.cfg["recent"] = rec[:10]

    def restore_progress(self):
        frac = self.cfg["progress"].get(self.cfg["file"])
        if frac:
            self.txt.yview_moveto(float(frac))
        self.update_pct()

    def maybe_save_progress(self, force=False):
        path = self.cfg.get("file")
        if not path or not Path(path).exists():
            return
        self.cfg["progress"][path] = round(self.txt.yview()[0], 5)
        if len(self.cfg["progress"]) > 50:
            keep = sorted(self.cfg["progress"].items(), key=lambda kv: kv[1])[-50:]
            self.cfg["progress"] = dict(keep)
        if force:
            save_config(self.cfg)

    # ---------------- 目录 / 搜索 ----------------
    def build_toc(self, content):
        self.toc_items = []
        for i, line in enumerate(content.splitlines()[:20000]):
            s = line.strip()
            if 0 < len(s) <= 60 and CH_RE.match(s):
                self.toc_items.append((i, s))
        if self.toc_win and self.toc_win.winfo_exists():
            self._fill_toc()

    def _fill_toc(self):
        self.toc_list.delete(0, "end")
        for line_no, title in self.toc_items:
            self.toc_list.insert("end", title)
        self._toc_lines = [a for a, _ in self.toc_items]

    def show_toc(self):
        if self.toc_win and self.toc_win.winfo_exists():
            self.toc_win.destroy()
        t = self.theme()
        win = tk.Toplevel(self.root)
        win.title("目录")
        win.transient(self.root)
        win.geometry(f"320x460+{self.root.winfo_x() + self.root.winfo_width() + 6}"
                     f"+{self.root.winfo_y()}")
        self.toc_win = win
        self.toc_list = tk.Listbox(win, bg=t["bg"], fg=t["fg"], relief="flat", bd=0,
                                   selectbackground=t["sel"], activestyle="none",
                                   font=("Microsoft YaHei UI", 10))
        sc = tk.Scrollbar(win, orient="vertical", command=self.toc_list.yview)
        self.toc_list.configure(yscrollcommand=sc.set)
        sc.pack(side="right", fill="y")
        self.toc_list.pack(side="left", fill="both", expand=True)
        self._fill_toc()
        self.toc_list.bind("<Double-Button-1>", self._toc_jump)
        self.toc_list.bind("<Return>", self._toc_jump)

    def _toc_jump(self, e=None):
        sel = self.toc_list.curselection()
        if not sel:
            return
        line = self._toc_lines[sel[0]]
        self.txt.tag_remove("found", "1.0", "end")
        self.txt.see(f"{line + 1}.0")
        self.txt.mark_set("insert", f"{line + 1}.0")
        self.root.after(50, self.update_pct)

    def search(self):
        try:
            sel = self.txt.get("sel.first", "sel.last")
        except Exception:
            sel = ""
        term = simpledialog.askstring("搜索", "关键词（F3 找下一个）",
                                      initialvalue=sel or self.search_term,
                                      parent=self.root)
        if term is None:
            return
        term = term.strip()
        if not term:
            return
        self.search_term = term
        self.txt.mark_set("insert", self.txt.index("insert"))
        self.find_next()

    def find_next(self):
        term = self.search_term
        if not term:
            self.search()
            return
        start = self.txt.index("insert +1c")
        idx = self.txt.search(term, start, stopindex="end", nocase=1)
        if not idx:                                  # 回绕到开头
            idx = self.txt.search(term, "1.0", stopindex="end", nocase=1)
            if not idx:
                messagebox.showinfo("搜索", f"没找到「{term}」")
                return
        self.txt.tag_remove("found", "1.0", "end")
        end = f"{idx}+{len(term)}c"
        self.txt.tag_add("found", idx, end)
        self.txt.mark_set("insert", idx)
        self.txt.see(idx)
        self.root.after(50, self.update_pct)

    # ---------------- 工具栏功能 ----------------
    def set_toolbar_visible(self, v):
        if not self.cfg["toolbar_autohide"]:
            v = True
        if v:
            self.bar.pack(side="bottom", fill="x")
            self.grip.place(relx=1.0, rely=1.0, anchor="se")
        else:
            self.bar.pack_forget()
            self.grip.place_forget()

    def _on_scroll(self, first, last):
        self.sb.set(first, last)
        self.update_pct()

    def update_pct(self):
        try:
            top, bot = self.txt.yview()
            total = self.txt.index("end-1c")
            pct = 0 if bot >= 0.9999 else int(top * 100)
            self.pct.configure(text=f"{pct}%")
        except Exception:
            pass

    def page(self, direction):
        lines = max(1, self.txt.winfo_height() // max(10, self.cfg["font_size"] * 2))
        self.txt.yview_scroll(int(lines * 0.9) * direction, "units")
        self.update_pct()

    def bump_font(self, delta):
        self.cfg["font_size"] = int(max(8, min(72, self.cfg["font_size"] + delta)))
        self._apply_font()
        self.update_pct()

    def bump_opacity(self, delta):
        v = round(max(0.15, min(1.0, self.cfg["opacity"] + delta)), 2)
        self.cfg["opacity"] = v
        self.root.attributes("-alpha", v)

    def cycle_theme(self):
        names = list(THEMES)
        i = (names.index(self.cfg["theme"]) + 1) % len(names)
        self.cfg["theme"] = names[i]
        self._apply_theme()

    def cycle_line_height(self):
        steps = [120, 140, 150, 170, 200]
        cur = int(self.cfg.get("line_height", 150))
        self.cfg["line_height"] = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else 150
        self._apply_font()

    def toggle_wrap(self):
        self.cfg["wrap"] = not self.cfg["wrap"]
        self.txt.configure(wrap="word" if self.cfg["wrap"] else "none")
        self.buttons["wrap"].configure(
            text="换行✓" if self.cfg["wrap"] else "换行")

    def toggle_top(self):
        self.cfg["always_on_top"] = not self.cfg["always_on_top"]
        self.root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        self.buttons["pin"].configure(
            text="置顶✓" if self.cfg["always_on_top"] else "置顶")

    def toggle_scroll(self):
        if self.scroll_job:
            self.root.after_cancel(self.scroll_job)
            self.scroll_job = None
            self.buttons["play"].configure(text="滚")
        else:
            self._scroll_acc = 0.0
            self._tick()
            self.buttons["play"].configure(text="滚⏸")

    def _tick(self):
        self._scroll_acc += self.cfg["speed"] * 0.05
        step = int(self._scroll_acc)
        if step >= 1:
            self._scroll_acc -= step
            if self.txt.yview()[1] >= 0.9999:
                self.scroll_job = None
                self.buttons["play"].configure(text="滚")
                return
            self.txt.yview_scroll(step, "pixels")
            self.update_pct()
        self.scroll_job = self.root.after(50, self._tick)

    def toggle_hidden(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-alpha", self.cfg["opacity"])
            if getattr(self, "_resume_scroll", False):
                self._resume_scroll = False
                self._scroll_acc = 0.0
                self._tick()
                self.buttons["play"].configure(text="滚⏸")
        else:
            # 隐藏时暂停自动滚动，回来再接着滚
            self._resume_scroll = bool(self.scroll_job)
            if self.scroll_job:
                self.root.after_cancel(self.scroll_job)
                self.scroll_job = None
                self.buttons["play"].configure(text="滚")
            self.maybe_save_progress()
            save_config(self.cfg)
            self.root.withdraw()

    # ---------------- 贴边隐藏 ----------------
    def _screen(self):
        return (self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def _near_edge(self):
        return self.root.winfo_x() <= 10 or \
            self.root.winfo_x() + self.root.winfo_width() >= self.root.winfo_screenwidth() - 10

    def maybe_snap(self):
        if not self.cfg.get("edge_snap"):
            return
        if self._near_edge():
            self.collapse()

    def _collapse_if_idle(self):
        try:
            if not self.collapsed and self.root.winfo_containing(
                    self.root.winfo_pointerx(), self.root.winfo_pointery()) is None:
                self.collapse()
        except Exception:
            pass

    def collapse(self):
        if self.collapsed:
            return
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        side = "left" if x <= 10 else "right"
        self.saved_geom = f"{w}x{h}+{x}+{y}"
        self.collapsed = side
        edge = 8
        nx = -w + edge if side == "left" else self.root.winfo_screenwidth() - edge
        self.root.geometry(f"{w}x{h}+{nx}+{y}")
        self.root.attributes("-alpha", 0.55)
        self.bar.pack_forget()

    def expand(self):
        if not self.collapsed:
            return
        self.collapsed = None
        if self.saved_geom:
            self.root.geometry(self.saved_geom)
        self.root.attributes("-alpha", self.cfg["opacity"])
        self.set_toolbar_visible(not self.cfg["toolbar_autohide"])

    def toggle_edge_snap(self):
        self.cfg["edge_snap"] = not self.cfg.get("edge_snap")
        if not self.cfg["edge_snap"]:
            self.expand()

    # ---------------- 菜单 ----------------
    def show_menu(self, e=None):
        t = self.theme()
        m = tk.Menu(self.root, tearoff=0, bg=t["bar"], fg=t["fg"], bd=0,
                    activebackground=t["sel"], activeforeground="#000000",
                    font=("Microsoft YaHei UI", 9))
        m.add_command(label="打开文件… (Ctrl+O)", command=self.open_file)
        for p in self.cfg.get("recent", [])[:6]:
            m.add_command(label="  " + Path(p).name,
                          command=lambda _p=p: self.load_file(_p))
        m.add_separator()
        m.add_command(label="章节目录 (Ctrl+T)", command=self.show_toc)
        m.add_command(label="查找 (Ctrl+F)", command=self.search)
        m.add_command(label="切换配色 (Ctrl+G)", command=self.cycle_theme)
        m.add_command(label="自动换行 (Ctrl+W)", command=self.toggle_wrap)
        m.add_command(label=f"行距 {self.cfg.get('line_height')}%",
                      command=self.cycle_line_height)
        m.add_checkbutton(label="窗口置顶", command=self.toggle_top)
        m.add_checkbutton(
            label="贴边隐藏（拖到屏幕边缘自动收起）", command=self.toggle_edge_snap)
        m.add_checkbutton(label="工具条自动隐藏", command=self.toggle_autohide)
        m.add_separator()
        m.add_command(label="字号…", command=self.ask_font)
        m.add_command(label="不透明度…", command=self.ask_opacity)
        m.add_command(label="自动滚动速度…", command=self.ask_speed)
        m.add_separator()
        m.add_command(label="隐藏窗口 (Esc)", command=self.toggle_hidden)
        m.add_command(label="退出", command=self.quit_app)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def toggle_autohide(self):
        self.cfg["toolbar_autohide"] = not self.cfg["toolbar_autohide"]
        self.set_toolbar_visible(not self.cfg["toolbar_autohide"])

    def ask_font(self):
        v = simpledialog.askinteger("字号", "字号 (8-72)",
                                    initialvalue=self.cfg["font_size"],
                                    minvalue=8, maxvalue=72, parent=self.root)
        if v:
            self.cfg["font_size"] = int(v)
            self._apply_font()

    def ask_opacity(self):
        v = simpledialog.askinteger("不透明度", "百分比 (15-100)",
                                    initialvalue=int(self.cfg["opacity"] * 100),
                                    minvalue=15, maxvalue=100, parent=self.root)
        if v:
            self.cfg["opacity"] = v / 100
            self.root.attributes("-alpha", self.cfg["opacity"])

    def ask_speed(self):
        v = simpledialog.askinteger("自动滚动", "像素 / 秒 (5-300)",
                                    initialvalue=self.cfg["speed"],
                                    minvalue=5, maxvalue=300, parent=self.root)
        if v:
            self.cfg["speed"] = int(v)

    # ---------------- 老板键轮询 ----------------
    def _poll_hotkeys(self):
        try:
            while True:
                name = self.hotkey_q.get_nowait()
                self._dispatch(name)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_hotkeys)

    def _dispatch(self, name):
        actions = {
            "toggle": self.toggle_hidden,
            "bigger": lambda: self.bump_font(2),
            "smaller": lambda: self.bump_font(-2),
            "pause": self.toggle_scroll,
            "kill": self.quit_app,
        }
        fn = actions.get(name)
        if fn:
            fn()

    def _report_hotkey_failure(self):
        if getattr(self.hotkeys, "failed", None):
            if "toggle" in self.hotkeys.failed:
                messagebox.showwarning(
                    APP_NAME, "老板键 Ctrl+Alt+H 被其它程序占用了，\n请关掉占用它的软件后重启本程序。")

    # ---------------- 退出 ----------------
    def quit_app(self):
        self.maybe_save_progress()
        if self.collapsed and self.saved_geom:
            geometry = self.saved_geom
        else:
            w, h = self.root.winfo_width(), self.root.winfo_height()
            geometry = f"{w}x{h}+{self.root.winfo_x()}+{self.root.winfo_y()}"
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry)
        if m:
            self.cfg["size"] = [int(m.group(1)), int(m.group(2))]
            self.cfg["pos"] = [int(m.group(3)), int(m.group(4))]
        save_config(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------- 入口
def already_running() -> bool:
    try:
        return bool(ctypes.windll.user32.FindWindowW(None, TITLE_TAG))
    except Exception:
        return False


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if already_running():
        messagebox.showinfo(APP_NAME, "阅读器已经在运行了。\n按 Ctrl+Alt+H 把它调出来。")
        return

    root = tk.Tk()
    app = Reader(root)
    if args and Path(args[0]).exists():
        if app.load_file(args[0]):
            save_config(app.cfg)
    if "--selftest" in sys.argv:
        root.after(1200, app.quit_app)
    root.mainloop()


if __name__ == "__main__":
    main()
