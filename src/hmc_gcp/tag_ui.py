"""Review window: one crop per GCP-photo pair; accept, correct or reject."""

from __future__ import annotations

import time

import pygame
import pygame_gui
from pygame_gui.elements import UIButton, UIPanel, UITextBox

from . import render as R
from .imgio import crop_padded
from .session import ACCEPTED, Session

PANEL_W = 380

HELP = (
    "<b>hmc-gcp tag: keys</b><br><br>"
    "<b>Enter</b> or <b>A</b>: accept point, go to next unreviewed<br>"
    "<b>X</b> or <b>Delete</b>: target not visible, go to next unreviewed<br>"
    "<b>Left click</b>: place the point by hand<br>"
    "<b>Shift + click</b>: place by hand and accept<br>"
    "<b>I J K L</b>: nudge point up/left/down/right 1 px (Shift: 5 px)<br>"
    "<b>Right / Left</b>: next / previous pair<br>"
    "<b>N</b>: next unreviewed pair<br>"
    "<b>PgDn / PgUp</b>: next / previous GCP<br>"
    "<b>U</b>: reset this pair to unreviewed<br>"
    "<b>R</b>: run detection again<br>"
    "<b>O</b> or <b>Tab</b>: switch detail / overview<br>"
    "<b>+ / -</b> or mouse wheel: zoom<br>"
    "<b>S</b>: save and export<br>"
    "<b>H</b> or <b>F1</b>: show / hide this help<br>"
    "<b>Q</b> or <b>Esc</b>: save, export and quit<br><br>"
    "Yellow ring: search area around the prediction. Green box: detected target.<br>"
    "Orange box: weak candidate (no centre dot); the point stays on the prediction.<br>"
    "White cross: point to accept. Green: accepted. Cyan: placed by hand.<br>"
    "Orange: other GCPs predicted in this photo."
)


class TagApp:
    def __init__(self, session: Session, size=(1500, 950)):
        pygame.display.init()
        pygame.font.init()
        self.s = session
        self.W, self.H = size
        self.screen = pygame.display.set_mode(size)
        pygame.display.set_caption("hmc-gcp tag")
        self.ui = pygame_gui.UIManager(size)
        self.ui.preload_fonts([{'name': 'noto_sans', 'point_size': 14, 'style': 'bold',
                               'antialiased': '1'}])
        self.canvas = pygame.Rect(0, 0, self.W - PANEL_W, self.H)
        self.canvas_surf = pygame.Surface(self.canvas.size)
        self.font = pygame.font.Font(None, 22)
        self.items = session.items
        self.idx = self._next_pending(-1, wrap=True)
        if self.idx is None:
            self.idx = 0
        self.overview = False
        self.zoom = 1.0
        self.cur = None
        self.edited = False
        self.view = None
        self.msg, self.msg_t = "", 0.0
        self._build_ui()
        self.dirty = True

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        x0 = self.W - PANEL_W
        self.panel = UIPanel(pygame.Rect(x0, 0, PANEL_W, self.H), manager=self.ui)
        self.info = UITextBox("", pygame.Rect(8, 8, PANEL_W - 22, 430),
                              manager=self.ui, container=self.panel)
        specs = [
            ("Accept  [Enter]", self.accept), ("Not visible  [X]", self.absent),
            ("< Prev  [Left]", lambda: self.step(-1)),
            ("Next >  [Right]", lambda: self.step(1)),
            ("Next unreviewed  [N]", self.next_pending), ("Reset  [U]", self.reset),
            ("Prev GCP  [PgUp]", lambda: self.jump_gcp(-1)),
            ("Next GCP  [PgDn]", lambda: self.jump_gcp(1)),
            ("Detect again  [R]", self.redetect), ("Overview  [O]", self.toggle_overview),
            ("Zoom in  [+]", lambda: self.zoom_by(1.25)), ("Zoom out  [-]", lambda: self.zoom_by(0.8)),
            ("Save + export  [S]", self.save_export), ("Help  [H]", self.toggle_help),
            ("Quit  [Q]", None),
        ]
        self.buttons = {}
        bw, bh, gap = (PANEL_W - 30) // 2, 34, 6
        y = 448
        for i, (text, fn) in enumerate(specs):
            col, row = i % 2, i // 2
            b = UIButton(pygame.Rect(8 + col * (bw + gap), y + row * (bh + gap), bw, bh),
                         text, manager=self.ui, container=self.panel)
            self.buttons[b] = fn
        self.help = UITextBox(HELP, pygame.Rect(40, 40, 620, 560), manager=self.ui)
        self.help.hide()

    def flash(self, msg: str):
        self.msg, self.msg_t = msg, time.time()
        self.dirty = True

    # ------------------------------------------------------------ navigation
    @property
    def it(self):
        return self.items[self.idx]

    def _next_pending(self, start: int, wrap=True):
        n = len(self.items)
        rng = range(start + 1, start + 1 + n) if wrap else range(start + 1, n)
        for k in rng:
            if self.items[k % n].status == "pending":
                return k % n
        return None

    def go(self, idx: int):
        if not self.items:
            return
        self.idx = max(0, min(len(self.items) - 1, idx))
        self.show()

    def step(self, d: int):
        """Move to the next/previous pair, skipping pairs predicted outside the photo."""
        k = self.idx + d
        while 0 <= k < len(self.items) and self.items[k].status == "outside":
            k += d
        if 0 <= k < len(self.items):
            self.go(k)

    def next_pending(self):
        k = self._next_pending(self.idx)
        if k is None:
            self.flash("All pairs reviewed. Press S to export, Q to quit.")
            self.step(1)
        else:
            self.go(k)

    def jump_gcp(self, step: int):
        cur = self.it.gcp
        k = self.idx
        if step > 0:
            while k < len(self.items) and self.items[k].gcp == cur:
                k += 1
        else:
            while k > 0 and self.items[k].gcp == cur:
                k -= 1
            g = self.items[k].gcp
            while k > 0 and self.items[k - 1].gcp == g:
                k -= 1
        if k >= len(self.items):
            return
        g = self.items[k].gcp
        j = k
        while j < len(self.items) and self.items[j].gcp == g and self.items[j].status == "outside":
            j += 1
        if j < len(self.items) and self.items[j].gcp == g:
            k = j
        self.go(k)

    def show(self):
        it = self.it
        if self.s.needs_detect(it):
            try:
                self.s.detect(it)
            except IOError as e:
                self.flash(str(e))
        pred, _, _ = self.s.predict(it)
        if it.px:
            self.cur = list(it.px)
        elif self._strong(it.det):
            self.cur = list(it.det["center"])
        else:
            self.cur = list(pred)
        self.edited = it.status == "manual"
        self.dirty = True

    @staticmethod
    def _strong(det) -> bool:
        """A detection good enough to put the point on: centre dot found, or high score."""
        return bool(det and det.get("center")
                    and (det.get("has_dot") or (det.get("score") or 0) >= 0.6))

    # ------------------------------------------------------------ actions
    def _after_change(self):
        self.s.update_offsets()
        self.s.save()

    def accept(self):
        it = self.it
        it.px = [float(self.cur[0]), float(self.cur[1])]
        it.status = "manual" if self.edited else "accepted"
        it.user = True
        self._after_change()
        self.flash(f"{it.gcp} in {it.image}: {it.status}")
        self.next_pending()

    def absent(self):
        it = self.it
        it.status, it.px, it.user = "absent", None, True
        self._after_change()
        self.flash(f"{it.gcp} in {it.image}: not visible")
        self.next_pending()

    def reset(self):
        it = self.it
        it.status, it.px, it.det, it.user = "pending", None, None, True
        self._after_change()
        self.show()

    def redetect(self):
        it = self.it
        if it.status == "pending":
            it.det = None
        else:
            self.s.detect(it)
        self.show()
        if it.status != "pending" and it.det.get("center"):
            self.cur, self.edited = list(it.det["center"]), False
        self.flash(f"detection score {self.it.score or 0:.2f}")

    def nudge(self, dx, dy):
        self.cur[0] += dx
        self.cur[1] += dy
        self.edited = True
        self.dirty = True

    def toggle_overview(self):
        self.overview = not self.overview
        self.zoom = 1.0
        self.dirty = True

    def zoom_by(self, f):
        self.zoom = max(0.2, min(20.0, self.zoom * f))
        self.dirty = True

    def toggle_help(self):
        if self.help.visible:
            self.help.hide()
        else:
            self.help.show()
        self.dirty = True

    def save_export(self):
        self.s.save()
        r = self.s.export()
        self.flash(f"Exported: {r['control']} control, {r['checkpoints']} checkpoints, "
                   f"{r['untagged']} untagged")

    # ------------------------------------------------------------ drawing
    def _render_canvas(self):
        surf = self.canvas_surf
        surf.fill((25, 25, 28))
        if not self.items:
            R.label(surf, self.font, "No GCP is predicted inside any photo.", (20, 20))
            return
        it = self.it
        sh = self.s.shots[it.image]
        pred, rad, exp = self.s.predict(it)
        try:
            img = self.s.cache.get(sh.path)
        except IOError as e:
            R.label(surf, self.font, str(e), (20, 20), R.RED)
            return
        if self.overview:
            hs = rad * 1.15 / self.zoom
            c = pred
        else:
            hs = max(5 * exp, 40) / self.zoom
            c = self.cur
        hs = max(8, int(hs))
        x0, y0 = int(round(c[0])) - hs, int(round(c[1])) - hs
        crop = crop_padded(img, x0, y0, x0 + 2 * hs, y0 + 2 * hs)
        side = min(self.canvas.w, self.canvas.h) - 20
        scale = side / (2 * hs)
        ox = (self.canvas.w - side) // 2
        oy = (self.canvas.h - side) // 2
        surf.blit(R.to_surface(crop, (side, side)), (ox, oy))
        self.view = (x0, y0, scale, ox, oy, side)

        def sc(p):
            return ox + (p[0] - x0) * scale, oy + (p[1] - y0) * scale

        clip = pygame.Rect(ox, oy, side, side)
        surf.set_clip(clip)
        R.ring(surf, sc(pred), R.YELLOW, rad * scale, 2)
        R.cross(surf, sc(pred), R.YELLOW, 6, 1)
        for other in self.items:
            if other.image == it.image and other.gcp != it.gcp and other.pred:
                p = other.px or other.pred
                col = R.STATUS_COLOR.get(other.status, R.ORANGE) if other.px else R.ORANGE
                R.ring(surf, sc(p), col, 8, 2)
                R.label(surf, self.font, other.gcp, (sc(p)[0] + 10, sc(p)[1] - 8), col)
        if it.det and it.det.get("box"):
            # green: likely target; orange: weak candidate (no centre dot, low score)
            pts = [sc(p) for p in it.det["box"]]
            pygame.draw.polygon(surf, R.BLACK, pts, 4)
            pygame.draw.polygon(surf, R.GREEN if self._strong(it.det) else R.ORANGE, pts, 2)
        col = R.WHITE
        if it.status in ACCEPTED and not self.edited and it.px and \
                abs(it.px[0] - self.cur[0]) < 1e-6 and abs(it.px[1] - self.cur[1]) < 1e-6:
            col = R.STATUS_COLOR[it.status]
        elif self.edited:
            col = R.CYAN
        R.cross(surf, sc(self.cur), col, 16, 2)
        surf.set_clip(None)
        pygame.draw.rect(surf, R.GREY, clip, 1)
        mode = "overview" if self.overview else "detail"
        R.label(surf, self.font, f"{it.gcp}  in  {it.image}   ({mode}, zoom {self.zoom:.2f})",
                (ox + 6, oy + 6))
        if self.msg and time.time() - self.msg_t < 6:
            R.label(surf, self.font, self.msg, (ox + 6, oy + side - 24), R.CYAN)

    def _update_info(self):
        if not self.items:
            self.info.set_text("No pairs to review.")
            return
        it = self.it
        sh = self.s.shots[it.image]
        g = self.s.gcp_by[it.gcp]
        pred, rad, exp = self.s.predict(it)
        gsd = self.s.gsd(g, sh)
        counts = {k: sum(x.status == k for x in self.items)
                  for k in ("pending", "accepted", "auto", "manual", "absent", "outside")}
        n_g = sum(1 for x in self.items if x.gcp == it.gcp and x.status in ACCEPTED)
        det = it.det or {}
        dot = "yes" if det.get("has_dot") else "no"
        score = f"{it.score:.2f}" if it.score is not None else "-"
        stat_col = R.STATUS_HEX.get(it.status, "#FFFFFF")
        off = self.s.offset(it.image)
        html = (
            f"<font color='#FFDC28'>to review {counts['pending']}</font>, "
            f"<font color='#3CE65A'>accepted {counts['accepted'] + counts['auto']}</font>, "
            f"<font color='#3CDCFF'>by hand {counts['manual']}</font>, "
            f"<font color='#FF4646'>not visible {counts['absent']}</font>, "
            f"<font color='#969696'>outside {counts['outside']}</font><br><br>"
            f"<b>Pair {self.idx + 1} / {len(self.items)}</b><br>"
            f"GCP <b>{it.gcp}</b>  (z {g['z']:.2f})<br>"
            f"Photo <b>{it.image}</b><br>"
            f"Status <font color='{stat_col}'><b>{it.status}</b></font><br>"
            f"Detection score {score}, centre dot {dot}<br>"
            f"Point x {self.cur[0]:.1f}, y {self.cur[1]:.1f}"
            f"{' (edited)' if self.edited else ''}<br>"
            f"GSD {gsd * 100:.2f} cm, target ~{exp:.0f} px<br>"
            f"Search radius {rad * gsd:.1f} m ({rad:.0f} px)<br>"
            f"Position correction E {off[0]:+.2f} m, N {off[1]:+.2f} m<br>"
            f"Heading from {sh.yaw_source} ({sh.yaw:.1f} deg)<br>"
            f"{self._flight_line(sh.flight)}<br>"
            f"This GCP tagged in {n_g} photos"
        )
        self.info.set_text(html)

    def _flight_line(self, f: int) -> str:
        fl = self.s.flights[f]
        name = f"Flight {f}" if f else "No capture time"
        if fl["tags"]:
            sig = f" +/- {fl['sigma']:.1f}" if fl["sigma"] is not None else ""
            return (f"{name}: take-off {self.s.flight_takeoff(f):.2f}{sig} m "
                    f"({fl['tags']} tags)")
        return f"{name}: take-off {self.s.flight_takeoff(f):.2f} m (not learned yet)"

    # ------------------------------------------------------------ events
    def _on_key(self, e) -> bool:
        k, shift = e.key, bool(e.mod & pygame.KMOD_SHIFT)
        step = 5 if shift else 1
        if k in (pygame.K_q, pygame.K_ESCAPE):
            if k == pygame.K_ESCAPE and self.help.visible:
                self.toggle_help()
                return True
            return False
        actions = {
            pygame.K_RETURN: self.accept, pygame.K_KP_ENTER: self.accept, pygame.K_a: self.accept,
            pygame.K_x: self.absent, pygame.K_DELETE: self.absent,
            pygame.K_RIGHT: lambda: self.step(1),
            pygame.K_LEFT: lambda: self.step(-1),
            pygame.K_n: self.next_pending,
            pygame.K_PAGEDOWN: lambda: self.jump_gcp(1),
            pygame.K_PAGEUP: lambda: self.jump_gcp(-1),
            pygame.K_u: self.reset, pygame.K_r: self.redetect,
            pygame.K_o: self.toggle_overview, pygame.K_TAB: self.toggle_overview,
            pygame.K_PLUS: lambda: self.zoom_by(1.25), pygame.K_EQUALS: lambda: self.zoom_by(1.25),
            pygame.K_KP_PLUS: lambda: self.zoom_by(1.25),
            pygame.K_MINUS: lambda: self.zoom_by(0.8), pygame.K_KP_MINUS: lambda: self.zoom_by(0.8),
            pygame.K_s: self.save_export,
            pygame.K_h: self.toggle_help, pygame.K_F1: self.toggle_help,
            pygame.K_i: lambda: self.nudge(0, -step), pygame.K_k: lambda: self.nudge(0, step),
            pygame.K_j: lambda: self.nudge(-step, 0), pygame.K_l: lambda: self.nudge(step, 0),
        }
        fn = actions.get(k)
        if fn and self.items:
            fn()
        return True

    def _on_click(self, e):
        if not self.view or self.help.visible or not self.items:
            return
        x0, y0, scale, ox, oy, side = self.view
        if not pygame.Rect(ox, oy, side, side).collidepoint(e.pos):
            return
        self.cur = [x0 + (e.pos[0] - ox) / scale, y0 + (e.pos[1] - oy) / scale]
        self.edited = True
        self.dirty = True
        if pygame.key.get_mods() & pygame.KMOD_SHIFT:
            self.accept()

    def handle(self, e) -> bool:
        """Process one event. Returns False to quit."""
        running = True
        if e.type == pygame.QUIT:
            return False
        if e.type == pygame.KEYDOWN:
            running = self._on_key(e)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.canvas.collidepoint(e.pos):
            self._on_click(e)
        elif e.type == pygame.MOUSEWHEEL and self.canvas.collidepoint(pygame.mouse.get_pos()):
            self.zoom_by(1.25 if e.y > 0 else 0.8)
        elif e.type == pygame_gui.UI_BUTTON_PRESSED and e.ui_element in self.buttons:
            fn = self.buttons[e.ui_element]
            if fn is None:
                return False
            if self.items:
                fn()
        self.ui.process_events(e)
        return running

    def draw(self):
        if self.dirty:
            self._render_canvas()
            self._update_info()
            self.dirty = False
        self.screen.blit(self.canvas_surf, (0, 0))
        self.ui.draw_ui(self.screen)

    def run(self):
        clock = pygame.time.Clock()
        if self.items:
            self.show()
        running = True
        while running:
            dt = clock.tick(60) / 1000.0
            for e in pygame.event.get():
                if not self.handle(e):
                    running = False
                    break
            if self.msg and time.time() - self.msg_t > 6:
                self.msg = ""
                self.dirty = True
            self.ui.update(dt)
            self.draw()
            pygame.display.flip()
        self.s.save()
        r = self.s.export()
        print(f"Saved. Exported {r['control']} control GCPs, {r['checkpoints']} checkpoints, "
              f"{r['untagged']} untagged to {self.s.out}")
        pygame.quit()
