"""The radial menu's liquid glass render (#123), on every platform: the shape's signed
distance field is computed in one folded pass and cached per petal count, and the glass
made from it is the glass the per-petal union made."""
import math

import numpy as np
import pytest
from PIL import Image

from dragshift import liquid, radial
from dragshift.radial import CORNER, R_HUB, R_IN, R_OUT, SIZE, angles


def _union_sdf(n: int) -> np.ndarray:
    """The shape as it was built before #123: the hub and one full field per petal."""
    c = SIZE / 2
    fields = [liquid.sdf_circle(SIZE, SIZE, c, c, R_HUB)]
    for i in range(n):
        fields.append(liquid.sdf_ring_sector(SIZE, SIZE, c, c, R_IN, R_OUT, *angles(n, i), CORNER))
    return liquid.union(*fields)


def _old_ring_sector(w, h, cx, cy, r0, r1, a0, a1, round_r):
    """liquid.sdf_ring_sector before the `count` parameter, verbatim."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    dx, dy = xs - cx, cy - ys
    rad = np.hypot(dx, dy)
    mid = math.radians((a0 + a1) / 2)
    half = math.radians((a1 - a0) / 2)
    ang = np.arctan2(dy, dx) - mid
    ang = (ang + np.pi) % (2 * np.pi) - np.pi
    side = rad * np.sin(np.abs(ang) - half)
    side = np.where(np.abs(ang) - half > np.pi / 2, rad, side)
    ring = np.maximum(r0 - rad, rad - r1)
    ring_s, side_s = ring + round_r, side + round_r
    outside = np.hypot(np.maximum(ring_s, 0), np.maximum(side_s, 0))
    inside = np.minimum(np.maximum(ring_s, side_s), 0)
    return outside + inside - round_r


@pytest.mark.parametrize("args", [(200, 200, 100, 100, 30, 90, 45, 135, 6),
                                  (400, 400, 200, 200, 60, 158, 91.6, 178.4, 10),
                                  (120, 80, 50, 40, 10, 35, -30, 250, 4)])
def test_one_sector_is_unchanged(args):
    """count=1, the default every other caller uses, gives exactly the old field."""
    assert np.array_equal(liquid.sdf_ring_sector(*args), _old_ring_sector(*args))


@pytest.mark.parametrize("n", range(0, 13))
def test_menu_sdf_is_the_union_of_its_petals(n):
    """The folded field equals the union of n rotated petals up to float rounding, and the
    glass rendered from it differs by at most a few levels in a few pixels."""
    radial._SDFS.pop(n, None)
    sdf, ref = radial.menu_sdf(n), _union_sdf(n)
    assert sdf.shape == (SIZE, SIZE)
    assert float(np.abs(sdf - ref).max()) < 1e-3
    rng = np.random.default_rng(n)
    bg = Image.fromarray(rng.integers(0, 255, (SIZE, SIZE, 3), dtype=np.uint8))
    a = np.asarray(liquid.render(bg, sdf)[0], np.int16)
    b = np.asarray(liquid.render(bg, ref)[0], np.int16)
    diff = np.abs(a - b)
    assert diff.max() <= 4 and (diff > 0).any(-1).sum() < 200


def test_menu_sdf_is_cached_and_read_only():
    radial._SDFS.pop(9, None)
    sdf = radial.menu_sdf(9)
    assert radial.menu_sdf(9) is sdf
    assert not sdf.flags.writeable
    with pytest.raises(ValueError):
        sdf[0, 0] = 0


def test_render_job_uses_the_cached_shape(monkeypatch):
    """A render job emits the glass for its petal count, made from menu_sdf(n)."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    asked, got = [], []
    real = radial.menu_sdf
    monkeypatch.setattr(radial, "menu_sdf", lambda n: asked.append(n) or real(n))
    job = radial._RenderJob(3, 7, Image.new("RGB", (SIZE, SIZE), (30, 90, 160)))
    job.sig.done.connect(lambda *a: got.append(a))
    job.run()
    assert asked == [7]
    serial, n, img, light = got[0]
    assert (serial, n, img.width(), img.height(), light) == (3, 7, SIZE, SIZE, False)
