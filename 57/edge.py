"""Ad-hoc check of the runner's failure path, ring flip, multi-select and desktop drags."""
import sys
sys.path.insert(0, sys.argv[1] + "/demo")
import e2e_linux as e
from e2e_common import v_format, v_rotated, v_merged, v_combined

real = e.press_and_move
state = {"no_shift": False}


def press_and_move(start, toward):
    if not state["no_shift"]:
        return real(start, toward)
    x0, y0 = e.inject.glide(e.C.shell, *start, dur=0.25)   # a drag without Shift: no menu
    e.C.shell.button(1, True)
    for k in range(1, 37):
        e.C.shell.move_pointer(x0 + 2 * k, y0 + 0.6 * k)


e.press_and_move = press_and_move


def no_shift(on):
    def f(*a):
        state["no_shift"] = on
        return v_format("PNG", ".png")(*a)
    return f


class Flag(e.Scenario):
    pass


def bad(outs, srcs):
    raise AssertionError("deliberate verifier failure")


def toggle(on):
    state["no_shift"] = on


S = e.Scenario
e.SCENARIOS[:] = [
    S("menu never opens", "nautilus", ["Photo.jpg"], "Convert", "PNG", v_format("PNG", ".png")),
    S("JPG → PNG", "nautilus", ["Photo.jpg"], "Convert", "PNG", e.v_same_size("PNG", ".png")),
    S("verifier fails", "nautilus", ["Graphic.png"], "Convert", "JPG", bad),
    S("PDF Rotate", "nautilus", ["Report.pdf"], "Tools", "Rotate", v_rotated),
    S("2 images → 1 PDF", "nautilus", ["Photo.jpg", "Second.jpg"], "Convert", "PDF", v_combined, select_extra=True),
    S("Merge 2 PDFs", "nautilus", ["Appendix.pdf", "Report.pdf"], "Tools", "Merge", v_merged, select_extra=True),
    S("Desktop: JPG → PNG", "desktop", ["D-Photo.jpg"], "Convert", "PNG", v_format("PNG", ".png")),
]
orig_run = e.run


def run(name, *a):
    toggle(name == "menu never opens")
    return orig_run(name, *a)


e.run = run
sys.exit(e.main(sys.argv[2:]))
