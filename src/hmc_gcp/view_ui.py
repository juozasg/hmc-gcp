"""Viewer: page through the photos and see what was tagged in each."""

from __future__ import annotations

import json
from pathlib import Path

import pygame
import pygame_gui
from pygame_gui.elements import UIButton, UIPanel, UITextBox

from . import render as R
from .imgio import ImageCache, crop_padded
from .session import ACCEPTED, STATE_FILE

PANEL_W = 380
FILTERS = ("photos with GCPs", "photos with tags", "all photos")

HELP = (
    "<b>hmc-gcp view: keys</b><br><br>"
    "<b>Right / Left</b>: next / previous photo<br>"
    "<b>Home / End</b>: first / last photo<br>"
    "<b>PgDn / PgUp</b>: 10 photos forward / back<br>"
    "<b>+ / -</b> or mouse wheel: zoom (wheel zooms at the cursor)<br>"
    "<b>Drag</b> with left mouse button: pan<br>"
    "<b>F</b> or <b>0</b>: fit the whole photo<br>"
    "<b>Tab</b>: zoom to the next GCP in this photo<br>"
    "<b>T</b>: cycle filter (photos with GCPs / with tags / all)<br>"
    "<b>1</b>: show / hide accepted tags<br>"
    "<b>2</b>: show / hide unreviewed predictions<br>"
    "<b>3</b>: show / hide 'not visible' marks<br>"
    "<b>L</b>: show / hide labels<br>"
    "<b>R</b>: reload tags (to see changes made in the tag window)<br>"
    "<b>H</b> or <b>F1</b>: show / hide this help<br>"
    "<b>Q</b> or <b>Esc</b>: quit<br><br>"
    "Green cross: accepted. Cyan: placed by hand. "
    "Yellow ring: unreviewed prediction. Red X: marked not visible."
)


class ViewApp:
    def __init__(self, images_dir: Path, out_dir: Path, size=(1500, 950)):
        self.images_dir = Path(images_dir)
        self.state_path = Path(out_dir) / STATE_FILE
        if not self.state_path.exists():
            raise SystemExit(f"No {STATE_FILE} in {out_dir}. Run 'hmc-gcp tag' first.")
        pygame.display.init()
        pygame.font.init()
        self.W, self.H = size
        self.screen = pygame.display.set_mode(size)
        pygame.display.set_caption("hmc-gcp view")
        self.ui = pygame_gui.UIManager(size)
        self.ui.preload_fonts([{'name': 'noto_sans', 'point_size': 14, 'style': 'bold',
                               'antialiased': '1'}])
        self.canvas = pygame.Rect(0, 0, self.W - PANEL_W, self.H)
        self.canvas_surf = pygame.Surface(self.canvas.size)
        self.font = pygame.font.Font(None, 22)
        self.cache = ImageCache(3)
        self.filter = 0
        self.show_acc = self.show_pend = self.show_abs = self.show_labels = True
        self.zoom, self.center = 1.0, None
        self.tag_cycle = -1
        self.drag = None
        self.load_state()
        self.idx = 0
        self._build_ui()
        self.reset_view()

    # ------------------------------------------------------------ data
    def load_state(self):
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.images = data["images"]
        self.gcp_z = {g["name"]: g["z"] for g in data["gcps"]}
        self.by_image: dict[str, list] = {}
        for it in data["items"]:
            self.by_image.setdefault(it["image"], []).append(it)
        self.offset = data.get("offset_m", [0, 0])
        self.apply_filter()

    def apply_filter(self, keep_name=None):
        f = self.filter
        if f == 0:
            lst = [im for im in self.images
                   if any(t["status"] != "outside" for t in self.by_image.get(im["name"], []))]
        elif f == 1:
            lst = [im for im in self.images
                   if any(t["status"] in ACCEPTED for t in self.by_image.get(im["name"], []))]
        else:
            lst = list(self.images)
        self.visible = lst or list(self.images)
        names = [im["name"] for im in self.visible]
        self.idx = names.index(keep_name) if keep_name in names else 0

    def img_path(self, name: str) -> str:
        p = self.images_dir / name
        return str(p)

    @property
    def im(self):
        return self.visible[self.idx]

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        x0 = self.W - PANEL_W
        self.panel = UIPanel(pygame.Rect(x0, 0, PANEL_W, self.H), manager=self.ui)
        self.info = UITextBox("", pygame.Rect(8, 8, PANEL_W - 22, 420),
                              manager=self.ui, container=self.panel)
        specs = [
            ("< Prev  [Left]", lambda: self.go(self.idx - 1)),
            ("Next >  [Right]", lambda: self.go(self.idx + 1)),
            ("First  [Home]", lambda: self.go(0)),
            ("Last  [End]", lambda: self.go(len(self.visible) - 1)),
            ("Fit  [F]", self.reset_view), ("Next GCP  [Tab]", self.next_tag),
            ("Zoom in  [+]", lambda: self.zoom_at(1.25)),
            ("Zoom out  [-]", lambda: self.zoom_at(0.8)),
            ("Filter  [T]", self.cycle_filter), ("Labels  [L]", self.toggle_labels),
            ("Accepted  [1]", lambda: self.toggle("show_acc")),
            ("Unreviewed  [2]", lambda: self.toggle("show_pend")),
            ("Not visible  [3]", lambda: self.toggle("show_abs")),
            ("Reload  [R]", self.reload), ("Help  [H]", self.toggle_help), ("Quit  [Q]", None),
        ]
        self.buttons = {}
        bw, bh, gap = (PANEL_W - 30) // 2, 34, 6
        y = 440
        for i, (text, fn) in enumerate(specs):
            col, row = i % 2, i // 2
            b = UIButton(pygame.Rect(8 + col * (bw + gap), y + row * (bh + gap), bw, bh),
                         text, manager=self.ui, container=self.panel)
            self.buttons[b] = fn
        self.help = UITextBox(HELP, pygame.Rect(40, 40, 600, 520), manager=self.ui)
        self.help.hide()

    # ------------------------------------------------------------ actions
    def go(self, idx: int):
        self.idx = max(0, min(len(self.visible) - 1, idx))
        self.tag_cycle = -1
        self.reset_view()

    def fit_scale(self) -> float:
        im = self.im
        return min(self.canvas.w / im["width"], self.canvas.h / im["height"])

    def reset_view(self):
        im = self.im
        self.zoom = 1.0
        self.center = [im["width"] / 2, im["height"] / 2]
        self.dirty = True

    def zoom_at(self, f, screen_pos=None):
        old = self.fit_scale() * self.zoom
        self.zoom = max(1.0, min(40.0, self.zoom * f))
        new = self.fit_scale() * self.zoom
        if screen_pos is not None:
            cx, cy = self.canvas.w / 2, self.canvas.h / 2
            ix = self.center[0] + (screen_pos[0] - cx) / old
            iy = self.center[1] + (screen_pos[1] - cy) / old
            self.center = [ix - (screen_pos[0] - cx) / new, iy - (screen_pos[1] - cy) / new]
        if self.zoom == 1.0:
            self.center = [self.im["width"] / 2, self.im["height"] / 2]
        self.dirty = True

    def next_tag(self):
        tags = [t for t in self.by_image.get(self.im["name"], []) if t["status"] != "outside"]
        if not tags:
            return
        self.tag_cycle = (self.tag_cycle + 1) % len(tags)
        t = tags[self.tag_cycle]
        p = t["px"] or t["pred"]
        if p:
            self.center = list(p)
            self.zoom = max(1.0, min(40.0, self.im["width"] / 400))
        self.dirty = True

    def cycle_filter(self):
        name = self.im["name"]
        self.filter = (self.filter + 1) % len(FILTERS)
        self.apply_filter(keep_name=name)
        self.reset_view()

    def toggle(self, attr):
        setattr(self, attr, not getattr(self, attr))
        self.dirty = True

    def toggle_labels(self):
        self.toggle("show_labels")

    def reload(self):
        name = self.im["name"]
        self.load_state()
        self.apply_filter(keep_name=name)
        self.dirty = True

    def toggle_help(self):
        if self.help.visible:
            self.help.hide()
        else:
            self.help.show()
        self.dirty = True

    # ------------------------------------------------------------ drawing
    def _render(self):
        surf = self.canvas_surf
        surf.fill((25, 25, 28))
        im = self.im
        try:
            img = self.cache.get(self.img_path(im["name"]))
        except IOError as e:
            R.label(surf, self.font, str(e), (20, 20), R.RED)
            return
        scale = self.fit_scale() * self.zoom
        vw, vh = self.canvas.w / scale, self.canvas.h / scale
        x0 = int(self.center[0] - vw / 2)
        y0 = int(self.center[1] - vh / 2)
        x1, y1 = int(x0 + vw) + 1, int(y0 + vh) + 1
        crop = crop_padded(img, x0, y0, x1, y1)
        out_w = max(1, int(round((x1 - x0) * scale)))
        out_h = max(1, int(round((y1 - y0) * scale)))
        surf.blit(R.to_surface(crop, (out_w, out_h)), (0, 0))

        def sc(p):
            return (p[0] - x0) * scale, (p[1] - y0) * scale

        for t in self.by_image.get(im["name"], []):
            st = t["status"]
            if st in ACCEPTED and self.show_acc and t["px"]:
                pos = sc(t["px"])
                R.cross(surf, pos, R.STATUS_COLOR[st], 14, 2)
            elif st == "pending" and self.show_pend and t["pred"]:
                pos = sc(t["pred"])
                R.ring(surf, pos, R.YELLOW, 14, 2)
            elif st == "absent" and self.show_abs and t["pred"]:
                pos = sc(t["pred"])
                R.xmark(surf, pos, R.RED, 10, 2)
            else:
                continue
            if self.show_labels:
                R.label(surf, self.font, f"{t['gcp']} ({st})",
                        (pos[0] + 16, pos[1] - 9), R.STATUS_COLOR.get(st, R.WHITE))
        R.label(surf, self.font,
                f"{im['name']}   {self.idx + 1}/{len(self.visible)}   zoom {self.zoom:.1f}x",
                (8, 8))

    def _update_info(self):
        im = self.im
        tags = sorted((t for t in self.by_image.get(im["name"], []) if t["status"] != "outside"),
                      key=lambda t: t["gcp"])
        lines = [
            f"<b>{im['name']}</b><br>",
            f"Photo {self.idx + 1} of {len(self.visible)} ({FILTERS[self.filter]})<br>",
            f"Size {im['width']} x {im['height']}, heading {im.get('yaw', 0):.1f} deg "
            f"({im.get('yaw_source', 'exif')})<br>",
            f"Flight position offset E {self.offset[0]:+.2f} m, N {self.offset[1]:+.2f} m<br><br>",
        ]
        if not tags:
            lines.append("No GCPs predicted in this photo.")
        for t in tags:
            col = R.STATUS_HEX.get(t["status"], "#FFFFFF")
            p = t["px"] or t["pred"]
            where = f" at {p[0]:.0f}, {p[1]:.0f}" if p else ""
            score = f", score {t['score']:.2f}" if t.get("score") is not None else ""
            lines.append(f"<font color='{col}'><b>{t['gcp']}</b> {t['status']}</font>"
                         f"{where}{score}<br>")
        n_acc = sum(1 for ts in self.by_image.values() for t in ts if t["status"] in ACCEPTED)
        n_all = sum(1 for ts in self.by_image.values() for t in ts if t["status"] != "outside")
        lines.append(f"<br>All photos: {n_acc} of {n_all} GCP-photo pairs tagged")
        self.info.set_text("".join(lines))

    # ------------------------------------------------------------ events
    def _on_key(self, e) -> bool:
        k = e.key
        if k in (pygame.K_q, pygame.K_ESCAPE):
            if k == pygame.K_ESCAPE and self.help.visible:
                self.toggle_help()
                return True
            return False
        actions = {
            pygame.K_RIGHT: lambda: self.go(self.idx + 1),
            pygame.K_LEFT: lambda: self.go(self.idx - 1),
            pygame.K_HOME: lambda: self.go(0),
            pygame.K_END: lambda: self.go(len(self.visible) - 1),
            pygame.K_PAGEDOWN: lambda: self.go(self.idx + 10),
            pygame.K_PAGEUP: lambda: self.go(self.idx - 10),
            pygame.K_PLUS: lambda: self.zoom_at(1.25), pygame.K_EQUALS: lambda: self.zoom_at(1.25),
            pygame.K_KP_PLUS: lambda: self.zoom_at(1.25),
            pygame.K_MINUS: lambda: self.zoom_at(0.8), pygame.K_KP_MINUS: lambda: self.zoom_at(0.8),
            pygame.K_f: self.reset_view, pygame.K_0: self.reset_view, pygame.K_KP0: self.reset_view,
            pygame.K_TAB: self.next_tag, pygame.K_t: self.cycle_filter,
            pygame.K_1: lambda: self.toggle("show_acc"),
            pygame.K_2: lambda: self.toggle("show_pend"),
            pygame.K_3: lambda: self.toggle("show_abs"),
            pygame.K_l: self.toggle_labels, pygame.K_r: self.reload,
            pygame.K_h: self.toggle_help, pygame.K_F1: self.toggle_help,
        }
        fn = actions.get(k)
        if fn:
            fn()
        return True

    def handle(self, e) -> bool:
        running = True
        if e.type == pygame.QUIT:
            return False
        if e.type == pygame.KEYDOWN:
            running = self._on_key(e)
        elif e.type == pygame.MOUSEWHEEL and self.canvas.collidepoint(pygame.mouse.get_pos()):
            self.zoom_at(1.25 if e.y > 0 else 0.8, pygame.mouse.get_pos())
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.canvas.collidepoint(e.pos):
            self.drag = (e.pos, list(self.center))
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
        elif e.type == pygame.MOUSEMOTION and self.drag:
            (sx, sy), c0 = self.drag
            scale = self.fit_scale() * self.zoom
            self.center = [c0[0] - (e.pos[0] - sx) / scale, c0[1] - (e.pos[1] - sy) / scale]
            self.dirty = True
        elif e.type == pygame_gui.UI_BUTTON_PRESSED and e.ui_element in self.buttons:
            fn = self.buttons[e.ui_element]
            if fn is None:
                return False
            fn()
        self.ui.process_events(e)
        return running

    def draw(self):
        if self.dirty:
            self._render()
            self._update_info()
            self.dirty = False
        self.screen.blit(self.canvas_surf, (0, 0))
        self.ui.draw_ui(self.screen)

    def run(self):
        clock = pygame.time.Clock()
        running = True
        while running:
            dt = clock.tick(60) / 1000.0
            for e in pygame.event.get():
                if not self.handle(e):
                    running = False
                    break
            self.ui.update(dt)
            self.draw()
            pygame.display.flip()
        pygame.quit()
