"""Root-cause demo: change the monitor scale the moment org.gnome.Shell answers (what the
old launcher did via set_scale), then see whether the shell ever logs startup-complete."""
import sys, time
sys.path.insert(0, ".")
from tests.linux import session as sm

orig = sm.Session._started_up
results = []
DELAYS = [float(x) for x in sys.argv[2].split(",")]
for i in range(int(sys.argv[1])):
    fired = []
    def hooked(self):
        if not fired:
            time.sleep(DELAYS[i % len(DELAYS)])
            fired.append(time.monotonic())
            dc = ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
                  "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method"]
            st = self.run(dc + ["org.gnome.Mutter.DisplayConfig.GetCurrentState"]).stdout
            serial = st.split(",", 1)[0].split()[-1]
            r = self.run(dc + ["org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig", serial, "1",
                               "[(0, 0, 2.0, 0, true, [('Meta-0', '3200x1800@60.000', @a{sv} {})])]",
                               "@a{sv} {}"])
            fired.append(r.returncode)
            fired.append(orig(self))  # already started before the change?
        return orig(self)
    sm.Session._started_up = hooked
    s = sm.Session(size=(3200, 1800), timeout=20)
    t0 = time.monotonic()
    try:
        s.start()
        res = f"started in {time.monotonic()-t0:.1f}s"
    except sm.SessionError as e:
        res = "WEDGED: " + str(e).splitlines()[0]
    finally:
        log = s.log_path.read_text(errors="replace") if s.log_path and s.log_path.exists() else ""
        s.stop()
    print(i, DELAYS[i % len(DELAYS)], f"apply rc={fired[1]} started-before-change={fired[2]}", res, flush=True)
