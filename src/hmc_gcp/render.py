"""Small pygame drawing helpers shared by the tag and view windows."""

from __future__ import annotations

import cv2
import numpy as np
import pygame

GREEN = (60, 230, 90)
CYAN = (60, 220, 255)
YELLOW = (255, 220, 40)
RED = (255, 70, 70)
ORANGE = (255, 150, 40)
PURPLE = (190, 90, 255)
WHITE = (245, 245, 245)
BLACK = (0, 0, 0)
GREY = (150, 150, 150)

STATUS_COLOR = {"accepted": GREEN, "auto": GREEN, "manual": CYAN,
                "pending": YELLOW, "absent": RED, "outside": GREY}
STATUS_HEX = {"accepted": "#3CE65A", "auto": "#3CE65A", "manual": "#3CDCFF",
              "pending": "#FFDC28", "absent": "#FF4646"}


def to_surface(bgr: np.ndarray, size: tuple[int, int]) -> pygame.Surface:
    """Resize a BGR image to size (w, h) and make a pygame surface."""
    h, w = bgr.shape[:2]
    interp = cv2.INTER_NEAREST if size[0] > 2.5 * w else (
        cv2.INTER_LINEAR if size[0] >= w else cv2.INTER_AREA)
    rgb = cv2.cvtColor(cv2.resize(bgr, size, interpolation=interp), cv2.COLOR_BGR2RGB)
    return pygame.image.frombytes(np.ascontiguousarray(rgb).tobytes(), size, "RGB")


def cross(surf, pos, color, r=10, width=2):
    x, y = int(pos[0]), int(pos[1])
    for c, w in ((BLACK, width + 2), (color, width)):
        pygame.draw.line(surf, c, (x - r, y), (x + r, y), w)
        pygame.draw.line(surf, c, (x, y - r), (x, y + r), w)


def xmark(surf, pos, color, r=9, width=2):
    x, y = int(pos[0]), int(pos[1])
    for c, w in ((BLACK, width + 2), (color, width)):
        pygame.draw.line(surf, c, (x - r, y - r), (x + r, y + r), w)
        pygame.draw.line(surf, c, (x - r, y + r), (x + r, y - r), w)


def ring(surf, pos, color, r, width=2):
    r = max(2, int(r))
    pygame.draw.circle(surf, BLACK, (int(pos[0]), int(pos[1])), r, width + 2)
    pygame.draw.circle(surf, color, (int(pos[0]), int(pos[1])), r, width)


def label(surf, font, text, pos, color=WHITE):
    img = font.render(text, True, color)
    bg = pygame.Surface((img.get_width() + 6, img.get_height() + 2), pygame.SRCALPHA)
    bg.fill((0, 0, 0, 160))
    surf.blit(bg, (pos[0] - 3, pos[1] - 1))
    surf.blit(img, pos)


MIN_CANVAS = (300, 300)


def resize_window(app, w: int, h: int, panel_w: int):
    """Adapt an app with a right-hand pygame_gui panel to a new window size."""
    w = max(w, panel_w + MIN_CANVAS[0])
    h = max(h, MIN_CANVAS[1])
    app.W, app.H = w, h
    app.screen = pygame.display.get_surface()
    app.ui.set_window_resolution((w, h))
    app.canvas = pygame.Rect(0, 0, w - panel_w, h)
    app.canvas_surf = pygame.Surface(app.canvas.size)
    app.panel.set_relative_position((w - panel_w, 0))
    app.panel.set_dimensions((panel_w, h))
    app.dirty = True
