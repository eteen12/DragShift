"""HEIC and AVIF: encode and decode through every convert/tool path, and a missing codec
hides its Convert target instead of failing at conversion time."""
import pytest
from PIL import Image

from dragshift import convert, imaging

CODECS = ["HEIC", "AVIF"]


@pytest.fixture(params=CODECS)
def coded(request, tmp_path):
    """A 64x48 image with transparency, converted from PNG to HEIC or AVIF by the app."""
    im = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    im.paste((255, 120, 80, 255), (8, 8, 56, 40))
    png = tmp_path / "pic.png"
    im.save(png)
    return convert.convert(png, "image", request.param)[0]


def test_round_trip(coded):
    assert coded.suffix in (".heic", ".avif")
    assert convert.open_image(coded).size == (64, 48)


def test_converts_to_every_target(coded):
    for target in convert.targets_for("image", coded.suffix):
        out = convert.convert(coded, "image", target)[0]
        assert out.stat().st_size > 0
        if target not in ("PDF", "ICO"):  # PDF isn't an image; ICO holds several sizes
            assert convert.open_image(out).size == (64, 48)


def test_tools_keep_the_format(coded):
    for out in (imaging.compress_image(coded, 60), imaging.resize_image(coded, (32, 24)),
                imaging.strip_image_metadata(coded),
                imaging.save_like(convert.open_image(coded).crop((0, 0, 20, 20)), coded, "Cropped")):
        assert out.suffix == coded.suffix
        convert.open_image(out)


@pytest.mark.parametrize("target", CODECS)
def test_exif_rotation_applied_once(tmp_path, target):
    """A rotated phone photo (EXIF orientation 6) opens upright, never rotated twice."""
    exif = Image.Exif()
    exif[0x0112] = 6
    p = tmp_path / f"phone.{target.lower()}"
    Image.new("RGB", (40, 20), (200, 30, 30)).save(p, convert.PIL_FORMAT[target], exif=exif.tobytes())
    assert convert.open_image(p).size == (20, 40)


@pytest.mark.parametrize("flag,target", [("HEIF_OK", "HEIC"), ("AVIF_OK", "AVIF")])
def test_missing_codec_hides_target(monkeypatch, flag, target):
    full = convert.targets_for("image", ".png")
    assert target in full
    monkeypatch.setattr(convert, flag, False)
    assert convert.targets_for("image", ".png") == [t for t in full if t != target]
