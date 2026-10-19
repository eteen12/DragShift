from e2e_linux import Scenario, inject, C
from e2e_linux_pdf_tools import drive, x_protected
def bad_keys(win):
    inject.type_text(C.shell, "abc")      # too short: Protect stays disabled
    inject.press(C.shell, "Return")
SCENARIOS = [Scenario("ADHOC protect too short", "nautilus", ["Report.pdf"], "Tools", "Protect",
                      x_protected, lambda: drive("Password-protect PDF", bad_keys))]
