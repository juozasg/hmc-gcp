"""Image loading and cropping (works with non-ASCII paths on Windows)."""

from __future__ import annotations

from collections import OrderedDict

import cv2
import numpy as np


def load_bgr(path: str) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise IOError(f"cannot read image {path}")
    return img


def crop_padded(img: np.ndarray, x0: int, y0: int, x1: int, y1: int, with_mask=False):
    """Crop [x0:x1, y0:y1]; parts outside the image are black.
    With with_mask=True also return a boolean mask of the pixels inside the image."""
    h, w = img.shape[:2]
    out = np.zeros((max(1, y1 - y0), max(1, x1 - x0), 3), dtype=img.dtype)
    valid = np.zeros(out.shape[:2], dtype=bool)
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(w, x1), min(h, y1)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0] = img[sy0:sy1, sx0:sx1]
        valid[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0] = True
    return (out, valid) if with_mask else out


class ImageCache:
    def __init__(self, size: int = 4):
        self.size = size
        self._d: OrderedDict[str, np.ndarray] = OrderedDict()

    def get(self, path: str) -> np.ndarray:
        if path in self._d:
            self._d.move_to_end(path)
            return self._d[path]
        img = load_bgr(path)
        self._d[path] = img
        while len(self._d) > self.size:
            self._d.popitem(last=False)
        return img
