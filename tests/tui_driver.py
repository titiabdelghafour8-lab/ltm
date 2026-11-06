#!/usr/bin/env python3
"""Drive ltm's curses interface with scripted keys inside a pseudo-terminal.

Each "DUMP name" step writes the screen text curses holds at that moment, so the
interface can be checked without a person at a terminal:

    python3 tests/tui_driver.py --size 140x40 --out /tmp/screens \
        "WAIT 1.5" "DUMP processes" 2 "DUMP performance" KEY_DOWN "DUMP memory"

Steps: a literal key ("t", "/", "\\n"), a curses key name ("KEY_DOWN", "KEY_F1"),
"WAIT seconds" (lets the sampler tick), "DUMP name". After the last step it sends q.
Exit status is the child's: 0 when the interface ran and quit cleanly.
"""

import argparse
import os
import pty
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def child(steps, cols, rows, out_dir, extra_args):
    import fcntl
    import termios
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    os.environ["TERM"] = "xterm-256color"
    os.environ.setdefault("ESCDELAY", "25")
    import curses
    import ltm
    ltm.set_unicode(ltm.terminal_is_utf8())
    args = ltm.build_parser().parse_args(["tui"] + extra_args)
    mon = ltm.Monitor()

    class ScriptedScreen(object):
        def __init__(self, real):
            self.real = real
            self.queue = list(steps) + ["q"]

        def __getattr__(self, name):
            return getattr(self.real, name)

        def dump(self, name):
            h, w = self.real.getmaxyx()
            lines = []
            for y in range(h):
                raw = self.real.instr(y, 0, w * 4)   # n counts bytes; box characters take 3
                lines.append(raw.decode("utf-8", "replace").rstrip())
            with open(os.path.join(out_dir, name + ".txt"), "w") as f:
                f.write("\n".join(lines) + "\n")

        def get_wch(self):
            if not self.queue:
                return "q"
            step = self.queue.pop(0)
            if step.startswith("WAIT "):
                # Sleep in slices, as the real 100 ms input timeout does, so the
                # interface keeps sampling and redrawing while we wait.
                left = float(step[5:])
                time.sleep(min(0.1, left))
                if left > 0.1:
                    self.queue.insert(0, "WAIT %f" % (left - 0.1))
                raise curses.error("no input")
            if step.startswith("DUMP "):
                self.dump(step[5:])
                raise curses.error("no input")
            if step.startswith("KEY_"):
                return getattr(curses, step)
            return step.encode().decode("unicode_escape")

    def main(scr):
        ltm.App(ScriptedScreen(scr), mon, args).run()
    curses.wrapper(main)


def run(steps, cols=140, rows=40, out_dir=".", extra_args=(), timeout=120):
    os.makedirs(out_dir, exist_ok=True)
    err_path = os.path.join(out_dir, "traceback.txt")
    if os.path.exists(err_path):
        os.remove(err_path)
    pid, fd = pty.fork()
    if pid == 0:
        code = 0
        try:
            child(steps, cols, rows, out_dir, list(extra_args))
        except BaseException:
            import traceback
            with open(err_path, "w") as f:
                traceback.print_exc(file=f)
            code = 1
        os._exit(code)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not os.read(fd, 65536):
                break
        except OSError:
            break
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--size", default="140x40")
    p.add_argument("--out", default="screens")
    p.add_argument("--arg", action="append", default=[], help="extra ltm tui argument (repeatable)")
    p.add_argument("steps", nargs="*")
    a = p.parse_args()
    cols, rows = (int(x) for x in a.size.split("x"))
    code = run(a.steps, cols, rows, a.out, a.arg)
    err = os.path.join(a.out, "traceback.txt")
    if os.path.exists(err):
        sys.stderr.write(open(err).read())
    sys.exit(code)


if __name__ == "__main__":
    main()
