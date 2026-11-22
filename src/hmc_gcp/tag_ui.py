"""Review window: one crop per GCP-photo pair; accept, correct or reject."""

from __future__ import annotations

import math
import time

import pygame
import pygame_gui
from pygame_gui.elements import UIButton, UIPanel, UITextBox

from . import render as R
from .imgio import crop_padded
from .session import ACCEPTED, Session

PANEL_W = 380
INFO_H = 372
ROW_H = 20
MAX_MAG = 40.0  # most screen pixels per photo pixel
MIN_MAG_FIT = 0.5  # zoom out to half the whole-photo fit

HELP = (
    "<b>hmc-gcp tag: keys</b><br><br>"
    "<b>Enter</b> or <b>A</b>: accept point, go to next unreviewed<br>"
    "<b>X</b> or <b>Delete</b>: target not visible, go to next unreviewed<br>"
    "<b>Right click</b>: place the point by hand<br>"
    "<b>Shift + right click</b>: place by hand and accept<br>"
    "<b>Left drag</b>: pan the view<br>"
    "<b>I J K L</b>: nudge point up/left/down/right 1 px (Shift: 5 px)<br>"
    "<b>Right / Left</b>: next / previous pair<br>"
    "<b>N</b>: next unreviewed pair<br>"
    "<b>Pair list</b> (bottom right): click a pair to open it; wheel or the bar to scroll<br>"
    "<b>PgDn / PgUp</b>: next / previous GCP<br>"
    "<b>U</b>: reset this pair to unreviewed<br>"
    "<b>R</b>: run detection again<br>"
    "<b>O</b> or <b>Tab</b>: switch detail / overview<br>"
    "<b>F</b>: fit the whole photo in the window (F again: back)<br>"
    "<b>V</b>: show / hide rejected (not visible) pairs when stepping<br>"
    "<b>+ / -</b> or mouse wheel: zoom (the wheel zooms at the mouse)<br>"
    "<b>S</b>: save and export<br>"
    "<b>H</b> or <b>F1</b>: show / hide this help<br>"
    "<b>Q</b> or <b>Esc</b>: save, export and quit<br><br>"
    "<b>Colours</b><br>"
    "<font color='#FFDC28'><b>Yellow ring + small cross</b></font>: search area and "
    "predicted position<br>"
    "<font color='#3CE65A'><b>Green box</b></font>: detected target<br>"
    "<font color='#FF9628'><b>Orange box</b></font>: weak candidate (no centre dot); "
    "the point stays on the prediction<br>"
    "<font color='#F5F5F5'><b>White cross</b></font>: point to accept<br>"
    "<font color='#3CE65A'><b>Green cross / mark</b></font>: accepted (by you or "
    "automatically)<br>"
    "<font color='#3CDCFF'><b>Cyan cross / mark</b></font>: placed or moved by hand<br>"
    "<font color='#BE5AFF'><b>Purple mark</b></font>: other GCPs from the GeoPackage, "
    "projected into this photo<br>"
    "<font color='#FF4646'><b>Red</b></font> (info panel): not visible; "
    "<font color='#969696'><b>grey</b></font>: parked outside the photo"
)


class TagApp:
    def __init__(self, session: Session, size=(1500, 950), show_rejected: bool = False):
        pygame.display.init()
        pygame.font.init()
        self.s = session
        self.show_rejected = show_rejected
        self.W, self.H = size
        self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        pygame.display.set_caption("hmc-gcp tag")
        self.ui = pygame_gui.UIManager(size)
        self.ui.preload_fonts([{'name': 'noto_sans', 'point_size': 14, 'style': 'bold',
                               'antialiased': '1'}])
        self.canvas = pygame.Rect(0, 0, self.W - PANEL_W, self.H)
        self.canvas_surf = pygame.Surface(self.canvas.size)
        self.font = pygame.font.Font(None, 22)
        self.list_font = pygame.font.Font(None, 20)
        self.list_scroll = 0  # pixels
        self.list_drag = False
        self.list_rect = pygame.Rect(0, 0, 0, 0)
        self.items = session.items
        self.idx = self._next_pending(-1, wrap=True)
        if self.idx is None:
            self.idx = 0
        self.overview = False
        self.fit = False  # whole photo (F)
        self.zoom = 1.0
        self.view_center = None  # set by panning; None = follow the point / prediction
        self.drag = None
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
        self.info = UITextBox("", pygame.Rect(8, 8, PANEL_W - 22, INFO_H),
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
            ("Rejected  [V]", self.toggle_rejected), ("Whole photo  [F]", self.toggle_fit),
            ("Quit  [Q]", None),
        ]
        self.buttons = {}
        bw, bh, gap = (PANEL_W - 30) // 2, 34, 6
        y = INFO_H + 18
        for i, (text, fn) in enumerate(specs):
            col, row = i % 2, i // 2
            b = UIButton(pygame.Rect(8 + col * (bw + gap), y + row * (bh + gap), bw, bh),
                         text, manager=self.ui, container=self.panel)
            self.buttons[b] = fn
        self.list_top = y + ((len(specs) + 1) // 2) * (bh + gap) + 4
        self._layout_list()
        self.help = UITextBox(HELP, pygame.Rect(40, 20, 680, 810), manager=self.ui)
        self.help.hide()

    # ------------------------------------------------------------ pair list
    def _layout_list(self):
        x = self.W - PANEL_W + 8
        self.list_rect = pygame.Rect(x, self.list_top, PANEL_W - 16,
                                     max(0, self.H - self.list_top - 8))

    def _list_rows(self) -> list[int]:
        """Item indices shown in the list: everything not hidden, plus the current pair."""
        return [k for k, it in enumerate(self.items) if not self._hidden(it) or k == self.idx]

    def _list_max_scroll(self, n_rows: int) -> int:
        return max(0, n_rows * ROW_H - self.list_rect.h)

    def _list_follow(self):
        """Scroll the list so the current pair is visible."""
        rows = self._list_rows()
        if self.idx not in rows or self.list_rect.h < ROW_H:
            return
        y = rows.index(self.idx) * ROW_H
        if y < self.list_scroll or y + ROW_H > self.list_scroll + self.list_rect.h:
            self.list_scroll = y - self.list_rect.h // 2
        self.list_scroll = max(0, min(self._list_max_scroll(len(rows)), self.list_scroll))

    def _list_scroll_to(self, my: int):
        """Scroll bar: put the list position under the mouse's y."""
        rows = self._list_rows()
        frac = (my - self.list_rect.y) / max(1, self.list_rect.h)
        self.list_scroll = int(max(0.0, min(1.0, frac)) * self._list_max_scroll(len(rows)))

    def _list_click(self, pos) -> bool:
        r = self.list_rect
        if self.help.visible or not r.collidepoint(pos) or not self.items:
            return False
        if pos[0] >= r.right - 10:
            self.list_drag = True
            self._list_scroll_to(pos[1])
            return True
        rows = self._list_rows()
        k = (pos[1] - r.y + self.list_scroll) // ROW_H
        if 0 <= k < len(rows):
            self.go(rows[k])
        return True

    def _draw_list(self):
        r = self.list_rect
        if r.h < ROW_H or self.help.visible:
            return
        scr = self.screen
        pygame.draw.rect(scr, (22, 24, 28), r)
        rows = self._list_rows()
        self.list_scroll = max(0, min(self._list_max_scroll(len(rows)), self.list_scroll))
        scr.set_clip(r)
        first = self.list_scroll // ROW_H
        for n in range(first, min(len(rows), first + r.h // ROW_H + 2)):
            k = rows[n]
            it = self.items[k]
            y = r.y + n * ROW_H - self.list_scroll
            if k == self.idx:
                pygame.draw.rect(scr, (70, 74, 90), (r.x, y, r.w - 10, ROW_H))
            col = R.STATUS_COLOR.get(it.status, R.WHITE)
            for text, dx in ((f"{k + 1}", 6), (R.short_name(it.gcp), 46),
                             (R.short_name(it.image), 130), (it.status, 210)):
                scr.blit(self.list_font.render(text, True, col), (r.x + dx, y + 3))
        if len(rows) * ROW_H > r.h:
            th = max(20, r.h * r.h // (len(rows) * ROW_H))
            ty = r.y + (r.h - th) * self.list_scroll // max(1, self._list_max_scroll(len(rows)))
            pygame.draw.rect(scr, (45, 48, 56), (r.right - 8, r.y, 8, r.h))
            pygame.draw.rect(scr, (130, 135, 150), (r.right - 8, ty, 8, th))
        scr.set_clip(None)
        pygame.draw.rect(scr, (80, 84, 96), r, 1)

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
        self._list_follow()

    def _hidden(self, it) -> bool:
        """Pairs skipped when stepping: parked outside the photo, and rejected ones
        unless shown with V."""
        return it.status == "outside" or (it.status == "absent" and not self.show_rejected)

    def toggle_rejected(self):
        self.show_rejected = not self.show_rejected
        self.flash("Rejected pairs " + ("shown" if self.show_rejected else "hidden")
                   + " when stepping")

    def step(self, d: int):
        """Move to the next/previous pair, skipping hidden pairs."""
        k = self.idx + d
        while 0 <= k < len(self.items) and self._hidden(self.items[k]):
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
        while j < len(self.items) and self.items[j].gcp == g and self._hidden(self.items[j]):
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
        self.view_center = None
        self.dirty = True

    @staticmethod
    def _strong(det) -> bool:
        """A detection good enough to put the point on: centre dot found, or high score."""
        return bool(det and det.get("center")
                    and (det.get("has_dot") or (det.get("score") or 0) >= 0.6))

    def _point_color(self):
        """Colour of the current point: cyan by hand, green accepted or strong detection,
        orange weak detection, red not visible."""
        it = self.it
        if self.edited:
            return R.CYAN
        if it.status in ACCEPTED and it.px and abs(it.px[0] - self.cur[0]) < 1e-6 \
                and abs(it.px[1] - self.cur[1]) < 1e-6:
            return R.STATUS_COLOR[it.status]
        if it.status == "absent":
            return R.RED
        if self._strong(it.det):
            return R.GREEN
        return R.ORANGE if it.det else R.WHITE

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

    def toggle_fit(self):
        self.fit = not self.fit
        self.zoom = 1.0
        self.view_center = None
        self.dirty = True

    def toggle_overview(self):
        self.overview = not self.overview
        self.fit = False
        self.zoom = 1.0
        self.view_center = None
        self.dirty = True

    def zoom_by(self, f, screen_pos=None):
        """Zoom; with screen_pos, keep the image point under the mouse where it is."""
        if not self.view:
            self.zoom *= f
            self.dirty = True
            return
        # limit the real magnification (same limits for every photo), not the factor
        cx, cy, scale, ox, oy, vw, vh = self.view
        lo, hi = self._mag_limits()
        new_scale = max(lo, min(hi, scale * f))
        if abs(new_scale - scale) < 1e-9:
            return
        self.zoom *= new_scale / scale
        if screen_pos is not None:
            dx, dy = screen_pos[0] - ox - vw / 2, screen_pos[1] - oy - vh / 2
            self.view_center = [cx + dx / scale - dx / new_scale,
                                cy + dy / scale - dy / new_scale]
        self.dirty = True

    def _mag_limits(self):
        """Allowed magnification: from half the whole-photo fit up to MAX_MAG."""
        sh = self.s.shots[self.it.image]
        vw, vh = self.canvas.w - 20, self.canvas.h - 20
        fit = min(vw / sh.width, vh / sh.height)
        return min(fit * MIN_MAG_FIT, MAX_MAG), MAX_MAG

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
        # the image area fills the canvas; hs is the half-size of its shorter side
        ox, oy = 10, 10
        vw, vh = self.canvas.w - 20, self.canvas.h - 20
        if self.fit:
            scale = min(vw / sh.width, vh / sh.height) * self.zoom
            c = [sh.width / 2, sh.height / 2]
        else:
            scale = min(vw, vh) / (2 * hs)
        # keep the zoom inside the same limits on every photo
        lo, hi = self._mag_limits()
        if not lo <= scale <= hi:
            clamped = max(lo, min(hi, scale))
            self.zoom *= clamped / scale
            scale = clamped
        if self.view_center is not None:
            c = self.view_center
        cx, cy = float(c[0]), float(c[1])
        self.view = (cx, cy, scale, ox, oy, vw, vh)

        def sc(p):
            return ox + vw / 2 + (p[0] - cx) * scale, oy + vh / 2 + (p[1] - cy) * scale

        clip = pygame.Rect(ox, oy, vw, vh)
        surf.set_clip(clip)
        # crop only the visible part of the photo; outside it stays background
        hx, hy = vw / 2 / scale, vh / 2 / scale
        x0 = max(0, int(math.floor(cx - hx)))
        y0 = max(0, int(math.floor(cy - hy)))
        x1 = min(sh.width, int(math.ceil(cx + hx)) + 1)
        y1 = min(sh.height, int(math.ceil(cy + hy)) + 1)
        if x1 > x0 and y1 > y0:
            crop = crop_padded(img, x0, y0, x1, y1)
            size = (max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale)))
            sx, sy = sc((x0, y0))
            surf.blit(R.to_surface(crop, size), (round(sx), round(sy)))
        R.ring(surf, sc(pred), R.YELLOW, rad * scale, 2)
        R.cross(surf, sc(pred), R.YELLOW, 6, 1)
        # every other GCP from the GeoPackage, projected into this photo (purple);
        # GCPs already tagged in this photo are drawn at their tag in their status colour
        tagged = {o.gcp: o for o in self.items
                  if o.image == it.image and o.px and o.status in ACCEPTED}
        off = self.s.offset(it.image)
        for g in self.s.gcps:
            if g["name"] == it.gcp:
                continue
            if g["name"] in tagged:
                p, col = tagged[g["name"]].px, R.STATUS_COLOR[tagged[g["name"]].status]
            else:
                p = self.s.project(g, sh, off)
                if p is None:
                    continue
                col = R.PURPLE
            R.ring(surf, sc(p), col, 8, 2)
            R.label(surf, self.font, g["name"], (sc(p)[0] + 10, sc(p)[1] - 8), col)
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
        pcol = self._point_color()
        cx_, cy_ = sc(self.cur)
        R.label(surf, self.font, R.short_name(it.gcp), (cx_ + 20, cy_ - 30), pcol, underline=True)
        surf.set_clip(None)
        pygame.draw.rect(surf, R.GREY, clip, 1)
        mode = "whole photo" if self.fit else ("overview" if self.overview else "detail")
        R.label(surf, self.font, f"{R.short_name(it.gcp)} in {R.short_name(it.image, ext=True)}   ({mode}, {scale:.2f}x)",
                (ox + 6, oy + 6), pcol)
        if self.msg and time.time() - self.msg_t < 6:
            R.label(surf, self.font, self.msg, (ox + 6, oy + vh - 24), R.CYAN)

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
            f"<font color='#FFDC28'>review {counts['pending']}</font>  "
            f"<font color='#3CE65A'>ok {counts['accepted'] + counts['auto']}</font>  "
            f"<font color='#3CDCFF'>hand {counts['manual']}</font>  "
            f"<font color='#FF4646'>absent {counts['absent']}</font>  "
            f"<font color='#969696'>out {counts['outside']}</font><br><br>"
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
            pygame.K_v: self.toggle_rejected, pygame.K_f: self.toggle_fit,
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

    def _view_center(self):
        return [self.view[0], self.view[1]]

    def _on_click(self, e):
        """Right click: place the point by hand (Shift: and accept)."""
        if not self.view or self.help.visible or not self.items:
            return
        cx, cy, scale, ox, oy, vw, vh = self.view
        if not pygame.Rect(ox, oy, vw, vh).collidepoint(e.pos):
            return
        self.view_center = self._view_center()  # keep the view still
        self.cur = [cx + (e.pos[0] - ox - vw / 2) / scale, cy + (e.pos[1] - oy - vh / 2) / scale]
        self.edited = True
        self.dirty = True
        if pygame.key.get_mods() & pygame.KMOD_SHIFT:
            self.accept()

    def handle(self, e) -> bool:
        """Process one event. Returns False to quit."""
        running = True
        if e.type == pygame.QUIT:
            return False
        if e.type == pygame.VIDEORESIZE:
            R.resize_window(self, e.w, e.h, PANEL_W)
            self._layout_list()
            self._list_follow()
        elif e.type == pygame.KEYDOWN:
            running = self._on_key(e)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self._list_click(e.pos):
            pass
        elif e.type == pygame.MOUSEMOTION and self.list_drag:
            self._list_scroll_to(e.pos[1])
        elif e.type == pygame.MOUSEWHEEL and self.list_rect.collidepoint(pygame.mouse.get_pos()):
            self.list_scroll -= e.y * 3 * ROW_H
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 3 and self.canvas.collidepoint(e.pos):
            self._on_click(e)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.canvas.collidepoint(e.pos) \
                and self.view and not self.help.visible:
            self.drag = (e.pos, self._view_center(), self.view[2])
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
            self.list_drag = False
        elif e.type == pygame.MOUSEMOTION and self.drag:
            (sx, sy), c0, scale = self.drag
            self.view_center = [c0[0] - (e.pos[0] - sx) / scale, c0[1] - (e.pos[1] - sy) / scale]
            self.dirty = True
        elif e.type == pygame.MOUSEWHEEL and self.canvas.collidepoint(pygame.mouse.get_pos()):
            self.zoom_by(1.25 if e.y > 0 else 0.8, pygame.mouse.get_pos())
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
        self._draw_list()

    def run(self):
        clock = pygame.time.Clock()
        if self.items:
            self.show()
            self._list_follow()
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
        return r
