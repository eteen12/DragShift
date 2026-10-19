import sys

from dragshift.handoff import start_managed, try_handoff

if try_handoff(sys.argv):
    sys.exit(0)
# Linux: let the GNOME Shell extension start the app as its own Wayland client (#95)
if sys.platform.startswith("linux") and start_managed(sys.argv):
    sys.exit(0)

from dragshift.app import main  # noqa: E402

sys.exit(main())
