#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ltm - a task manager for the Linux terminal.

Shows what the machine and every process is using: CPU, memory, disk I/O, GPU,
network adapters, sensors and battery, with live graphs, plus users, systemd
services and startup entries. Interactive (curses) or one-shot (text / JSON).

Runs on any Linux distribution with Python 3.6 or newer. It needs nothing but
the standard library and reads /proc and /sys directly. Used when present:
nvidia-smi (NVIDIA GPUs), systemctl (services, startup), pci.ids (GPU names).

Run `ltm --help`, or press F1 inside the interface.
"""

from __future__ import division, print_function

import argparse
import collections
import json
import locale
import math
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata

try:
    import pwd
    import grp
except ImportError:  # not a Unix system; only the parsers are usable (tests)
    pwd = grp = None

try:
    import curses
except ImportError:  # e.g. openSUSE ships curses as python3-curses
    curses = None

__version__ = "1.0.0"

# Roots of the kernel interfaces. Overridable so tests can use fake trees.
PROC = os.environ.get("LTM_PROC", "/proc")
SYS = os.environ.get("LTM_SYS", "/sys")
ETC = os.environ.get("LTM_ETC", "/etc")


def _sysconf(name, default):
    try:
        value = os.sysconf(name)
    except (AttributeError, ValueError, OSError):
        return default
    return value if value and value > 0 else default


CLK_TCK = _sysconf("SC_CLK_TCK", 100)
PAGE_SIZE = _sysconf("SC_PAGE_SIZE", 4096)
HISTORY = 600                # samples kept per graph
KTHREAD_FLAG = 0x00200000    # PF_KTHREAD in the flags field of /proc/PID/stat
TEXT_SORT_KEYS = ("name", "user", "cmd", "unit", "description", "state", "type", "pid", "uid")

# ---------------------------------------------------------------------------
# Small helpers: file access, text width, formatting
# ---------------------------------------------------------------------------


def read_text(path, default=None):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (OSError, IOError, ValueError):
        return default
    return data.decode("utf-8", "replace")


def read_line(path, default=None):
    text = read_text(path)
    if text is None:
        return default
    text = text.strip()
    return text if text else default


def read_int(path, default=None):
    text = read_text(path)
    if text is None:
        return default
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return default


def listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def parse_kv(text, sep=":"):
    out = {}
    for line in (text or "").splitlines():
        key, found, value = line.partition(sep)
        if found:
            out[key.strip()] = value.strip()
    return out


def parse_kv_ints(text):
    out = {}
    for line in (text or "").splitlines():
        key, found, value = line.partition(":")
        if found:
            try:
                out[key.strip()] = int(value.split()[0])
            except (ValueError, IndexError):
                pass
    return out


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(s):
    """Make untrusted text (process names, command lines, file names) safe to show."""
    if not s:
        return ""
    s = s.replace("\t", " ").replace("\n", " ")
    return _CTRL_RE.sub("?", s)


GLYPHS_UNICODE = {
    "ell": u"…", "full": u"█", "empty": u"░",
    "blocks": u" ▁▂▃▄▅▆▇█",
    "tree_mid": u"├─ ", "tree_last": u"└─ ", "tree_pipe": u"│  ", "tree_gap": u"   ",
    "down": u"▼", "up": u"▲", "rx": u"↓", "tx": u"↑",
    "h": u"─", "v": u"│", "tl": u"┌", "tr": u"┐", "bl": u"└", "br": u"┘",
    "closed": u"▸", "open": u"▾", "deg": u"°", "mod": u"▓", "stby": u"▒",
    "free": u"░", "arrow": u"→", "dash": u"—",
}
GLYPHS_ASCII = {
    "ell": "~", "full": "#", "empty": ".", "blocks": " ..--==##",
    "tree_mid": "|- ", "tree_last": "`- ", "tree_pipe": "|  ", "tree_gap": "   ",
    "down": "v", "up": "^", "rx": "v", "tx": "^",
    "h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "closed": "+", "open": "-", "deg": "", "mod": "%", "stby": "=", "free": ".", "arrow": ">", "dash": "-",
}
G = dict(GLYPHS_UNICODE)


def set_unicode(enabled):
    G.clear()
    G.update(GLYPHS_UNICODE if enabled else GLYPHS_ASCII)


def terminal_is_utf8():
    """Ask the C library, not Python: in Python's UTF-8 mode a C locale still reports UTF-8,
    but curses (and the terminal) would print nothing for non-ASCII characters."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        codeset = locale.nl_langinfo(locale.CODESET)
    except (AttributeError, ValueError):
        codeset = locale.getpreferredencoding(False)
    return "UTF" in (codeset or "").upper()


def char_width(c):
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def text_width(s):
    try:
        s.encode("latin-1")
        return len(s)
    except UnicodeEncodeError:
        return sum(char_width(c) for c in s)


def clip(s, width):
    """Cut s to at most `width` terminal columns, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if text_width(s) <= width:
        return s
    out = []
    used = 0
    for c in s:
        cw = char_width(c)
        if used + cw > width - 1:
            break
        out.append(c)
        used += cw
    return "".join(out) + G["ell"]


def pad(s, width, align="l"):
    s = clip(s, width)
    gap = width - text_width(s)
    if gap <= 0:
        return s
    return " " * gap + s if align == "r" else s + " " * gap


def fmt_bytes(n, short=False):
    if n is None:
        return "-"
    n = float(n)
    units = ("B", "K", "M", "G", "T", "P") if short else ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    i = 0
    while abs(n) >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    sep = "" if short else " "
    if i == 0:
        return "%d%s%s" % (n, sep, units[0])
    if abs(n) >= 100:
        num = "%.0f" % n
    elif abs(n) >= 10 or short:
        num = "%.1f" % n
    else:
        num = "%.2f" % n
    return num + sep + units[i]


def fmt_rate(n, short=False):
    return "-" if n is None else fmt_bytes(n, short) + "/s"


def fmt_bits(bytes_per_s):
    """Network speed in bits per second, like Windows Task Manager (Kbps, Mbps)."""
    if bytes_per_s is None:
        return "-"
    b = bytes_per_s * 8.0
    for unit in ("bps", "Kbps", "Mbps", "Gbps", "Tbps"):
        if b < 1000 or unit == "Tbps":
            return ("%.0f %s" if unit == "bps" or b >= 100 else "%.1f %s") % (b, unit)
        b /= 1000.0
    return "-"


def fmt_pct(p, digits=1):
    return "-" if p is None else ("%." + str(digits) + "f%%") % p


def fmt_duration(sec):
    if sec is None:
        return "-"
    sec = int(max(0, sec))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return "%dd %02d:%02d:%02d" % (days, hours, minutes, seconds)
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def fmt_ago(sec):
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    if sec < 60:
        return "%ds ago" % sec
    if sec < 3600:
        return "%dm %02ds ago" % divmod(sec, 60)
    if sec < 86400:
        return "%dh %02dm ago" % (sec // 3600, sec % 3600 // 60)
    return "%dd %dh ago" % (sec // 86400, sec % 86400 // 3600)


def fmt_cputime(sec):
    if sec is None:
        return "-"
    if sec >= 3600:
        return "%dh%02dm" % (int(sec // 3600), int(sec % 3600 // 60))
    minutes = int(sec // 60)
    return "%d:%05.2f" % (minutes, sec - minutes * 60)


def fmt_freq(khz):
    if not khz:
        return "-"
    return "%.2f GHz" % (khz / 1e6) if khz >= 1e6 else "%.0f MHz" % (khz / 1e3)


def fmt_temp(c):
    return "-" if c is None else "%.0f %sC" % (c, G["deg"])


def fmt_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else "-"


def fmt_hours(h):
    if h is None:
        return "-"
    minutes = int(round(h * 60))
    return "%dh %02dm" % divmod(minutes, 60)


def compact_ranges(nums):
    nums = sorted(set(nums))
    parts = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else "%d-%d" % (nums[i], nums[j]))
        i = j + 1
    return ",".join(parts)


def nice_scale(value, base):
    """Round a graph maximum up to 1/2/5 x base^n so axis labels read well."""
    if not value or value <= 0:
        return 1.0
    unit = 1.0
    while value / unit >= base:
        unit *= base
    m = value / unit
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if m <= step:
            return step * unit
    return base * unit


def wrap_text(text, width):
    if width <= 4:
        return [text]
    return textwrap.wrap(text, width, break_long_words=True, break_on_hyphens=False) or [""]


def run_cmd_full(argv, timeout=10):
    """Run a command; return (returncode, stdout, stderr). Never raises."""
    env = dict(os.environ, LC_ALL="C", SYSTEMD_COLORS="0", SYSTEMD_PAGER="", PAGER="cat")
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, universal_newlines=True, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", str(e)
    return p.returncode, p.stdout, p.stderr


def run_cmd(argv, timeout=10):
    rc, out, _ = run_cmd_full(argv, timeout)
    return out if rc == 0 else None


_USERS = {}
_GROUPS = {}


def user_name(uid):
    name = _USERS.get(uid)
    if name is None:
        try:
            name = pwd.getpwuid(uid).pw_name if pwd else str(uid)
        except KeyError:
            name = str(uid)
        _USERS[uid] = name
    return name


def group_name(gid):
    name = _GROUPS.get(gid)
    if name is None:
        try:
            name = grp.getgrgid(gid).gr_name if grp else str(gid)
        except KeyError:
            name = str(gid)
        _GROUPS[gid] = name
    return name


class BackgroundTask(object):
    """Calls fn() every `period` seconds in a daemon thread; `value` is the latest result.

    Used for anything that can be slow or hang (systemctl, nvidia-smi, statvfs on
    network mounts) so the interface never freezes.
    """

    def __init__(self, fn, period):
        self.fn = fn
        self.period = period
        self.value = None
        self.error = None
        self.stamp = 0.0
        self._wake = threading.Event()
        self._done = threading.Event()
        self._thread = None

    def _ensure(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="ltm-background")
            self._thread.daemon = True
            self._thread.start()

    def get(self):
        self._ensure()
        return self.value

    def wait(self, timeout):
        self._ensure()
        self._done.wait(timeout)
        return self.value

    def refresh(self):
        self._ensure()
        self._wake.set()

    def _loop(self):
        while True:
            try:
                self.value = self.fn()
                self.error = None
            except Exception as e:  # keep the thread alive whatever happens
                self.error = "%s: %s" % (type(e).__name__, e)
            self.stamp = time.monotonic()
            self._done.set()
            self._wake.wait(self.period)
            self._wake.clear()

# ---------------------------------------------------------------------------
# Static system information
# ---------------------------------------------------------------------------


def os_release_name():
    for path in (ETC + "/os-release", "/usr/lib/os-release"):
        text = read_text(path)
        if text:
            d = {}
            for line in text.splitlines():
                k, found, v = line.partition("=")
                if found:
                    d[k.strip()] = v.strip().strip('"').strip("'")
            name = d.get("PRETTY_NAME") or d.get("NAME")
            if name:
                return name
    lsb = parse_kv(read_text(ETC + "/lsb-release", ""), "=").get("DISTRIB_DESCRIPTION")
    if lsb:
        return lsb.strip('"')
    for path, prefix in ((ETC + "/redhat-release", ""), (ETC + "/alpine-release", "Alpine Linux "),
                         (ETC + "/debian_version", "Debian ")):
        line = read_line(path)
        if line:
            return prefix + line
    return "Linux"


def detect_virt():
    """Best-effort: are we in WSL, a container or a virtual machine?"""
    version = (read_text(PROC + "/version", "") or "").lower()
    if "microsoft" in version:
        return "WSL2" if "wsl2" in version or "microsoft-standard" in version else "WSL"
    if os.path.exists("/.dockerenv"):
        return "Docker container"
    if os.path.exists("/run/.containerenv"):
        return "Podman container"
    env1 = read_text(PROC + "/1/environ", "") or ""
    if "container=lxc" in env1:
        return "LXC container"
    cg = read_text(PROC + "/1/cgroup", "") or ""
    if "/docker" in cg or "/kubepods" in cg:
        return "container"
    vendor = (read_line(SYS + "/class/dmi/id/sys_vendor") or "").lower()
    product = (read_line(SYS + "/class/dmi/id/product_name") or "").lower()
    for needle, name in (("qemu", "KVM/QEMU VM"), ("kvm", "KVM VM"), ("vmware", "VMware VM"),
                         ("virtualbox", "VirtualBox VM"), ("innotek", "VirtualBox VM"), ("xen", "Xen VM"),
                         ("amazon ec2", "Amazon EC2"), ("google compute", "Google Cloud VM"), ("parallels", "Parallels VM")):
        if needle in vendor or needle in product:
            return name
    if "microsoft" in vendor and "virtual" in product:
        return "Hyper-V VM"
    return None


def machine_model():
    vendor = read_line(SYS + "/class/dmi/id/sys_vendor")
    product = read_line(SYS + "/class/dmi/id/product_name")
    if product and product.lower() not in ("to be filled by o.e.m.", "system product name", "default string"):
        return clean(((vendor + " ") if vendor and not product.startswith(vendor) else "") + product)
    model = read_text(SYS + "/firmware/devicetree/base/model") or read_text(PROC + "/device-tree/model")
    if model:
        return clean(model.strip("\x00 \n"))
    return None


def static_system_info():
    try:
        uname = os.uname()
        kernel, arch = uname.release, uname.machine
    except AttributeError:
        kernel, arch = "?", "?"
    return {"hostname": socket.gethostname(), "os": os_release_name(), "kernel": kernel, "arch": arch,
            "virt": detect_virt(), "machine": machine_model()}


ARM_IMPLEMENTERS = {0x41: "ARM", 0x42: "Broadcom", 0x43: "Cavium", 0x48: "HiSilicon", 0x4e: "NVIDIA",
                    0x50: "Ampere", 0x51: "Qualcomm", 0x53: "Samsung", 0x61: "Apple", 0x6d: "Microsoft", 0xc0: "Ampere"}
ARM_PARTS = {0xd03: "Cortex-A53", 0xd04: "Cortex-A35", 0xd05: "Cortex-A55", 0xd07: "Cortex-A57", 0xd08: "Cortex-A72",
             0xd09: "Cortex-A73", 0xd0a: "Cortex-A75", 0xd0b: "Cortex-A76", 0xd0c: "Neoverse-N1", 0xd0d: "Cortex-A77",
             0xd40: "Neoverse-V1", 0xd41: "Cortex-A78", 0xd44: "Cortex-X1", 0xd46: "Cortex-A510", 0xd47: "Cortex-A710",
             0xd48: "Cortex-X2", 0xd49: "Neoverse-N2", 0xd4b: "Cortex-A78C", 0xd4d: "Cortex-A715", 0xd4e: "Cortex-X3",
             0xd4f: "Neoverse-V2", 0xd80: "Cortex-A520", 0xd81: "Cortex-A720", 0xd82: "Cortex-X4"}


def cpu_static_info():
    info = {"model": None, "vendor": None, "logical": os.cpu_count() or 1, "cores": None, "sockets": None,
            "max_khz": None, "base_khz": None, "flags": [], "caches": [], "hybrid": None}
    text = read_text(PROC + "/cpuinfo", "") or ""
    impl = part = hardware = None
    for block in re.split(r"\n\s*\n", text):
        d = parse_kv(block)
        if not d:
            continue
        if not info["model"]:
            info["model"] = d.get("model name") or d.get("cpu model") or d.get("cpu")
        if not info["vendor"]:
            info["vendor"] = d.get("vendor_id")
        if not info["flags"]:
            info["flags"] = (d.get("flags") or d.get("Features") or "").split()
        if impl is None and "CPU implementer" in d:
            try:
                impl = int(d["CPU implementer"], 16)
                part = int(d.get("CPU part", "0"), 16)
            except ValueError:
                pass
        hardware = hardware or d.get("Hardware")
    if not info["model"] and impl is not None:
        info["model"] = "%s %s" % (ARM_IMPLEMENTERS.get(impl, "0x%02x" % impl), ARM_PARTS.get(part, "part 0x%03x" % (part or 0)))
    if not info["model"]:
        info["model"] = hardware or machine_model() or "Unknown CPU"
    info["model"] = clean(re.sub(r"\s+", " ", info["model"]).strip())

    base = SYS + "/devices/system/cpu"
    cpus = sorted(int(n[3:]) for n in listdir(base) if re.match(r"cpu\d+$", n))
    cores, packages = set(), set()
    caches = {}
    for i in cpus:
        topo = "%s/cpu%d/topology/" % (base, i)
        pkg = read_int(topo + "physical_package_id")
        core = read_int(topo + "core_id")
        if pkg is not None and core is not None:
            cores.add((pkg, core))
            packages.add(pkg)
        for idx in listdir("%s/cpu%d/cache" % (base, i)):
            if not idx.startswith("index"):
                continue
            cdir = "%s/cpu%d/cache/%s/" % (base, i, idx)
            level = read_line(cdir + "level")
            ctype = read_line(cdir + "type") or ""
            size = read_line(cdir + "size")
            shared = read_line(cdir + "shared_cpu_list") or str(i)
            if level and size:
                caches[(level, ctype, shared)] = size
    if cores:
        info["cores"] = len(cores)
        info["sockets"] = len(packages)
    totals = collections.OrderedDict()
    for (level, ctype, _), size in sorted(caches.items()):
        m = re.match(r"(\d+)([KMG]?)", size)
        if not m:
            continue
        nbytes = int(m.group(1)) * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2)]
        name = "L%s%s" % (level, {"Data": "d", "Instruction": "i"}.get(ctype, ""))
        totals[name] = totals.get(name, 0) + nbytes
    info["caches"] = list(totals.items())
    info["max_khz"] = read_int(base + "/cpu0/cpufreq/cpuinfo_max_freq")
    info["base_khz"] = read_int(base + "/cpu0/cpufreq/base_frequency")
    pcores = read_line(SYS + "/devices/cpu_core/cpus")
    ecores = read_line(SYS + "/devices/cpu_atom/cpus")
    if pcores and ecores:
        info["hybrid"] = (pcores, ecores)
    return info


def cpuinfo_mhz():
    vals = []
    for line in (read_text(PROC + "/cpuinfo", "") or "").splitlines():
        if line.startswith("cpu MHz"):
            try:
                vals.append(float(line.split(":")[1]))
            except (IndexError, ValueError):
                pass
    return sum(vals) / len(vals) * 1000.0 if vals else None

# ---------------------------------------------------------------------------
# /proc and /sys parsers (pure functions, unit-tested with fake data)
# ---------------------------------------------------------------------------


def parse_proc_stat(text):
    out = {"cpu": None, "cpus": {}}
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        key = parts[0]
        try:
            if key == "cpu":
                out["cpu"] = [int(x) for x in parts[1:]]
            elif key.startswith("cpu") and key[3:].isdigit():
                out["cpus"][int(key[3:])] = [int(x) for x in parts[1:]]
            elif key in ("ctxt", "processes", "procs_running", "procs_blocked", "btime", "intr") and len(parts) > 1:
                out[key] = int(parts[1])
        except ValueError:
            pass
    return out


def cpu_percent(a, b):
    """CPU busy % between two /proc/stat rows, with a user/system/iowait/steal split."""
    a = (list(a) + [0] * 10)[:10]
    b = (list(b) + [0] * 10)[:10]
    d = [max(0, y - x) for x, y in zip(a, b)]
    user, nice, system, idle, iowait, irq, softirq, steal = d[:8]   # guest time is already in user
    total = user + nice + system + idle + iowait + irq + softirq + steal
    if total <= 0:
        return 0.0, {"user": 0.0, "system": 0.0, "iowait": 0.0, "steal": 0.0, "idle": 100.0}
    f = 100.0 / total
    busy = total - idle - iowait
    return busy * f, {"user": (user + nice) * f, "system": (system + irq + softirq) * f,
                      "iowait": iowait * f, "steal": steal * f, "idle": idle * f}


def parse_meminfo(text):
    d = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            value = int(parts[1])
        except ValueError:
            continue
        if len(parts) > 2 and parts[2].lower() == "kb":
            value *= 1024
        d[parts[0].rstrip(":")] = value
    if "MemAvailable" not in d and "MemTotal" in d:   # kernels before 3.14
        d["MemAvailable"] = max(0, d.get("MemFree", 0) + d.get("Buffers", 0) + d.get("Cached", 0)
                                + d.get("SReclaimable", 0) - d.get("Shmem", 0))
    return d


def memory_summary(mi):
    def g(k):
        return mi.get(k, 0)
    total = g("MemTotal")
    avail = min(g("MemAvailable"), total)
    used = max(0, total - avail)
    free = min(g("MemFree"), avail)
    modified = min(g("Dirty") + g("Writeback"), avail - free)
    standby = max(0, avail - free - modified)
    swap_total = g("SwapTotal")
    swap_used = max(0, swap_total - g("SwapFree"))
    return {
        "total": total, "available": avail, "used": used,
        "used_pct": used * 100.0 / total if total else 0.0,
        "free": g("MemFree"), "buffers": g("Buffers"), "cached": g("Cached") + g("SReclaimable"),
        "shared": g("Shmem"), "slab": g("Slab"), "slab_reclaimable": g("SReclaimable"),
        "page_tables": g("PageTables"), "kernel_stack": g("KernelStack"),
        "dirty": g("Dirty"), "writeback": g("Writeback"), "anon": g("AnonPages"), "mapped": g("Mapped"),
        "committed": g("Committed_AS"), "commit_limit": g("CommitLimit"),
        "swap_total": swap_total, "swap_used": swap_used,
        "swap_pct": swap_used * 100.0 / swap_total if swap_total else 0.0, "swap_cached": g("SwapCached"),
        "hugepages_total": g("HugePages_Total"), "hugepages_free": g("HugePages_Free"),
        "hugepage_size": g("Hugepagesize"),
        "composition": {"in_use": used, "modified": modified, "standby": standby, "free": free},
    }


def parse_diskstats(text):
    out = {}
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) < 14:
            continue
        try:
            out[p[2]] = [int(x) for x in p[3:]]
        except ValueError:
            pass
    return out


def parse_net_dev(text):
    out = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        name, _, data = line.partition(":")
        f = data.split()
        if len(f) < 16:
            continue
        try:
            f = [int(x) for x in f[:16]]
        except ValueError:
            continue
        out[name.strip()] = {"rx_bytes": f[0], "rx_packets": f[1], "rx_errs": f[2], "rx_drop": f[3],
                             "tx_bytes": f[8], "tx_packets": f[9], "tx_errs": f[10], "tx_drop": f[11]}
    return out


def parse_pid_stat(text):
    """Parse /proc/PID/stat. The name may contain spaces and parentheses."""
    if not text:
        return None
    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right < left:
        return None
    rest = text[right + 2:].split()
    if len(rest) < 22:
        return None

    def f(i, default=0):
        try:
            return int(rest[i])
        except (IndexError, ValueError):
            return default
    return {"comm": text[left + 1:right], "state": rest[0], "ppid": f(1), "pgrp": f(2), "session": f(3),
            "tty_nr": f(4), "flags": f(6), "minflt": f(7), "majflt": f(9), "utime": f(11), "stime": f(12),
            "cutime": f(13), "cstime": f(14), "priority": f(15), "nice": f(16), "threads": f(17),
            "starttime": f(19), "vsize": f(20), "rss": f(21), "processor": f(36, -1), "rt_priority": f(37),
            "policy": f(38), "blkio_ticks": f(39)}


def decode_tty(tty_nr):
    if not tty_nr:
        return None
    major = (tty_nr >> 8) & 0xfff
    minor = (tty_nr & 0xff) | ((tty_nr >> 12) & 0xfff00)
    if 136 <= major <= 143:
        return "pts/%d" % (minor + (major - 136) * 256)
    if major == 4:
        return "tty%d" % minor if minor < 64 else "ttyS%d" % (minor - 64)
    return "tty(%d,%d)" % (major, minor)


_CONTAINER_RES = (
    re.compile(r"(?:docker|libpod|cri-containerd|crio|containerd)[-/]([0-9a-f]{64})"),
    re.compile(r"/kubepods[^\n]*?([0-9a-f]{64})"),
    re.compile(r"/docker/([0-9a-f]{12,64})"),
)


def parse_cgroup(text):
    """Return (cgroup path, system unit, user unit, container id) for /proc/PID/cgroup."""
    path = None
    fallback = None
    for line in (text or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hid, controllers, p = parts
        if hid == "0" and controllers == "":   # cgroup v2
            path = p
            break
        if controllers == "name=systemd":
            path = p
        elif fallback is None:
            fallback = p
    path = path or fallback
    unit = user_unit = container = None
    if path:
        for rx in _CONTAINER_RES:
            m = rx.search(path)
            if m:
                container = m.group(1)[:12]
                break
        if container is None:
            m = re.search(r"lxc\.payload\.([^/]+)|/lxc/([^/]+)", path)
            if m:
                container = "lxc:" + (m.group(1) or m.group(2))
        comps = [c for c in path.split("/") if c]
        if comps and comps[0] == "system.slice":
            for c in comps[1:]:
                if c.endswith((".service", ".scope")):
                    unit = c
                    break
        elif comps and comps[0] == "init.scope":
            unit = "init.scope"
        elif comps and comps[0] == "user.slice":
            for c in comps:
                if re.match(r"user@\d+\.service$", c):   # the per-user manager is a system service
                    unit = c
            for c in reversed(comps):
                if c.endswith((".service", ".scope")) and c != unit:
                    user_unit = c
                    break
    return path, unit, user_unit, container


_SIZE_UNITS = {"": 1, "B": 1, "KiB": 1024, "kB": 1024, "KB": 1024, "K": 1024, "MiB": 1024 ** 2, "MB": 1024 ** 2,
               "GiB": 1024 ** 3, "GB": 1024 ** 3}


def parse_size(value):
    parts = value.split()
    try:
        n = int(parts[0])
    except (ValueError, IndexError):
        return None
    return n * _SIZE_UNITS.get(parts[1] if len(parts) > 1 else "", 1)


def parse_drm_fdinfo(text):
    """Parse a DRM client's fdinfo (Linux 5.19+: amdgpu, i915, xe, msm, panfrost, v3d, nouveau ...)."""
    if not text or "drm-driver" not in text:
        return None
    info = {"engines": {}, "capacity": {}, "cycles": {}, "total_cycles": {}, "mem": 0}
    for line in text.splitlines():
        key, found, value = line.partition(":")
        if not found:
            continue
        key = key.strip()
        value = value.strip()
        if key == "drm-driver":
            info["driver"] = value
        elif key == "drm-client-id":
            info["client"] = value
        elif key == "drm-pdev":
            info["pdev"] = value
        elif key.startswith("drm-engine-capacity-"):
            try:
                info["capacity"][key[20:]] = max(1, int(value))
            except ValueError:
                pass
        elif key.startswith("drm-engine-"):
            try:
                info["engines"][key[11:]] = int(value.split()[0])
            except (ValueError, IndexError):
                pass
        elif key.startswith("drm-total-cycles-"):
            try:
                info["total_cycles"][key[17:]] = int(value.split()[0])
            except (ValueError, IndexError):
                pass
        elif key.startswith("drm-cycles-"):
            try:
                info["cycles"][key[11:]] = int(value.split()[0])
            except (ValueError, IndexError):
                pass
        elif key.startswith(("drm-resident-", "drm-memory-")):
            region = key.split("-", 2)[2]
            if "vram" in region or "local" in region:
                size = parse_size(value)
                if size:
                    info["mem"] += size
    return info


def drm_client_busy(cur, prev, dt):
    """Per-engine busy fraction of one DRM client between two fdinfo samples."""
    out = {}
    if not prev or not dt or dt <= 0:
        return out
    for eng, ns in cur["engines"].items():
        before = prev["engines"].get(eng)
        if before is None:
            continue
        cap = cur["capacity"].get(eng, 1)
        out[eng] = max(0.0, min(1.0, (ns - before) / (dt * 1e9) / cap))
    for eng, cyc in cur["cycles"].items():
        total = cur["total_cycles"].get(eng)
        pc = prev["cycles"].get(eng)
        pt = prev["total_cycles"].get(eng)
        if None in (total, pc, pt) or total <= pt:
            continue
        out[eng] = max(0.0, min(1.0, (cyc - pc) / float(total - pt)))
    return out


def parse_systemctl_units(text):
    units = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip(u"●*").strip()
        parts = line.split(None, 4)
        if len(parts) < 4 or "." not in parts[0]:
            continue
        units.append({"unit": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3],
                      "description": parts[4] if len(parts) > 4 else ""})
    return units


def parse_desktop_entry(text):
    d = {}
    in_entry = False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if in_entry and "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            d.setdefault(k.strip(), v.strip())
    return d


def decode_net_addr(s):
    """'0100007F:0035' -> '127.0.0.1:53' (the /proc/net/tcp encoding, also IPv6)."""
    host, _, port = s.partition(":")
    try:
        port = int(port, 16)
        raw = bytes(bytearray.fromhex(host))
        if len(raw) == 4:
            return "%s:%d" % (socket.inet_ntop(socket.AF_INET, raw[::-1]), port)
        if len(raw) == 16:
            fixed = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
            return "[%s]:%d" % (socket.inet_ntop(socket.AF_INET6, fixed), port)
    except (ValueError, OSError):
        pass
    return s


TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2",
              "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}


def parse_socket_table(text, proto):
    out = {}
    for line in (text or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        try:
            inode = int(f[9])
        except ValueError:
            continue
        if inode == 0:
            continue
        if proto.startswith("tcp"):
            state = TCP_STATES.get(f[3].upper(), f[3])
        else:
            state = "CONNECTED" if f[3] == "01" else ""
        out[inode] = (proto, decode_net_addr(f[1]), decode_net_addr(f[2]), state)
    return out


_IO_ACCOUNTING = []


def io_accounting():
    """False on kernels built without CONFIG_TASK_IO_ACCOUNTING (WSL, some embedded)."""
    if not _IO_ACCOUNTING:
        _IO_ACCOUNTING.append(os.path.exists(PROC + "/self/io") or os.path.exists(PROC + "/1/io"))
    return _IO_ACCOUNTING[0]


def read_cmdline(pid):
    raw = read_text("%s/%d/cmdline" % (PROC, pid), "") or ""
    return [a for a in raw.split("\x00") if a] if raw else []


def derive_name(comm, args, kernel):
    if kernel or not args:
        return comm
    base = os.path.basename(args[0].split(" ")[0]) if args[0] else ""
    if len(comm) >= 15 and base.startswith(comm):   # comm is cut to 15 characters
        return base
    return comm

# ---------------------------------------------------------------------------
# Samplers: each one turns two snapshots of a kernel counter file into rates
# ---------------------------------------------------------------------------


class CpuSampler(object):
    def __init__(self):
        self.static = cpu_static_info()
        self.prev = None
        self.usage = 0.0
        self.breakdown = {"user": 0.0, "system": 0.0, "iowait": 0.0, "steal": 0.0, "idle": 100.0}
        self.per_cpu = {}
        self.rates = {"ctxt": 0.0, "intr": 0.0, "forks": 0.0}
        self.procs_running = 0
        self.procs_blocked = 0
        self.btime = None
        self.loadavg = (0.0, 0.0, 0.0)
        self.threads_total = 0
        self.freq_khz = None
        self._mhz = None
        self._mhz_at = -1e9
        self._has_cpufreq = True

    def update(self, dt):
        st = parse_proc_stat(read_text(PROC + "/stat", ""))
        prev = self.prev
        if prev is not None and st["cpu"] and prev["cpu"]:
            self.usage, self.breakdown = cpu_percent(prev["cpu"], st["cpu"])
            self.per_cpu = dict((i, cpu_percent(prev["cpus"][i], v)[0])
                                for i, v in st["cpus"].items() if i in prev["cpus"])
            if dt:
                for key, name in (("ctxt", "ctxt"), ("intr", "intr"), ("processes", "forks")):
                    if key in st and key in prev:
                        self.rates[name] = max(0, st[key] - prev[key]) / dt
        else:
            self.per_cpu = dict((i, 0.0) for i in st["cpus"])
        self.prev = st
        self.procs_running = st.get("procs_running", 0)
        self.procs_blocked = st.get("procs_blocked", 0)
        self.btime = st.get("btime", self.btime)
        parts = (read_text(PROC + "/loadavg", "") or "").split()
        if len(parts) >= 4:
            try:
                self.loadavg = tuple(float(x) for x in parts[:3])
                self.threads_total = int(parts[3].split("/")[1])
            except (ValueError, IndexError):
                pass
        self.freq_khz = self._freq(sorted(st["cpus"]))

    def _freq(self, cpus):
        vals = []
        if self._has_cpufreq:
            for i in cpus:
                v = read_int("%s/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % (SYS, i))
                if v:
                    vals.append(v)
            if not vals:
                self._has_cpufreq = False
        if vals:
            return sum(vals) / float(len(vals))
        now = time.monotonic()
        if now - self._mhz_at > 5:
            self._mhz_at = now
            self._mhz = cpuinfo_mhz()
        return self._mhz


class MemSampler(object):
    def __init__(self):
        self.raw = {}
        self.info = memory_summary({})

    def update(self):
        self.raw = parse_meminfo(read_text(PROC + "/meminfo", ""))
        self.info = memory_summary(self.raw)


WHOLE_DISK_RE = re.compile(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+|sr\d+)$")


def list_block_disks(stats, show_all=False):
    sysblock = SYS + "/block"
    entries = listdir(sysblock)
    if not entries:
        return sorted((n for n in stats if WHOLE_DISK_RE.match(n)), key=natural_key)
    out = []
    for n in sorted(entries, key=natural_key):
        if n not in stats:
            continue
        if not show_all:
            if n.startswith(("loop", "ram", "zram", "fd")):
                continue
            if not os.path.exists(sysblock + "/" + n + "/device"):
                continue
            if not read_int(sysblock + "/" + n + "/size", 0):
                continue
        out.append(n)
    return out


def disk_info(name):
    base = SYS + "/block/" + name
    model = read_line(base + "/device/model") or read_line(base + "/device/name")
    vendor = read_line(base + "/device/vendor")
    if vendor and model and vendor.upper() != "ATA" and not model.lower().startswith(vendor.lower()):
        model = vendor + " " + model
    rot = read_int(base + "/queue/rotational")
    removable = read_int(base + "/removable") == 1
    if name.startswith("nvme"):
        kind = "NVMe SSD"
    elif name.startswith("mmcblk"):
        kind = "SD/eMMC"
    elif name.startswith("sr"):
        kind = "Optical"
    elif name.startswith(("zram", "ram")):
        kind = "RAM disk"
    elif name.startswith("loop"):
        kind = "Loop"
    elif name.startswith("dm-"):
        kind = "Mapper"
    elif name.startswith("md"):
        kind = "RAID"
    elif rot == 0:
        kind = "SSD"
    elif rot == 1:
        kind = "HDD"
    else:
        kind = "Disk"
    if removable and kind in ("SSD", "HDD", "Disk"):
        kind = "Removable"
    return {"name": name, "model": clean(model) if model else None, "size": (read_int(base + "/size") or 0) * 512,
            "kind": kind, "removable": removable, "label": read_line(base + "/dm/name")}


class DiskSampler(object):
    def __init__(self, show_all=False):
        self.show_all = show_all
        self.prev = None
        self.devices = collections.OrderedDict()
        self.order = []
        self._info = {}

    def update(self, dt):
        stats = parse_diskstats(read_text(PROC + "/diskstats", ""))
        devices = collections.OrderedDict()
        for n in list_block_disks(stats, self.show_all):
            info = self._info.get(n)
            if info is None:
                info = self._info[n] = disk_info(n)
            s = stats[n]
            d = dict(info)
            d["read_total"] = s[2] * 512
            d["write_total"] = s[6] * 512
            d["inflight"] = s[8]
            p = self.prev.get(n) if self.prev else None
            if p is not None and dt:
                reads, writes = max(0, s[0] - p[0]), max(0, s[4] - p[4])
                d["read_bps"] = max(0, s[2] - p[2]) * 512 / dt
                d["write_bps"] = max(0, s[6] - p[6]) * 512 / dt
                d["read_iops"] = reads / dt
                d["write_iops"] = writes / dt
                d["active"] = min(100.0, max(0, s[9] - p[9]) / (dt * 10.0))
                ios = reads + writes
                d["response_ms"] = (max(0, s[3] - p[3]) + max(0, s[7] - p[7])) / float(ios) if ios else 0.0
            else:
                d.update(read_bps=0.0, write_bps=0.0, read_iops=0.0, write_iops=0.0, active=0.0, response_ms=0.0)
            devices[n] = d
        self.prev = stats
        self.devices = devices
        self.order = list(devices)


PSEUDO_FS = frozenset((
    "proc sysfs devtmpfs devpts tmpfs cgroup cgroup2 securityfs pstore debugfs tracefs configfs fusectl "
    "mqueue hugetlbfs bpf autofs binfmt_misc efivarfs rpc_pipefs nsfs overlay squashfs ramfs selinuxfs "
    "fuse.gvfsd-fuse fuse.portal fuse.lxcfs fuse.snapfuse nfsd binder rootfs devfs ".split()))


def _unescape_mount(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def physical_disks(majmin, depth=0):
    """Whole disks under a block device (follows partitions, LVM, LUKS, RAID)."""
    if depth > 8:
        return set()
    path = os.path.realpath(SYS + "/dev/block/" + majmin)
    if not os.path.isdir(path):
        return set()
    name = os.path.basename(path)
    if os.path.exists(path + "/partition"):
        name = os.path.basename(os.path.dirname(path))
    slaves = listdir(SYS + "/block/" + name + "/slaves")
    if slaves:
        out = set()
        for s in slaves:
            dev = read_line(SYS + "/class/block/" + s + "/dev")
            if dev:
                out |= physical_disks(dev, depth + 1)
        return out
    return {name}


def list_filesystems():
    text = read_text(PROC + "/self/mountinfo") or ""
    out = []
    seen = set()
    for line in text.splitlines():
        left, found, right = line.partition(" - ")
        if not found:
            continue
        lf, rf = left.split(), right.split()
        if len(lf) < 6 or len(rf) < 2:
            continue
        majmin, mnt, fstype, source = lf[2], _unescape_mount(lf[4]), rf[0], _unescape_mount(rf[1])
        if fstype in PSEUDO_FS or source.startswith("/dev/loop"):
            continue
        key = source if source.startswith("/dev/") else majmin
        if key in seen:
            continue
        try:
            sv = os.statvfs(mnt)
        except OSError:
            continue
        total = sv.f_blocks * sv.f_frsize
        if total <= 0:
            continue
        seen.add(key)
        free = sv.f_bfree * sv.f_frsize
        avail = sv.f_bavail * sv.f_frsize
        used = total - free
        out.append({"mount": clean(mnt), "fstype": fstype, "source": clean(source), "total": total, "used": used,
                    "available": avail, "pct": used * 100.0 / (used + avail) if used + avail else 0.0,
                    "disks": sorted(physical_disks(majmin)), "readonly": "ro" in lf[5].split(",")})
    return out


def ipv4_address(ifname):
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            req = struct.pack("256s", ifname[:15].encode())
            addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])        # SIOCGIFADDR
            try:
                mask = fcntl.ioctl(s.fileno(), 0x891b, req)[20:24]                       # SIOCGIFNETMASK
                bits = bin(struct.unpack("!I", mask)[0]).count("1")
                return "%s/%d" % (addr, bits)
            except (OSError, IOError):
                return addr
        finally:
            s.close()
    except (OSError, IOError, ImportError, ValueError):
        return None


def ipv6_addresses():
    out = {}
    for line in (read_text(PROC + "/net/if_inet6", "") or "").splitlines():
        f = line.split()
        if len(f) < 6:
            continue
        try:
            addr = socket.inet_ntop(socket.AF_INET6, bytes(bytearray.fromhex(f[0])))
            plen, scope = int(f[2], 16), int(f[3], 16)
        except (ValueError, OSError):
            continue
        out.setdefault(f[5], []).append(("%s/%d" % (addr, plen), scope))
    for name in out:   # global addresses first, link-local last
        out[name] = [a for a, _ in sorted(out[name], key=lambda x: x[1])]
    return out


def default_gateways():
    out = {}
    for line in (read_text(PROC + "/net/route", "") or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 4 or f[1] != "00000000":
            continue
        try:
            if int(f[3], 16) & 2:
                out.setdefault(f[0], socket.inet_ntoa(struct.pack("<I", int(f[2], 16))))
        except (ValueError, struct.error):
            pass
    return out


def wifi_signals():
    out = {}
    for line in (read_text(PROC + "/net/wireless", "") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) >= 3:
            try:
                out[name.strip()] = float(f[2].rstrip("."))
            except ValueError:
                pass
    return out


def iface_info(name, v6, gateways, wifi):
    base = SYS + "/class/net/" + name
    if os.path.isdir(base + "/wireless") or name in wifi:
        kind = "Wi-Fi"
    elif name == "lo":
        kind = "Loopback"
    elif os.path.isdir(base + "/bridge"):
        kind = "Bridge"
    elif os.path.exists(base + "/tun_flags") or name.startswith(("tun", "tap", "wg", "tailscale", "zt", "ppp")):
        kind = "VPN"
    elif name.startswith("wwan"):
        kind = "Mobile"
    elif name.startswith(("docker", "br-", "virbr", "cni", "flannel", "cali", "vxlan", "podman")):
        kind = "Virtual"
    elif os.path.exists(base + "/device"):
        kind = "Ethernet"
    else:
        kind = "Virtual"
    speed = read_int(base + "/speed")
    return {"name": name, "type": kind, "state": read_line(base + "/operstate", "unknown"),
            "speed_mbps": speed if speed and speed > 0 else None, "mac": read_line(base + "/address"),
            "mtu": read_int(base + "/mtu"), "ipv4": ipv4_address(name), "ipv6": v6.get(name, []),
            "gateway": gateways.get(name), "signal_dbm": wifi.get(name)}


class NetSampler(object):
    def __init__(self, show_all=False):
        self.show_all = show_all
        self.prev = None
        self.ifaces = collections.OrderedDict()
        self.order = []
        self._info = {}
        self._info_at = -1e9

    def update(self, dt):
        stats = parse_net_dev(read_text(PROC + "/net/dev", ""))
        now = time.monotonic()
        if now - self._info_at > 5:
            self._info_at = now
            v6, gws, wifi = ipv6_addresses(), default_gateways(), wifi_signals()
            self._info = dict((n, iface_info(n, v6, gws, wifi)) for n in stats)
        def order(n):
            info = self._info.get(n) or {}
            return ({"Ethernet": 0, "Wi-Fi": 0, "Mobile": 1, "VPN": 2}.get(info.get("type"), 3), natural_key(n))
        out = collections.OrderedDict()
        for name in sorted(stats, key=order):
            if not self.show_all and (name == "lo" or name.startswith(("veth", "vnet"))):
                continue
            s = stats[name]
            d = dict(self._info.get(name) or iface_info(name, {}, {}, {}))
            d.update(s)
            p = self.prev.get(name) if self.prev else None
            if p is not None and dt:
                d["rx_bps"] = max(0, s["rx_bytes"] - p["rx_bytes"]) / dt
                d["tx_bps"] = max(0, s["tx_bytes"] - p["tx_bytes"]) / dt
                d["rx_pps"] = max(0, s["rx_packets"] - p["rx_packets"]) / dt
                d["tx_pps"] = max(0, s["tx_packets"] - p["tx_packets"]) / dt
            else:
                d.update(rx_bps=0.0, tx_bps=0.0, rx_pps=0.0, tx_pps=0.0)
            out[name] = d
        self.prev = stats
        self.ifaces = out
        self.order = list(out)

# --- GPU -------------------------------------------------------------------


# Render drivers of GPUs that are not PCI devices (phones, boards, Apple silicon).
SOC_GPU_DRIVERS = frozenset(("msm", "msm_drm", "panfrost", "panthor", "lima", "v3d", "vc4", "etnaviv", "asahi",
                             "powervr", "pvr", "tegra", "nouveau", "mali", "rockchip", "mediatek", "imagination",
                             "virtio_gpu", "virtio-gpu", "exynos", "meson", "sun4i-drm", "imx-drm"))
GPU_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel", "0x1af4": "Virtio", "0x15ad": "VMware",
               "0x1234": "QEMU", "0x5143": "Qualcomm", "0x13b5": "ARM", "0x1414": "Microsoft", "0x1ad0": "Matrox",
               "0x102b": "Matrox", "0x1a03": "ASPEED"}
_PCI_NAMES = {}


def pci_device_name(vendor, device):
    """Look a PCI device up in pci.ids (hwdata), e.g. 'GeForce RTX 4070'."""
    key = (vendor, device)
    if key in _PCI_NAMES:
        return _PCI_NAMES[key]
    v = (vendor or "").lower().replace("0x", "")
    dv = (device or "").lower().replace("0x", "")
    name = None
    for path in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids", "/usr/share/hwdata/pci.ids.d"):
        text = read_text(path)
        if not text:
            continue
        in_vendor = False
        for line in text.splitlines():
            if not line or line[0] == "#":
                continue
            if line[0] != "\t":
                if in_vendor:
                    break
                in_vendor = line[:4].lower() == v
            elif in_vendor and line[1] != "\t" and line[1:5].lower() == dv:
                name = line[5:].strip()
                break
        if name:
            break
    if name:
        m = re.search(r"\[([^\]]+)\]", name)
        name = m.group(1) if m else name
    _PCI_NAMES[key] = name
    return name


def discover_drm_cards():
    cards = []
    for n in sorted(listdir(SYS + "/class/drm"), key=natural_key):
        if not re.match(r"card\d+$", n):
            continue
        dev = SYS + "/class/drm/" + n + "/device"
        uevent = parse_kv(read_text(dev + "/uevent", ""), "=")
        driver = uevent.get("DRIVER") or os.path.basename(readlink(dev + "/driver") or "")
        vendor = read_line(dev + "/vendor")
        if not vendor and driver not in SOC_GPU_DRIVERS:
            continue   # simpledrm, vkms, WSL's faux_driver ... are not GPUs
        device = read_line(dev + "/device")
        vname = GPU_VENDORS.get(vendor, vendor or "")
        model = pci_device_name(vendor, device) if vendor and device else None
        cards.append({"card": n, "dev": dev, "vendor": vendor, "vendor_name": vname, "driver": driver or "?",
                      "pci": uevent.get("PCI_SLOT_NAME"),
                      "name": clean(("%s %s" % (vname, model)).strip() if model else ("%s GPU (%s)" % (vname, driver or n)).strip())})
    return cards


def query_nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    fields = "index,name,pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.gr,driver_version,fan.speed"
    out = run_cmd([exe, "--query-gpu=" + fields, "--format=csv,noheader,nounits"], timeout=5)
    if out is None:
        return None

    def num(x):
        try:
            return float(x)
        except ValueError:
            return None
    gpus = []
    for line in out.splitlines():
        v = [x.strip() for x in line.split(",")]
        if len(v) < 11:
            continue
        used, total = num(v[4]), num(v[5])
        gpus.append({"id": "nvidia" + v[0], "name": clean(v[1]), "vendor": "NVIDIA", "driver": "nvidia " + v[9],
                     "pci": v[2], "util": num(v[3]), "vram_used": used * 1048576 if used is not None else None,
                     "vram_total": total * 1048576 if total is not None else None, "temp": num(v[6]),
                     "power": num(v[7]), "clock_mhz": num(v[8]), "fan": num(v[10]), "source": "nvidia-smi"})
    procs = {}
    out = run_cmd([exe, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"], timeout=5)
    for line in (out or "").splitlines():
        v = [x.strip() for x in line.split(",")]
        if len(v) >= 2 and v[0].isdigit() and num(v[1]) is not None:
            procs[int(v[0])] = procs.get(int(v[0]), 0) + num(v[1]) * 1048576
    return {"gpus": gpus, "procs": procs}


class GpuSampler(object):
    def __init__(self):
        self.cards = discover_drm_cards()
        self.has_drm = any(n.startswith("renderD") for n in listdir(SYS + "/class/drm")) or bool(self.cards)
        self.nv_task = BackgroundTask(query_nvidia_smi, 2.0) if shutil.which("nvidia-smi") else None
        self.gpus = []
        self.nv_procs = {}

    def update(self, procs):
        nv = self.nv_task.get() if self.nv_task else None
        gpus = list(nv["gpus"]) if nv else []
        self.nv_procs = nv["procs"] if nv else {}
        for c in self.cards:
            if c["vendor"] == "0x10de" and gpus:
                continue   # nvidia-smi already describes it, with more detail
            dev = c["dev"]
            g = {"id": c["card"], "name": c["name"], "vendor": c["vendor_name"], "driver": c["driver"], "pci": c["pci"],
                 "util": read_int(dev + "/gpu_busy_percent"), "vram_used": read_int(dev + "/mem_info_vram_used"),
                 "vram_total": read_int(dev + "/mem_info_vram_total"), "temp": None, "power": None,
                 "clock_mhz": None, "fan": None, "source": "sysfs"}
            for h in listdir(dev + "/hwmon"):
                hb = dev + "/hwmon/" + h
                t = read_int(hb + "/temp1_input")
                g["temp"] = t / 1000.0 if t is not None else None
                p = read_int(hb + "/power1_average") or read_int(hb + "/power1_input")
                g["power"] = p / 1e6 if p else None
                f = read_int(hb + "/freq1_input")
                g["clock_mhz"] = f / 1e6 if f else None
                fan = read_int(hb + "/fan1_input")
                g["fan"] = fan
                break
            if g["clock_mhz"] is None:
                f = read_int(SYS + "/class/drm/" + c["card"] + "/gt_cur_freq_mhz") or read_int(dev + "/tile0/gt0/freq0/cur_freq")
                g["clock_mhz"] = float(f) if f else None
            if g["util"] is None and c["pci"] and c["pci"] in procs.gpu_by_pdev:
                g["util"] = procs.gpu_by_pdev[c["pci"]]
                g["source"] = "fdinfo"
            gpus.append(g)
        self.gpus = gpus
        for pid, mem in self.nv_procs.items():
            p = procs.procs.get(pid)
            if p is not None:
                p.gpu_mem = (p.gpu_mem or 0) + mem

# --- Sensors and power -------------------------------------------------------


CPU_TEMP_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "x86_pkg_temp", "acpitz")
_SCALE = {"temp": 1000.0, "fan": 1.0, "in": 1000.0, "power": 1e6, "curr": 1000.0}


def discover_sensors():
    out = []
    for h in sorted(listdir(SYS + "/class/hwmon"), key=natural_key):
        base = SYS + "/class/hwmon/" + h
        chip = read_line(base + "/name") or h
        dirs = [base]
        if not any(f.endswith("_input") for f in listdir(base)):
            dirs.append(base + "/device")
        for d in dirs:
            files = set(listdir(d))
            for f in sorted(files, key=natural_key):
                m = re.match(r"^(temp|fan|in|power|curr)(\d+)_(input|average)$", f)
                if not m:
                    continue
                kind, idx, which = m.groups()
                if which == "average" and "%s%s_input" % (kind, idx) in files:
                    continue
                stem = "%s/%s%s" % (d, kind, idx)
                scale = _SCALE[kind]
                hi = read_int(stem + "_max")
                crit = read_int(stem + "_crit")
                out.append({"chip": chip, "label": clean(read_line(stem + "_label") or "%s%s" % (kind, idx)),
                            "kind": kind, "path": d + "/" + f, "scale": scale,
                            "max": hi / scale if hi else None, "crit": crit / scale if crit else None})
    if not any(s["kind"] == "temp" for s in out):
        for z in sorted(listdir(SYS + "/class/thermal"), key=natural_key):
            if z.startswith("thermal_zone"):
                base = SYS + "/class/thermal/" + z
                out.append({"chip": "thermal", "label": read_line(base + "/type") or z, "kind": "temp",
                            "path": base + "/temp", "scale": 1000.0, "max": None, "crit": None})
    return out


class SensorSampler(object):
    def __init__(self):
        self.sensors = []
        self._found_at = -1e9
        self.cpu_temp = None
        self.max_temp = None

    def update(self):
        now = time.monotonic()
        if now - self._found_at > 60:
            self._found_at = now
            self.sensors = discover_sensors()
        temps = []
        for s in self.sensors:
            v = read_int(s["path"])
            s["value"] = v / s["scale"] if v is not None else None
            if s["kind"] == "temp" and s["value"] is not None and -40 < s["value"] < 200:
                temps.append(s)
        self.max_temp = max(t["value"] for t in temps) if temps else None
        self.cpu_temp = None
        for chip in CPU_TEMP_CHIPS:
            mine = [t for t in temps if t["chip"] == chip or (chip == "x86_pkg_temp" and t["label"] == chip)]
            if mine:
                pref = [t for t in mine if t["label"].startswith(("Package", "Tctl", "Tdie"))]
                self.cpu_temp = pref[0]["value"] if pref else max(t["value"] for t in mine)
                break


class PowerSampler(object):
    def __init__(self):
        self.batteries = []
        self.ac_online = None

    def update(self):
        bats = []
        ac = None
        for n in sorted(listdir(SYS + "/class/power_supply"), key=natural_key):
            base = SYS + "/class/power_supply/" + n
            kind = read_line(base + "/type")
            if kind in ("Mains", "USB"):
                online = read_int(base + "/online")
                if online is not None:
                    ac = bool(online) or bool(ac)
                continue
            if kind != "Battery" or read_line(base + "/scope") == "Device":
                continue
            volt = read_int(base + "/voltage_now") or read_int(base + "/voltage_min_design")

            def energy(name):
                v = read_int(base + "/energy_" + name)
                if v is None:
                    c = read_int(base + "/charge_" + name)
                    if c is not None and volt:
                        v = c * volt / 1e6
                return v
            now, full, design = energy("now"), energy("full"), energy("full_design")
            power = read_int(base + "/power_now")
            if power is None:
                cur = read_int(base + "/current_now")
                power = cur * volt / 1e6 if cur is not None and volt else None
            status = read_line(base + "/status", "Unknown")
            cap = read_int(base + "/capacity")
            if cap is None and now and full:
                cap = int(round(now * 100.0 / full))
            left = None
            if power and power > 0 and now is not None:
                if status == "Discharging":
                    left = now / float(power)
                elif status == "Charging" and full:
                    left = max(0, full - now) / float(power)
            bats.append({"name": n, "capacity": cap, "status": status, "power_w": power / 1e6 if power else None,
                         "energy_now_wh": now / 1e6 if now else None, "energy_full_wh": full / 1e6 if full else None,
                         "energy_design_wh": design / 1e6 if design else None,
                         "health_pct": full * 100.0 / design if full and design else None, "hours_left": left,
                         "cycles": read_int(base + "/cycle_count"), "technology": read_line(base + "/technology"),
                         "model": clean(" ".join(x for x in (read_line(base + "/manufacturer"), read_line(base + "/model_name")) if x)) or None})
        self.batteries = bats
        self.ac_online = ac

# --- Processes ---------------------------------------------------------------


class ProcInfo(object):
    __slots__ = ("pid", "ppid", "starttime", "comm", "name", "cmd", "uid", "user", "state", "nice", "priority",
                 "threads", "cpu_ticks", "cpu_pct", "rss", "shared", "private", "vsize", "rbytes", "wbytes",
                 "read_rate", "write_rate", "io_denied", "gpu_pct", "gpu_mem", "unit", "user_unit", "container",
                 "cgroup", "tty", "kernel", "processor", "nfds", "start_epoch")

    def __init__(self, pid):
        self.pid = pid
        self.ppid = 0
        self.starttime = 0
        self.comm = self.name = self.cmd = self.user = ""
        self.uid = -1
        self.state = "?"
        self.nice = self.priority = self.threads = 0
        self.cpu_ticks = 0
        self.cpu_pct = 0.0
        self.rss = self.shared = self.private = self.vsize = 0
        self.rbytes = self.wbytes = None
        self.read_rate = self.write_rate = None
        self.io_denied = False
        self.gpu_pct = self.gpu_mem = None
        self.unit = self.user_unit = self.container = self.cgroup = self.tty = None
        self.kernel = False
        self.processor = -1
        self.nfds = None
        self.start_epoch = None

    @property
    def io_rate(self):
        if self.read_rate is None:
            return None
        return self.read_rate + self.write_rate


class ProcSampler(object):
    """The process table. CPU% is relative to one core here (100% = one core busy)."""

    def __init__(self, ncpu=None):
        self.ncpu = max(1, ncpu or os.cpu_count() or 1)
        self.procs = {}
        self.last_t = None
        self.btime = parse_proc_stat(read_text(PROC + "/stat", "")).get("btime", 0)
        self.io_ok = io_accounting()
        self.total_threads = 0
        self.running = 0
        self.gpu_by_pdev = {}
        self._drm_fds = {}
        self._drm_scan_at = -1e9
        self._drm_prev = {}

    def update(self, now=None, scan_gpu=False):
        now = time.monotonic() if now is None else now
        dt = now - self.last_t if self.last_t is not None else None
        old = self.procs
        new = {}
        for entry in listdir(PROC):
            if entry.isdigit():
                pid = int(entry)
                p = self._sample(pid, old.get(pid), dt)
                if p is not None:
                    new[pid] = p
        self.procs = new
        self.last_t = now
        self.total_threads = sum(p.threads for p in new.values())
        self.running = sum(1 for p in new.values() if p.state == "R")
        if scan_gpu:
            self._sample_gpu(now, dt)

    def _sample(self, pid, prev, dt):
        base = "%s/%d" % (PROC, pid)
        st = parse_pid_stat(read_text(base + "/stat"))
        if st is None:
            return None
        same = prev is not None and prev.starttime == st["starttime"]
        if same:
            p = prev
            if p.comm != st["comm"]:   # exec() replaced the program
                self._identity(p, st, base)
        else:
            p = ProcInfo(pid)
            p.starttime = st["starttime"]
            p.start_epoch = self.btime + st["starttime"] / float(CLK_TCK) if self.btime else None
            self._identity(p, st, base)
        p.ppid = st["ppid"]
        p.state = st["state"]
        p.gpu_pct = p.gpu_mem = None   # filled in by _sample_gpu and GpuSampler
        p.nice = st["nice"]
        p.priority = st["priority"]
        p.threads = st["threads"]
        p.processor = st["processor"]
        ticks = st["utime"] + st["stime"]
        if dt:
            delta = ticks - p.cpu_ticks if same else ticks
            p.cpu_pct = max(0.0, min(delta / float(CLK_TCK) / dt * 100.0, 100.0 * self.ncpu))
        else:
            p.cpu_pct = 0.0
        p.cpu_ticks = ticks
        statm = (read_text(base + "/statm") or "").split()
        if len(statm) >= 3:
            p.vsize = int(statm[0]) * PAGE_SIZE
            p.rss = int(statm[1]) * PAGE_SIZE
            p.shared = int(statm[2]) * PAGE_SIZE
            p.private = max(0, p.rss - p.shared)
        if not p.io_denied and self.io_ok:
            io = read_text(base + "/io")
            if io is None:
                p.io_denied = os.path.exists(base)
                p.read_rate = p.write_rate = None
            else:
                vals = parse_kv_ints(io)
                rb, wb = vals.get("read_bytes", 0), vals.get("write_bytes", 0)
                if dt and same and p.rbytes is not None:
                    p.read_rate = max(0, rb - p.rbytes) / dt
                    p.write_rate = max(0, wb - p.wbytes) / dt
                elif dt and not same:
                    p.read_rate, p.write_rate = rb / dt, wb / dt
                else:
                    p.read_rate = p.write_rate = 0.0
                p.rbytes, p.wbytes = rb, wb
        return p

    def _identity(self, p, st, base):
        p.comm = clean(st["comm"])
        p.kernel = bool(st["flags"] & KTHREAD_FLAG) or p.pid == 2 or st["ppid"] == 2
        args = [] if p.kernel else read_cmdline(p.pid)
        p.cmd = clean(" ".join(args))
        p.name = clean(derive_name(st["comm"], args, p.kernel))
        if st["state"] == "Z":
            p.name += " <defunct>"
        status = parse_kv(read_text(base + "/status", ""))
        try:
            p.uid = int(status.get("Uid", "").split()[1])
        except (IndexError, ValueError):
            try:
                p.uid = os.stat(base).st_uid
            except OSError:
                p.uid = -1
        p.user = user_name(p.uid) if p.uid >= 0 else "?"
        p.tty = decode_tty(st["tty_nr"])
        p.cgroup, p.unit, p.user_unit, p.container = parse_cgroup(read_text(base + "/cgroup", ""))

    def _sample_gpu(self, now, dt):
        """Per-process GPU use from DRM fdinfo; open fds are rescanned every 5 s."""
        if now - self._drm_scan_at > 5.0:
            self._drm_scan_at = now
            found = {}
            for pid, p in self.procs.items():
                if p.kernel:
                    continue
                fddir = "%s/%d/fd" % (PROC, pid)
                try:
                    names = os.listdir(fddir)
                except OSError:
                    continue
                p.nfds = len(names)
                fds = [fd for fd in names if (readlink(fddir + "/" + fd) or "").startswith("/dev/dri/")]
                if fds:
                    found[pid] = fds
            self._drm_fds = found
        clients = {}
        for pid, fds in self._drm_fds.items():
            if pid not in self.procs:
                continue
            for fd in fds:
                info = parse_drm_fdinfo(read_text("%s/%d/fdinfo/%s" % (PROC, pid, fd)))
                if info and "client" in info:
                    clients.setdefault((info.get("pdev", ""), info["client"]), (pid, info))
        engines = {}
        for key, (pid, info) in clients.items():
            p = self.procs[pid]
            busy = drm_client_busy(info, self._drm_prev.get(key), dt)
            if busy:
                p.gpu_pct = max(p.gpu_pct or 0.0, max(busy.values()) * 100.0)
            elif p.gpu_pct is None:
                p.gpu_pct = 0.0
            if info["mem"]:
                p.gpu_mem = (p.gpu_mem or 0) + info["mem"]
            per = engines.setdefault(key[0], {})
            for eng, frac in busy.items():
                per[eng] = per.get(eng, 0.0) + frac
        self._drm_prev = dict((k, v[1]) for k, v in clients.items())
        self.gpu_by_pdev = dict((pdev, min(100.0, max(e.values()) * 100.0) if e else 0.0)
                                for pdev, e in engines.items() if pdev)

# ---------------------------------------------------------------------------
# Views over the process table: tree, application groups, users, services
# ---------------------------------------------------------------------------


class ProcGroup(object):
    """Processes with the same name added together (Task Manager's "Apps")."""

    def __init__(self, name, members):
        self.name = name
        self.members = members
        self.count = len(members)
        self.pid = min(p.pid for p in members)
        users = set(p.user for p in members)
        self.user = members[0].user if len(users) == 1 else "%d users" % len(users)
        self.uid = members[0].uid
        self.state = "R" if any(p.state == "R" for p in members) else members[0].state
        self.cpu_pct = sum(p.cpu_pct for p in members)
        self.private = sum(p.private for p in members)
        self.rss = sum(p.rss for p in members)
        known = [p for p in members if p.read_rate is not None]
        self.read_rate = sum(p.read_rate for p in known) if known else None
        self.write_rate = sum(p.write_rate for p in known) if known else None
        gpu = [p.gpu_pct for p in members if p.gpu_pct is not None]
        self.gpu_pct = min(100.0, sum(gpu)) if gpu else None
        mem = [p.gpu_mem for p in members if p.gpu_mem]
        self.gpu_mem = sum(mem) if mem else None
        self.threads = sum(p.threads for p in members)
        self.nice = members[0].nice
        self.cpu_ticks = sum(p.cpu_ticks for p in members)
        self.cmd = members[0].cmd
        self.comm = name
        self.starttime = min(p.starttime for p in members)
        self.kernel = all(p.kernel for p in members)
        self.unit = members[0].unit

    @property
    def io_rate(self):
        return None if self.read_rate is None else self.read_rate + self.write_rate


def make_matcher(text):
    """Filter: every word must match. `user:NAME` matches a user, anything else a substring."""
    tokens = (text or "").lower().split()
    if not tokens:
        return None

    def match(p):
        hay = None
        for t in tokens:
            if t.startswith("user:"):
                if p.user.lower() != t[5:]:
                    return False
                continue
            if hay is None:
                hay = ("%d %s %s %s %s %s %s" % (p.pid, p.name, p.cmd, p.user, p.unit or "", p.user_unit or "",
                                                 p.container or "")).lower()
            if t not in hay:
                return False
        return True
    return match


def build_tree(procs, sort_fn, reverse, collapsed=frozenset()):
    """Depth-first parent/child order. Returns [(proc, prefix, has_children)]."""
    by_pid = dict((p.pid, p) for p in procs)
    children = {}
    roots = []
    for p in procs:
        if p.ppid in by_pid and p.ppid != p.pid:
            children.setdefault(p.ppid, []).append(p)
        else:
            roots.append(p)

    def order(lst):
        lst.sort(key=lambda q: q.pid)
        lst.sort(key=sort_fn, reverse=reverse)
    order(roots)
    for lst in children.values():
        order(lst)
    out = []
    stack = [(p, "", True, True) for p in reversed(roots)]
    while stack:
        p, pre, last, is_root = stack.pop()
        kids = children.get(p.pid, [])
        out.append((p, pre + ("" if is_root else G["tree_last"] if last else G["tree_mid"]), bool(kids)))
        if kids and p.pid not in collapsed:
            child_pre = pre if is_root else pre + (G["tree_gap"] if last else G["tree_pipe"])
            for i in range(len(kids) - 1, -1, -1):
                stack.append((kids[i], child_pre, i == len(kids) - 1, False))
    return out


def build_groups(procs):
    groups = collections.OrderedDict()
    for p in procs:
        groups.setdefault(p.name, []).append(p)
    return [ProcGroup(name, members) for name, members in groups.items()]


def user_rows(procs):
    users = {}
    for p in procs.values():
        u = users.get(p.uid)
        if u is None:
            u = users[p.uid] = {"user": p.user, "uid": p.uid, "procs": 0, "threads": 0, "cpu": 0.0, "private": 0,
                                "rss": 0, "read_rate": None, "write_rate": None, "gpu": None}
        u["procs"] += 1
        u["threads"] += p.threads
        u["cpu"] += p.cpu_pct
        u["private"] += p.private
        u["rss"] += p.rss
        if p.read_rate is not None:
            u["read_rate"] = (u["read_rate"] or 0.0) + p.read_rate
            u["write_rate"] = (u["write_rate"] or 0.0) + p.write_rate
        if p.gpu_pct is not None:
            u["gpu"] = min(100.0, (u["gpu"] or 0.0) + p.gpu_pct)
    return list(users.values())


def service_rows(units, procs, show_inactive=True):
    by_unit = {}
    for p in procs.values():
        if p.unit:
            by_unit.setdefault(p.unit, []).append(p)
    rows = []
    for u in units or []:
        if not u["unit"].endswith(".service"):
            continue
        if u["load"] == "not-found":   # referenced by another unit but not installed
            continue
        if not show_inactive and u["active"] in ("inactive", "dead") and u["sub"] != "failed":
            continue
        members = by_unit.get(u["unit"], [])
        known = [p for p in members if p.read_rate is not None]
        rows.append({"unit": u["unit"], "load": u["load"], "active": u["active"], "sub": u["sub"],
                     "description": clean(u["description"]), "pids": len(members),
                     "pid_list": sorted(p.pid for p in members), "cpu": sum(p.cpu_pct for p in members),
                     "private": sum(p.private for p in members), "rss": sum(p.rss for p in members),
                     "io_rate": sum(p.io_rate for p in known) if known else None})
    return rows


def query_units():
    out = run_cmd(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager", "--plain"])
    return parse_systemctl_units(out or "")


def real_user_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and pwd:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def startup_entries(descriptions=None):
    """Programs started at login (XDG autostart) or boot (enabled systemd services)."""
    rows = []
    home = real_user_home()
    user_dir = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "autostart")
    sys_dirs = [os.path.join(d, "autostart") for d in (os.environ.get("XDG_CONFIG_DIRS") or ETC + "/xdg").split(":") if d]
    seen = set()
    for scope, d in [("user", user_dir)] + [("system", x) for x in sys_dirs]:
        for fn in sorted(listdir(d)):
            if not fn.endswith(".desktop") or fn in seen:
                continue
            seen.add(fn)
            path = os.path.join(d, fn)
            e = parse_desktop_entry(read_text(path, ""))
            enabled = e.get("Hidden", "false").lower() != "true" and \
                e.get("X-GNOME-Autostart-enabled", "true").lower() != "false"
            only = e.get("OnlyShowIn", "").strip(";")
            rows.append({"name": clean(e.get("Name") or fn[:-8]), "type": "autostart (%s)" % scope,
                         "enabled": enabled, "command": clean(e.get("Exec", "")), "path": path,
                         "note": ("only in " + only.replace(";", ", ")) if only else ""})
    if shutil.which("systemctl"):
        for scope, argv in (("systemd", ["systemctl"]), ("systemd --user", ["systemctl", "--user"])):
            if scope == "systemd --user" and os.geteuid() == 0:
                continue
            out = run_cmd(argv + ["list-unit-files", "--type=service", "--state=enabled", "--no-legend", "--no-pager"],
                          timeout=10)
            for line in (out or "").splitlines():
                parts = line.split()
                if not parts or not parts[0].endswith(".service"):
                    continue
                rows.append({"name": parts[0], "type": scope, "enabled": True,
                             "command": (descriptions or {}).get(parts[0], ""), "path": parts[0], "note": ""})
    return rows

# ---------------------------------------------------------------------------
# Monitor: owns every sampler and the graph history
# ---------------------------------------------------------------------------


class Monitor(object):
    def __init__(self, all_disks=False, all_nics=False):
        self.static = static_system_info()
        self.cpu = CpuSampler()
        self.mem = MemSampler()
        self.disks = DiskSampler(all_disks)
        self.net = NetSampler(all_nics)
        self.gpu = GpuSampler()
        self.sensors = SensorSampler()
        self.power = PowerSampler()
        self.procs = ProcSampler(self.cpu.static["logical"])
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY))
        self.has_systemd = os.path.isdir("/run/systemd/system") and bool(shutil.which("systemctl"))
        self.fs_task = BackgroundTask(list_filesystems, 5.0)
        self.units_task = BackgroundTask(query_units, 5.0) if self.has_systemd else None
        self.startup_task = BackgroundTask(self._startup, 30.0)
        self.uptime = 0.0
        self.ticks = 0
        self.last_update = None
        self.is_root = hasattr(os, "geteuid") and os.geteuid() == 0

    def _startup(self):
        units = self.units_task.get() if self.units_task else None
        return startup_entries(dict((u["unit"], u["description"]) for u in units or []))

    def update(self):
        now = time.monotonic()
        dt = now - self.last_update if self.last_update is not None else None
        self.last_update = now
        try:
            self.uptime = float((read_text(PROC + "/uptime", "0") or "0").split()[0])
        except (ValueError, IndexError):
            pass
        self.cpu.update(dt)
        self.mem.update()
        self.disks.update(dt)
        self.net.update(dt)
        self.procs.update(now, scan_gpu=self.gpu.has_drm)
        self.gpu.update(self.procs)
        self.sensors.update()
        self.power.update()
        self.ticks += 1
        if dt:
            self._record()

    def _record(self):
        h = self.hist
        h["cpu"].append(self.cpu.usage)
        for i, v in self.cpu.per_cpu.items():
            h["cpu.%d" % i].append(v)
        h["mem"].append(self.mem.info["used_pct"])
        h["swap"].append(self.mem.info["swap_pct"])
        for n, d in self.disks.devices.items():
            h["disk.%s.active" % n].append(d["active"])
            h["disk.%s.rw" % n].append(d["read_bps"] + d["write_bps"])
        for n, d in self.net.ifaces.items():
            h["net.%s.rx" % n].append(d["rx_bps"])
            h["net.%s.tx" % n].append(d["tx_bps"])
            h["net.%s.total" % n].append(d["rx_bps"] + d["tx_bps"])
        for g in self.gpu.gpus:
            h["gpu.%s.util" % g["id"]].append(g["util"] or 0.0)
            if g["vram_total"]:
                h["gpu.%s.vram" % g["id"]].append((g["vram_used"] or 0) * 100.0 / g["vram_total"])
        if self.sensors.max_temp is not None:
            h["temp.max"].append(self.sensors.max_temp)
        for b in self.power.batteries:
            if b["capacity"] is not None:
                h["bat.%s" % b["name"]].append(b["capacity"])

    def sample(self, interval):
        """Two updates `interval` apart, so rates (CPU%, I/O/s) are meaningful."""
        self.update()
        if self.gpu.nv_task:
            self.gpu.nv_task.wait(3)
        time.sleep(max(0.1, interval))
        self.update()

    def disk_totals(self):
        devs = self.disks.devices.values()
        return sum(d["read_bps"] for d in devs), sum(d["write_bps"] for d in devs)

    def net_totals(self):
        ifs = [d for d in self.net.ifaces.values() if d["type"] not in ("Loopback", "Bridge", "Virtual")]
        return sum(d["rx_bps"] for d in ifs), sum(d["tx_bps"] for d in ifs)

    def snapshot(self, fs=None):
        c, s = self.cpu, self.cpu.static
        procs = self.procs
        return collections.OrderedDict([
            ("time", time.time()),
            ("ltm_version", __version__),
            ("host", self.static),
            ("uptime_s", self.uptime),
            ("cpu", collections.OrderedDict([
                ("model", s["model"]), ("sockets", s["sockets"]), ("cores", s["cores"]), ("logical", s["logical"]),
                ("usage_pct", round(c.usage, 2)),
                ("breakdown_pct", dict((k, round(v, 2)) for k, v in c.breakdown.items())),
                ("per_cpu_pct", [round(c.per_cpu[i], 1) for i in sorted(c.per_cpu)]),
                ("frequency_mhz", round(c.freq_khz / 1000.0) if c.freq_khz else None),
                ("max_frequency_mhz", round(s["max_khz"] / 1000.0) if s["max_khz"] else None),
                ("load_average", list(c.loadavg)),
                ("context_switches_per_s", round(c.rates["ctxt"])), ("interrupts_per_s", round(c.rates["intr"])),
                ("forks_per_s", round(c.rates["forks"], 1)),
                ("caches", dict(s["caches"])), ("temperature_c", self.sensors.cpu_temp),
                ("virtualization", "VT-x" if "vmx" in s["flags"] else "AMD-V" if "svm" in s["flags"] else None),
                ("hypervisor_present", "hypervisor" in s["flags"]),
            ])),
            ("memory", self.mem.info),
            ("disks", list(self.disks.devices.values())),
            ("filesystems", fs if fs is not None else (self.fs_task.value or [])),
            ("network", list(self.net.ifaces.values())),
            ("gpus", self.gpu.gpus),
            ("sensors", [dict((k, v) for k, v in x.items() if k not in ("path", "scale")) for x in self.sensors.sensors]),
            ("batteries", self.power.batteries),
            ("ac_online", self.power.ac_online),
            ("processes", {"count": len(procs.procs), "threads": procs.total_threads, "running": c.procs_running,
                           "blocked": c.procs_blocked}),
        ])

# ---------------------------------------------------------------------------
# One process in depth (details popup and `ltm proc`)
# ---------------------------------------------------------------------------


STATE_NAMES = {"R": "running", "S": "sleeping", "D": "waiting for I/O (uninterruptible)", "Z": "zombie (exited, not reaped)",
               "T": "stopped", "t": "stopped by debugger", "X": "dead", "I": "idle", "W": "paging", "P": "parked",
               "K": "wakekill"}
POLICY_NAMES = {0: "normal (SCHED_OTHER)", 1: "real-time FIFO", 2: "real-time round-robin", 3: "batch",
                5: "idle", 6: "deadline"}


def read_sockets(pid):
    """inode -> (proto, local, remote, state), in the network namespace of `pid`."""
    table = {}
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        table.update(parse_socket_table(read_text("%s/%d/net/%s" % (PROC, pid, proto)), proto))
    return table


def read_btime():
    m = re.search(r"^btime (\d+)", read_text(PROC + "/stat", "") or "", re.M)
    return int(m.group(1)) if m else None


def process_details(pid, prev=None, mon=None):
    base = "%s/%d" % (PROC, pid)
    st = parse_pid_stat(read_text(base + "/stat"))
    if st is None:
        return None
    now = time.monotonic()
    status = parse_kv(read_text(base + "/status", ""))
    args = read_cmdline(pid)
    kernel = bool(st["flags"] & KTHREAD_FLAG) or pid == 2 or st["ppid"] == 2
    try:
        uptime = float((read_text(PROC + "/uptime", "0") or "0").split()[0])
    except (ValueError, IndexError):
        uptime = 0.0
    btime = read_btime()
    ncpu = os.cpu_count() or 1
    d = collections.OrderedDict()
    d["pid"] = pid
    d["starttime"] = st["starttime"]
    d["_t"] = now
    d["comm"] = clean(st["comm"])
    d["name"] = clean(derive_name(st["comm"], args, kernel))
    d["kernel_thread"] = kernel
    d["ppid"] = st["ppid"]
    chain = []
    cur = st["ppid"]
    while cur and len(chain) < 64:
        pst = parse_pid_stat(read_text("%s/%d/stat" % (PROC, cur)))
        if pst is None:
            break
        chain.append((cur, clean(pst["comm"])))
        cur = pst["ppid"]
    d["parent_name"] = chain[0][1] if chain else None
    d["ancestry"] = list(reversed(chain))
    uids = [int(x) for x in status.get("Uid", "").split() if x.isdigit()]
    gids = [int(x) for x in status.get("Gid", "").split() if x.isdigit()]
    d["uid"], d["euid"] = (uids + [-1, -1])[:2]
    d["gid"], d["egid"] = (gids + [-1, -1])[:2]
    d["user"] = user_name(d["uid"]) if d["uid"] >= 0 else "?"
    d["euser"] = user_name(d["euid"]) if d["euid"] >= 0 else "?"
    d["group"] = group_name(d["gid"]) if d["gid"] >= 0 else "?"
    d["state"] = st["state"]
    d["state_name"] = STATE_NAMES.get(st["state"], "?")
    d["threads"] = st["threads"]
    d["nice"] = st["nice"]
    d["priority"] = st["priority"]
    d["policy"] = POLICY_NAMES.get(st["policy"], str(st["policy"]))
    d["rt_priority"] = st["rt_priority"]
    d["last_cpu"] = st["processor"]
    try:
        d["affinity"] = compact_ranges(os.sched_getaffinity(pid))
    except (OSError, AttributeError):
        d["affinity"] = status.get("Cpus_allowed_list")
    d["tty"] = decode_tty(st["tty_nr"])
    d["session"] = st["session"]
    d["pgrp"] = st["pgrp"]
    d["started"] = btime + st["starttime"] / float(CLK_TCK) if btime else None
    d["elapsed_s"] = max(0.0, uptime - st["starttime"] / float(CLK_TCK)) if uptime else None
    d["cgroup"], d["unit"], d["user_unit"], d["container"] = parse_cgroup(read_text(base + "/cgroup", ""))
    nspid = status.get("NSpid", "").split()
    d["namespace_pid"] = int(nspid[-1]) if len(nspid) > 1 and nspid[-1].isdigit() else None
    d["exe"] = clean(readlink(base + "/exe") or "") or None
    d["cwd"] = clean(readlink(base + "/cwd") or "") or None
    d["cmdline"] = clean(" ".join(args)) or None
    d["seccomp"] = {"0": "off", "1": "strict", "2": "filter"}.get(status.get("Seccomp"), None)

    # CPU
    ticks = st["utime"] + st["stime"]
    same = prev is not None and prev.get("pid") == pid and prev.get("starttime") == st["starttime"]
    cpu_core = None
    if same and now > prev["_t"]:
        cpu_core = max(0.0, (ticks - prev["_ticks"]) / float(CLK_TCK) / (now - prev["_t"]) * 100.0)
    elif mon is not None:
        p = mon.procs.procs.get(pid)
        if p is not None and p.starttime == st["starttime"]:
            cpu_core = p.cpu_pct
    d["_ticks"] = ticks
    d["cpu_pct_core"] = cpu_core
    d["cpu_pct"] = cpu_core / ncpu if cpu_core is not None else None
    d["cpu_user_s"] = st["utime"] / float(CLK_TCK)
    d["cpu_system_s"] = st["stime"] / float(CLK_TCK)
    d["cpu_children_s"] = (st["cutime"] + st["cstime"]) / float(CLK_TCK)
    d["io_delay_s"] = st["blkio_ticks"] / float(CLK_TCK)
    d["ctx_voluntary"] = int(status.get("voluntary_ctxt_switches", 0) or 0)
    d["ctx_involuntary"] = int(status.get("nonvoluntary_ctxt_switches", 0) or 0)
    d["faults_minor"] = st["minflt"]
    d["faults_major"] = st["majflt"]

    # Memory
    def kb(key):
        v = status.get(key)
        try:
            return int(v.split()[0]) * 1024 if v else None
        except (ValueError, IndexError):
            return None
    d["rss"] = kb("VmRSS")
    d["rss_anon"] = kb("RssAnon")
    d["rss_file"] = kb("RssFile")
    d["rss_shmem"] = kb("RssShmem")
    d["swap"] = kb("VmSwap")
    d["vsize"] = st["vsize"]
    d["peak_rss"] = kb("VmHWM")
    d["locked"] = kb("VmLck")
    d["data"] = kb("VmData")
    d["stack"] = kb("VmStk")
    d["page_tables"] = kb("VmPTE")
    rollup = parse_kv_ints(read_text(base + "/smaps_rollup"))
    if rollup:
        d["pss"] = rollup.get("Pss", 0) * 1024
        d["uss"] = (rollup.get("Private_Clean", 0) + rollup.get("Private_Dirty", 0)) * 1024
    else:
        d["pss"] = d["uss"] = None
    d["oom_score"] = read_int(base + "/oom_score")
    d["oom_score_adj"] = read_int(base + "/oom_score_adj")

    # Disk I/O
    io_text = read_text(base + "/io")
    d["io_accounting"] = io_accounting()
    if io_text is None:
        d["io"] = None
    else:
        io = parse_kv_ints(io_text)
        d["io"] = io
        if same and prev.get("io") and now > prev["_t"]:
            dt = now - prev["_t"]
            d["read_rate"] = max(0, io.get("read_bytes", 0) - prev["io"].get("read_bytes", 0)) / dt
            d["write_rate"] = max(0, io.get("write_bytes", 0) - prev["io"].get("write_bytes", 0)) / dt
        elif mon is not None and pid in mon.procs.procs:
            p = mon.procs.procs[pid]
            d["read_rate"], d["write_rate"] = p.read_rate, p.write_rate
        else:
            d["read_rate"] = d["write_rate"] = None

    # GPU
    p = mon.procs.procs.get(pid) if mon is not None else None
    d["gpu_pct"] = p.gpu_pct if p is not None else None
    d["gpu_mem"] = p.gpu_mem if p is not None else None

    # Open files and sockets
    fdnames = None
    try:
        fdnames = os.listdir(base + "/fd")
    except OSError:
        pass
    if fdnames is None:
        d["fds"] = None
    else:
        kinds = {"file": 0, "socket": 0, "pipe": 0, "device": 0, "other": 0}
        files, inodes = [], []
        for fd in sorted(fdnames, key=natural_key):
            target = readlink(base + "/fd/" + fd)
            if target is None:
                continue
            if target.startswith("socket:["):
                kinds["socket"] += 1
                try:
                    inodes.append(int(target[8:-1]))
                except ValueError:
                    pass
            elif target.startswith("pipe:["):
                kinds["pipe"] += 1
            elif target.startswith("/dev/"):
                kinds["device"] += 1
            elif target.startswith("/"):
                kinds["file"] += 1
                files.append((int(fd) if fd.isdigit() else -1, clean(target)))
            else:
                kinds["other"] += 1
        d["fds"] = {"count": len(fdnames), "kinds": kinds}
        d["files"] = files
        conns = []
        if inodes:
            table = read_sockets(pid)
            for inode in inodes:
                if inode in table:
                    conns.append(table[inode])
        d["connections"] = conns
    limits = {}
    for line in (read_text(base + "/limits", "") or "").splitlines()[1:]:
        m = re.match(r"^(Max [a-z ]+?)\s{2,}(\S+)\s+(\S+)\s*(\S*)", line)
        if m:
            vals = [fmt_bytes(int(v)) if m.group(4) == "bytes" and v.isdigit() else v for v in m.group(2, 3)]
            limits[m.group(1)] = tuple(vals)
    d["limits"] = limits

    # Threads, with CPU% when there is a previous sample
    threads = []
    prev_threads = prev.get("_threads", {}) if same else {}
    dt = (now - prev["_t"]) if same else None
    tick_map = {}
    for tid in listdir(base + "/task"):
        if not tid.isdigit():
            continue
        tst = parse_pid_stat(read_text("%s/task/%s/stat" % (base, tid)))
        if tst is None:
            continue
        t_ticks = tst["utime"] + tst["stime"]
        tick_map[int(tid)] = t_ticks
        pct = None
        if dt and int(tid) in prev_threads:
            pct = max(0.0, (t_ticks - prev_threads[int(tid)]) / float(CLK_TCK) / dt * 100.0)
        threads.append({"tid": int(tid), "name": clean(tst["comm"]), "state": tst["state"], "cpu_pct_core": pct,
                        "cpu_s": t_ticks / float(CLK_TCK), "last_cpu": tst["processor"]})
    threads.sort(key=lambda t: (t["cpu_pct_core"] or 0.0, t["cpu_s"]), reverse=True)
    d["thread_list"] = threads
    d["_threads"] = tick_map
    return d


def details_public(d):
    """The details dict without private bookkeeping, for JSON."""
    out = collections.OrderedDict((k, v) for k, v in d.items() if not k.startswith("_"))
    out["connections"] = [{"proto": c[0], "local": c[1], "remote": c[2], "state": c[3]} for c in d.get("connections", [])]
    out["files"] = [{"fd": fd, "path": path} for fd, path in d.get("files", [])]
    return out


def details_lines(d, width):
    """Lay out process details as lines of (text, style) segments."""
    lines = []
    lw = 24

    def section(title):
        if lines:
            lines.append([])
        lines.append([(title, "h")])

    def kv(label, value, style="v"):
        if value is None or value == "":
            return
        for i, chunk in enumerate(wrap_text(str(value), max(10, width - lw))):
            lines.append([(pad(label if i == 0 else "", lw), "k"), (chunk, style)])

    def note(text, style="k"):
        for chunk in wrap_text(text, max(10, width - 2)):
            lines.append([("  " + chunk, style)])

    section("General")
    kv("Name", d["name"] + ("" if d["name"] == d["comm"] else "  (kernel name: %s)" % d["comm"]))
    kv("PID", "%d%s" % (d["pid"], "  (%d inside its namespace)" % d["namespace_pid"] if d["namespace_pid"] else ""))
    kv("Parent", "%d (%s)" % (d["ppid"], d["parent_name"]) if d["parent_name"] else str(d["ppid"]))
    if d["ancestry"]:
        kv("Started by", (" %s " % G["arrow"]).join("%s(%d)" % (n, p) for p, n in d["ancestry"]))
    user = "%s (uid %d)" % (d["user"], d["uid"])
    if d["euid"] != d["uid"]:
        user += ", effective %s (uid %d)" % (d["euser"], d["euid"])
    kv("User", user)
    kv("Group", "%s (gid %d)" % (d["group"], d["gid"]))
    kv("State", "%s (%s)" % (d["state"], d["state_name"]), "warn" if d["state"] in ("D", "Z") else "v")
    if d["started"]:
        kv("Started", "%s  (%s)" % (fmt_time(d["started"]), fmt_ago(d["elapsed_s"])))
    kv("Threads", d["threads"])
    kv("Priority", "nice %d, priority %d, %s%s" % (d["nice"], d["priority"], d["policy"],
                                                   " %d" % d["rt_priority"] if d["rt_priority"] else ""))
    kv("CPU affinity", d["affinity"])
    kv("Last ran on CPU", d["last_cpu"] if d["last_cpu"] >= 0 else None)
    kv("Terminal", d["tty"])
    kv("Session / group", "%d / %d" % (d["session"], d["pgrp"]))
    if d["user_unit"]:
        kv("Service", "%s (user unit%s)" % (d["user_unit"], ", in " + d["unit"] if d["unit"] else ""))
    else:
        kv("Service", d["unit"])
    kv("Container", d["container"])
    kv("Control group", d["cgroup"])
    kv("Seccomp", d["seccomp"])
    kv("Executable", d["exe"] or ("(kernel thread)" if d["kernel_thread"] else "(not readable: other user)"))
    kv("Working directory", d["cwd"])
    kv("Command line", d["cmdline"])

    section("CPU")
    if d["cpu_pct"] is not None:
        kv("Usage", "%.1f%% of the machine  (%.1f%% of one core)" % (d["cpu_pct"], d["cpu_pct_core"]),
           "crit" if d["cpu_pct_core"] >= 90 else "v")
    kv("CPU time", "%s user + %s system = %s" % (fmt_cputime(d["cpu_user_s"]), fmt_cputime(d["cpu_system_s"]),
                                                fmt_cputime(d["cpu_user_s"] + d["cpu_system_s"])))
    if d["cpu_children_s"]:
        kv("Waited-for children", fmt_cputime(d["cpu_children_s"]))
    kv("Context switches", "%d voluntary, %d involuntary" % (d["ctx_voluntary"], d["ctx_involuntary"]))
    kv("Page faults", "%d minor, %d major (from disk)" % (d["faults_minor"], d["faults_major"]))
    if d["io_delay_s"]:
        kv("Waited for disk", fmt_cputime(d["io_delay_s"]))

    section("Memory")
    if d["uss"] is not None:
        kv("Private (USS)", fmt_bytes(d["uss"]))
        kv("Proportional (PSS)", fmt_bytes(d["pss"]))
    if d["rss"] is not None:
        kv("Resident (RSS)", "%s  = %s anonymous + %s file-backed + %s shared memory" % (
            fmt_bytes(d["rss"]), fmt_bytes(d["rss_anon"]), fmt_bytes(d["rss_file"]), fmt_bytes(d["rss_shmem"])))
    kv("Swapped out", fmt_bytes(d["swap"]) if d["swap"] is not None else None)
    kv("Peak resident", fmt_bytes(d["peak_rss"]) if d["peak_rss"] else None)
    kv("Virtual size", fmt_bytes(d["vsize"]))
    if d["data"] is not None:
        kv("Heap+data / stack", "%s / %s" % (fmt_bytes(d["data"]), fmt_bytes(d["stack"])))
    if d["locked"]:
        kv("Locked", fmt_bytes(d["locked"]))
    kv("Page tables", fmt_bytes(d["page_tables"]) if d["page_tables"] is not None else None)
    if d["oom_score"] is not None:
        kv("OOM score", "%d (adjust %s)  - higher is killed first when memory runs out" % (d["oom_score"], d["oom_score_adj"]))

    section("Disk I/O")
    io = d["io"]
    if io is None and not d["io_accounting"]:
        note("This kernel does not count I/O per process (built without CONFIG_TASK_IO_ACCOUNTING).")
    elif io is None:
        note("Not readable for another user's process. Run ltm as root to see it.")
    else:
        if d.get("read_rate") is not None:
            kv("Read / write now", "%s / %s" % (fmt_rate(d["read_rate"]), fmt_rate(d["write_rate"])))
        kv("Read from storage", fmt_bytes(io.get("read_bytes", 0)))
        kv("Written to storage", fmt_bytes(io.get("write_bytes", 0)))
        kv("All reads / writes", "%s / %s  (including page cache, pipes, sockets)" % (
            fmt_bytes(io.get("rchar", 0)), fmt_bytes(io.get("wchar", 0))))
        kv("Read / write calls", "%d / %d" % (io.get("syscr", 0), io.get("syscw", 0)))

    if d["gpu_pct"] is not None or d["gpu_mem"]:
        section("GPU")
        kv("Usage", fmt_pct(d["gpu_pct"]) if d["gpu_pct"] is not None else None)
        kv("Video memory", fmt_bytes(d["gpu_mem"]) if d["gpu_mem"] else None)

    section("Files and network")
    if d["fds"] is None:
        note("Open files are not readable for another user's process (run as root).")
    else:
        k = d["fds"]["kinds"]
        limit = d["limits"].get("Max open files", ("?", "?"))[0]
        kv("Open descriptors", "%d of %s allowed: %d files, %d sockets, %d pipes, %d devices, %d other" % (
            d["fds"]["count"], limit, k["file"], k["socket"], k["pipe"], k["device"], k["other"]))
        conns = d["connections"]
        if conns:
            lines.append([(pad("Connections", lw), "k"), ("%d" % len(conns), "v")])
            for proto, local, remote, state in sorted(conns, key=lambda c: (c[3] != "LISTEN", c[0], c[1]))[:60]:
                rem = "" if remote.endswith(("0.0.0.0:0", "[::]:0")) else "  %s %s" % (G["arrow"], remote)
                lines.append([("  %-5s %s%s" % (proto, local, rem), "v"), ("  " + state, "good" if state == "ESTABLISHED" else "k")])
            if len(conns) > 60:
                note("... %d more" % (len(conns) - 60))
        if d["files"]:
            lines.append([(pad("Open files", lw), "k"), ("%d" % len(d["files"]), "v")])
            for fd, path in d["files"][:40]:
                lines.append([("  %4d  " % fd, "k"), (path, "v")])
            if len(d["files"]) > 40:
                note("... %d more" % (len(d["files"]) - 40))

    threads = d["thread_list"]
    if threads:
        section("Threads (%d, busiest first)" % len(threads))
        lines.append([("  %8s  %-16s %s %7s %10s %4s" % ("TID", "NAME", "S", "CPU%", "TIME", "CPU"), "k")])
        for t in threads[:30]:
            pct = "%6.1f%%" % t["cpu_pct_core"] if t["cpu_pct_core"] is not None else "      -"
            lines.append([("  %8d  %-16s %s %s %10s %4s" % (t["tid"], clip(t["name"], 16), t["state"], pct,
                                                          fmt_cputime(t["cpu_s"]), t["last_cpu"]), "v")])
        if len(threads) > 30:
            note("... %d more threads" % (len(threads) - 30))

    if d["limits"]:
        section("Limits (soft / hard)")
        for name in ("Max open files", "Max processes", "Max address space", "Max resident set", "Max locked memory",
                     "Max core file size", "Max stack size", "Max cpu time"):
            if name in d["limits"]:
                kv(name[4:].capitalize(), "%s / %s" % d["limits"][name])
    return lines

# ---------------------------------------------------------------------------
# Command-line output (no curses)
# ---------------------------------------------------------------------------


class Style(object):
    def __init__(self, enabled):
        self.enabled = enabled

    def __call__(self, text, code):
        return "\033[%sm%s\033[0m" % (code, text) if self.enabled and text else text

    def bold(self, text):
        return self(text, "1")

    def head(self, text):
        return self(text, "1;36")

    def dim(self, text):
        return self(text, "2")


def want_color(args):
    return (sys.stdout.isatty() and not getattr(args, "no_color", False) and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb")


def terminal_width():
    return shutil.get_terminal_size((120, 40)).columns if sys.stdout.isatty() else None


def format_table(headers, rows, aligns, width=None, flex=None):
    widths = [text_width(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], text_width(c))
    if width and flex is not None:
        fixed = sum(w for i, w in enumerate(widths) if i != flex) + 2 * (len(widths) - 1)
        widths[flex] = max(text_width(headers[flex]), min(widths[flex], width - fixed))

    def line(cells):
        return "  ".join(pad(c, widths[i], aligns[i]) for i, c in enumerate(cells)).rstrip()
    return [line(headers)] + [line(r) for r in rows]


def proc_dict(p, ncpu):
    return collections.OrderedDict([
        ("pid", p.pid), ("ppid", p.ppid), ("name", p.name), ("user", p.user), ("uid", p.uid), ("state", p.state),
        ("cpu_pct", round(p.cpu_pct / ncpu, 2)), ("cpu_pct_core", round(p.cpu_pct, 2)),
        ("memory_private", p.private), ("memory_rss", p.rss), ("memory_virtual", p.vsize),
        ("disk_read_bps", p.read_rate), ("disk_write_bps", p.write_rate),
        ("gpu_pct", p.gpu_pct), ("gpu_memory", p.gpu_mem), ("threads", p.threads), ("nice", p.nice),
        ("cpu_time_s", round(p.cpu_ticks / float(CLK_TCK), 2)), ("start_time", p.start_epoch),
        ("tty", p.tty), ("service", p.unit or p.user_unit), ("container", p.container), ("kernel_thread", p.kernel),
        ("command", p.cmd)])


CLI_SORTS = {
    "cpu": (lambda p: p.cpu_pct, True), "mem": (lambda p: p.private, True), "rss": (lambda p: p.rss, True),
    "disk": (lambda p: p.io_rate if p.io_rate is not None else -1, True),
    "gpu": (lambda p: p.gpu_pct if p.gpu_pct is not None else -1, True),
    "pid": (lambda p: p.pid, False), "name": (lambda p: p.name.lower(), False), "user": (lambda p: p.user, False),
    "time": (lambda p: p.cpu_ticks, True), "threads": (lambda p: p.threads, True),
}


def select_procs(mon, args):
    procs = list(mon.procs.procs.values())
    if not getattr(args, "kernel", False):
        procs = [p for p in procs if not p.kernel]
    match = make_matcher(" ".join(x for x in (getattr(args, "filter", None),
                                              "user:" + args.user if getattr(args, "user", None) else None) if x))
    me = os.getpid()   # like pgrep: a filter should not find ltm's own command line
    return [p for p in procs if match(p) and p.pid != me] if match else procs


def proc_table_lines(mon, procs, prefixes=None, width=None, use_rss=False):
    ncpu = mon.procs.ncpu
    show_gpu = any(p.gpu_pct is not None or p.gpu_mem for p in procs)
    headers = ["PID", "USER", "NAME", "S", "CPU%", "RSS" if use_rss else "MEMORY", "DISK/s"] + \
              (["GPU%"] if show_gpu else []) + ["THR", "TIME+", "COMMAND"]
    aligns = ["r", "l", "l", "l", "r", "r", "r"] + (["r"] if show_gpu else []) + ["r", "r", "l"]
    rows = []
    for i, p in enumerate(procs):
        row = [str(p.pid), p.user, (prefixes[i] if prefixes else "") + p.name, p.state, "%.1f" % (p.cpu_pct / ncpu),
               fmt_bytes(p.rss if use_rss else p.private, True), fmt_rate(p.io_rate, True)]
        if show_gpu:
            row.append("%.0f" % p.gpu_pct if p.gpu_pct is not None else "-")
        row += [str(p.threads), fmt_cputime(p.cpu_ticks / float(CLK_TCK)), p.cmd or "[%s]" % p.comm]
        rows.append(row)
    return format_table(headers, rows, aligns, width, flex=len(headers) - 1)


def cmd_top(args):
    mon = Monitor()
    mon.sample(args.interval or 1.0)
    procs = select_procs(mon, args)
    keyfn, rev = CLI_SORTS[args.sort]
    if args.reverse:
        rev = not rev
    limit = args.limit
    if limit is None:
        limit = max(10, shutil.get_terminal_size((120, 40)).lines - 4) if sys.stdout.isatty() else 0
    prefixes = None
    if args.tree:
        entries = build_tree(procs, keyfn, rev)
        procs = [e[0] for e in entries]
        prefixes = [e[1] for e in entries]
    else:
        procs.sort(key=lambda p: p.pid)
        procs.sort(key=keyfn, reverse=rev)
    if limit:
        procs = procs[:limit]
        if prefixes:
            prefixes = prefixes[:limit]
    if args.json:
        print(json.dumps([proc_dict(p, mon.procs.ncpu) for p in procs], indent=2))
        return 0
    st = Style(want_color(args))
    width = None if args.wide else terminal_width()
    lines = proc_table_lines(mon, procs, prefixes, width, args.sort == "rss")
    print(st.bold(lines[0]))
    for line in lines[1:]:
        print(line)
    if not procs:
        pass
    elif not io_accounting():
        print(st.dim("DISK/s: this kernel does not count I/O per process (no CONFIG_TASK_IO_ACCOUNTING)."))
    elif not mon.is_root and any(p.io_denied for p in procs):
        print(st.dim("DISK/s of other users' processes needs root (sudo ltm top)."))
    return 0


def cmd_summary(args):
    mon = Monitor(getattr(args, "all_disks", False), getattr(args, "all_nics", False))
    mon.sample(getattr(args, "interval", None) or 1.0)
    fs = mon.fs_task.wait(3) or []
    snap = mon.snapshot(fs)
    if getattr(args, "json", False):
        print(json.dumps(snap, indent=2, default=str))
        return 0
    st = Style(want_color(args))
    out = []
    c, s, m = mon.cpu, mon.cpu.static, mon.mem.info
    host = mon.static
    head = "%s %s %s %s kernel %s %s %s" % (host["hostname"], G["dash"], host["os"], G["dash"], host["kernel"], G["dash"], host["arch"])
    if host["virt"]:
        head += " %s %s" % (G["dash"], host["virt"])
    out.append(st.bold(head) + st.dim("   up " + fmt_duration(mon.uptime)))
    if host["machine"]:
        out.append(st.dim(host["machine"]))
    out.append("")
    label_w = 12

    def row(label, text):
        out.append(st.head(pad(label, label_w)) + text)

    def more(text):
        out.append(" " * label_w + text)
    topo = []
    if s["sockets"] and s["sockets"] > 1:
        topo.append("%d sockets" % s["sockets"])
    if s["cores"]:
        topo.append("%d cores" % s["cores"])
    topo.append("%d logical" % s["logical"])
    row("CPU", "%s %s %s, %s" % (s["model"], G["dash"], ", ".join(topo), fmt_freq(c.freq_khz)))
    b = c.breakdown
    more("%s used  (user %.1f%%, system %.1f%%, I/O wait %.1f%%, steal %.1f%%)%s" % (
        st.bold("%.1f%%" % c.usage), b["user"], b["system"], b["iowait"], b["steal"],
        "   %s" % fmt_temp(mon.sensors.cpu_temp) if mon.sensors.cpu_temp is not None else ""))
    more("load %.2f %.2f %.2f  %s  %d processes, %d threads, %d running" % (
        c.loadavg[0], c.loadavg[1], c.loadavg[2], G["dash"], len(mon.procs.procs), mon.procs.total_threads, c.procs_running))
    if c.per_cpu:
        cells = ["%d:%3.0f%%" % (i, c.per_cpu[i]) for i in sorted(c.per_cpu)]
        per_line = max(1, ((terminal_width() or 100) - label_w) // 9)
        for i in range(0, len(cells), per_line):
            more(st.dim("  ".join(cells[i:i + per_line])))
    row("Memory", "%s of %s in use (%s)  %s  %s available" % (
        st.bold(fmt_bytes(m["used"])), fmt_bytes(m["total"]), fmt_pct(m["used_pct"]), G["dash"], fmt_bytes(m["available"])))
    more("cached %s, buffers %s, shared %s, committed %s of %s" % (
        fmt_bytes(m["cached"]), fmt_bytes(m["buffers"]), fmt_bytes(m["shared"]), fmt_bytes(m["committed"]),
        fmt_bytes(m["commit_limit"])))
    if m["swap_total"]:
        row("Swap", "%s of %s (%s)" % (fmt_bytes(m["swap_used"]), fmt_bytes(m["swap_total"]), fmt_pct(m["swap_pct"])))
    for n, dsk in mon.disks.devices.items():
        desc = ", ".join(x for x in (dsk["model"], dsk["kind"], fmt_bytes(dsk["size"])) if x)
        row("Disk " + n, "%s  %s  active %s, read %s, write %s, response %.1f ms" % (
            desc, G["dash"], fmt_pct(dsk["active"]), fmt_rate(dsk["read_bps"]), fmt_rate(dsk["write_bps"]), dsk["response_ms"]))
    mount_w = min(36, max([text_width(f["mount"]) for f in fs] + [8]))
    for f in fs:
        row("Filesystem", "%s  %-6s %s of %s (%s)%s" % (pad(f["mount"], mount_w), f["fstype"], fmt_bytes(f["used"]),
                                                      fmt_bytes(f["total"]), fmt_pct(f["pct"], 0),
                                                      "  read-only" if f["readonly"] else ""))
    for n, i in mon.net.ifaces.items():
        bits = [i["type"], i["state"]]
        if i["speed_mbps"]:
            bits.append("%d Mbps link" % i["speed_mbps"])
        addr = "  " + i["ipv4"] if i["ipv4"] else ""
        row("Net " + n, "%s  %s  %s %s  %s %s%s" % (", ".join(bits), G["dash"], G["rx"], fmt_bits(i["rx_bps"]),
                                                  G["tx"], fmt_bits(i["tx_bps"]), addr))
    for k, g in enumerate(mon.gpu.gpus):
        parts = [g["name"]]
        if g["util"] is not None:
            parts.append("%s busy" % fmt_pct(g["util"], 0))
        if g["vram_total"]:
            parts.append("VRAM %s of %s" % (fmt_bytes(g["vram_used"]), fmt_bytes(g["vram_total"])))
        if g["temp"] is not None:
            parts.append(fmt_temp(g["temp"]))
        if g["power"]:
            parts.append("%.0f W" % g["power"])
        row("GPU %d" % k, (" %s " % G["dash"]).join(parts))
    temps = [x for x in mon.sensors.sensors if x["kind"] == "temp" and x.get("value") is not None]
    if temps:
        cells = ["%s %s %s" % (t["chip"], t["label"], fmt_temp(t["value"])) for t in temps[:12]]
        row("Sensors", ", ".join(cells) + (" ..." if len(temps) > 12 else ""))
    for bat in mon.power.batteries:
        extra = ""
        if bat["hours_left"] is not None:
            extra = ", %s %s" % (fmt_hours(bat["hours_left"]), "to full" if bat["status"] == "Charging" else "left")
        if bat["health_pct"]:
            extra += ", health %.0f%%" % bat["health_pct"]
        row("Battery", "%s %s%% %s%s" % (bat["name"], bat["capacity"], bat["status"].lower(), extra))
    procs = select_procs(mon, args)
    for title, key in (("Top CPU", "cpu"), ("Top memory", "mem")):
        out.append("")
        out.append(st.head(title))
        fn, rev = CLI_SORTS[key]
        top = sorted(procs, key=fn, reverse=rev)[:5]
        lines = proc_table_lines(mon, top, width=terminal_width())
        out.append(st.bold(lines[0]))
        out.extend(lines[1:])
    print("\n".join(out))
    return 0


def resolve_pid(mon, target):
    if target.isdigit():
        return int(target), None
    t = target.lower()
    matches = [p for p in mon.procs.procs.values() if t == p.name.lower()] or \
              [p for p in mon.procs.procs.values() if t in p.name.lower() or t in p.cmd.lower()]
    matches = [p for p in matches if p.pid != os.getpid()]
    if len(matches) == 1:
        return matches[0].pid, None
    return None, sorted(matches, key=lambda p: -p.cpu_pct)


def cmd_proc(args):
    mon = Monitor()
    mon.update()
    pid, many = resolve_pid(mon, args.target)
    if pid is None:
        if not many:
            sys.stderr.write("ltm: no process matches %r\n" % args.target)
            return 1
        sys.stderr.write("ltm: %d processes match %r; pass a PID:\n" % (len(many), args.target))
        for line in proc_table_lines(mon, many[:30], width=terminal_width()):
            sys.stderr.write(line + "\n")
        return 2
    first = process_details(pid, mon=mon)
    if first is None:
        sys.stderr.write("ltm: no process with PID %d\n" % pid)
        return 1
    time.sleep(max(0.1, args.interval or 1.0))
    mon.update()
    d = process_details(pid, first, mon) or first
    if args.json:
        print(json.dumps(details_public(d), indent=2, default=str))
        return 0
    st = Style(want_color(args))
    width = terminal_width() or 120
    for segs in details_lines(d, width - 2):
        text = ""
        for chunk, style in segs:
            text += st.head(chunk) if style == "h" else st.dim(chunk) if style == "k" else chunk
        print(text.rstrip())
    return 0


def cmd_users(args):
    mon = Monitor()
    mon.sample(args.interval or 1.0)
    rows = sorted(user_rows(mon.procs.procs), key=lambda u: -u["cpu"])
    ncpu = mon.procs.ncpu
    if args.json:
        for u in rows:
            u["cpu"] = round(u["cpu"] / ncpu, 2)
        print(json.dumps(rows, indent=2))
        return 0
    lines = format_table(["USER", "UID", "PROCS", "THREADS", "CPU%", "MEMORY", "RSS", "DISK/s"],
                         [[u["user"], str(u["uid"]), str(u["procs"]), str(u["threads"]), "%.1f" % (u["cpu"] / ncpu),
                           fmt_bytes(u["private"], True), fmt_bytes(u["rss"], True),
                           fmt_rate((u["read_rate"] or 0) + (u["write_rate"] or 0), True) if u["read_rate"] is not None else "-"]
                          for u in rows], ["l", "r", "r", "r", "r", "r", "r", "r"])
    st = Style(want_color(args))
    print(st.bold(lines[0]))
    print("\n".join(lines[1:]))
    return 0


def cmd_services(args):
    if not (os.path.isdir("/run/systemd/system") and shutil.which("systemctl")):
        sys.stderr.write("ltm: services need systemd, which is not running here.\n")
        return 1
    mon = Monitor()
    mon.sample(args.interval or 1.0)
    rows = service_rows(query_units(), mon.procs.procs, show_inactive=args.all)
    rows.sort(key=lambda r: (r["sub"] != "running", -r["cpu"], -r["private"], r["unit"]))
    ncpu = mon.procs.ncpu
    if args.json:
        for r in rows:
            r["cpu"] = round(r["cpu"] / ncpu, 2)
        print(json.dumps(rows, indent=2))
        return 0
    lines = format_table(["UNIT", "STATE", "PIDS", "CPU%", "MEMORY", "DISK/s", "DESCRIPTION"],
                         [[r["unit"], "%s/%s" % (r["active"], r["sub"]), str(r["pids"]), "%.1f" % (r["cpu"] / ncpu),
                           fmt_bytes(r["private"], True), fmt_rate(r["io_rate"], True), r["description"]] for r in rows],
                         ["l", "l", "r", "r", "r", "r", "l"], terminal_width(), flex=6)
    st = Style(want_color(args))
    print(st.bold(lines[0]))
    print("\n".join(lines[1:]))
    return 0


def cmd_startup(args):
    descriptions = {}
    if shutil.which("systemctl") and os.path.isdir("/run/systemd/system"):
        descriptions = dict((u["unit"], u["description"]) for u in query_units())
    rows = startup_entries(descriptions)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    lines = format_table(["NAME", "TYPE", "STATUS", "COMMAND / DESCRIPTION"],
                         [[r["name"], r["type"], "enabled" if r["enabled"] else "disabled",
                           (r["command"] + ("  (%s)" % r["note"] if r["note"] else "")).strip()] for r in rows],
                         ["l", "l", "l", "l"], terminal_width(), flex=3)
    st = Style(want_color(args))
    print(st.bold(lines[0]))
    print("\n".join(lines[1:]))
    return 0

# ---------------------------------------------------------------------------
# Interactive interface (curses)
# ---------------------------------------------------------------------------


HELP_TEXT = u"""\
ltm %s - task manager for the Linux terminal

Everywhere
  1-5, Tab, Shift-Tab   switch tab: Processes, Performance, Users, Services, Startup
  F1, ?, h              this help
  p                     pause / resume updates
  + / -                 slower / faster updates
  F5                    update now
  q, F10, Ctrl-C        quit
  mouse                 click tabs, column headers (sort) and rows; the wheel scrolls

Processes
  Up/Down PgUp/PgDn Home/End    move
  Enter, double-click   live details of the process: memory breakdown, open files,
                        network connections, threads, limits ...
  /                     filter by name, command, user, PID, service or container;
                        "user:root" keeps one user's processes. Esc clears it.
  c  m  d  g            sort by CPU, memory, disk, GPU
  <  >   r              previous / next sort column, reverse the order
  t                     tree view (who started whom)
  a                     group by application: same-named processes added together
  Space, Left, Right    collapse / expand a tree branch or an application group
  x, Delete             end task (SIGTERM; asks first). On a group: every process in it
  k                     send another signal (SIGKILL, SIGHUP, SIGSTOP, SIGCONT ...)
  n                     change priority (nice: -20 highest .. 19 lowest)
  K                     show kernel threads
  u                     only my processes
  M                     memory column: private (default) or resident (RSS)
  %%                     CPU%%: share of the whole machine (default) or of one core

Performance
  Up/Down               choose CPU, memory, a disk, a network adapter, a GPU,
                        sensors or a battery
  l                     CPU: one graph per logical processor

Users
  Enter                 show that user's processes

Services (systemd)
  Enter                 systemctl status
  S  T  R               start / stop / restart (asks first; needs root or polkit)
  A                     show or hide inactive services
  /                     filter

Startup
  Enter                 show the unit file or the .desktop file

What the columns mean
  CPU%%     share of all CPUs (8 cores, one busy = 12.5%%). Press %% for per-core.
  MEMORY   private memory: resident pages not shared with other processes
           (RSS minus file-backed and shared pages). Press M for RSS. The details
           window also shows PSS and USS.
  DISK/s   bytes read from and written to storage per second (/proc/PID/io).
           Other users' processes show "-" unless ltm runs as root.
  GPU%%     the busiest engine of the GPU the process uses (from DRM fdinfo:
           Intel, AMD and other open drivers). NVIDIA via nvidia-smi.
  S        state: R running, S sleeping, D waiting for disk, Z zombie,
           T stopped, I idle kernel thread
  TIME+    CPU time used since the process started
"""


class Column(object):
    def __init__(self, key, title, width, get, align="l", attr=None, prio=0, flex=False, sortable=True):
        self.key = key
        self.title = title
        self.width = width
        self.get = get
        self.align = align
        self.attr = attr
        self.prio = prio
        self.flex = flex
        self.sortable = sortable


class TableView(object):
    """A scrolling, sortable table that keeps the selected row across refreshes."""

    def __init__(self, columns, row_id, sort_key=None, sort_rev=True, sorts=None):
        self.columns = columns
        self.row_id = row_id
        self.sort_key = sort_key
        self.sort_rev = sort_rev
        self.sorts = sorts or {}
        self.rows = []
        self.sel = 0
        self.top = 0
        self.sel_id = None
        self.want = []          # rows to select next time, in order of preference
        self.height = 10
        self.on_activate = None
        self.empty_text = "Nothing to show."
        self.visible = []

    def cols(self):
        return self.columns() if callable(self.columns) else self.columns

    def set_rows(self, rows, presorted=False):
        if not presorted and self.sort_key in self.sorts:
            rows.sort(key=self.sorts[self.sort_key], reverse=self.sort_rev)
        self.rows = rows
        wanted = [w for w in self.want + [self.sel_id] if w is not None]
        self.want = []
        if wanted:
            ids = [self.row_id(r) for r in rows]
            for w in wanted:
                if w in ids:
                    self.sel = ids.index(w)
                    break
        self.sel = max(0, min(self.sel, len(rows) - 1))
        self.sel_id = self.row_id(rows[self.sel]) if rows else None

    def selected(self):
        return self.rows[self.sel] if self.rows else None

    def select(self, idx):
        if self.rows:
            self.sel = max(0, min(idx, len(self.rows) - 1))
            self.sel_id = self.row_id(self.rows[self.sel])

    def nav(self, ch):
        page = max(1, self.height - 1)
        if ch == curses.KEY_UP:
            self.select(self.sel - 1)
        elif ch == curses.KEY_DOWN:
            self.select(self.sel + 1)
        elif ch == curses.KEY_PPAGE:
            self.select(self.sel - page)
        elif ch == curses.KEY_NPAGE:
            self.select(self.sel + page)
        elif ch == curses.KEY_HOME:
            self.select(0)
        elif ch == curses.KEY_END:
            self.select(len(self.rows) - 1)
        else:
            return False
        return True

    def set_sort(self, key):
        if self.sort_key == key:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_key = key
            self.sort_rev = key not in TEXT_SORT_KEYS

    def cycle_sort(self, step):
        keys = [c.key for c, _ in self.visible if c.sortable] or [c.key for c in self.cols() if c.sortable]
        if not keys:
            return
        i = keys.index(self.sort_key) if self.sort_key in keys else 0
        self.sort_key = keys[(i + step) % len(keys)]
        self.sort_rev = self.sort_key not in TEXT_SORT_KEYS

    def layout(self, width):
        cols = list(self.cols())

        def need(cs):
            return sum(c.width for c in cs) + max(0, len(cs) - 1)
        while len(cols) > 1 and need(cols) > width:
            worst = max(cols, key=lambda c: c.prio)
            if worst.prio <= 0:
                break
            cols.remove(worst)
        extra = width - need(cols)
        flex = [c for c in cols if c.flex]
        target = flex[-1] if flex else None
        return [(c, c.width + (extra if c is target and extra > 0 else 0)) for c in cols]

    def draw(self, app, y, x, h, w):
        A = app.A
        self.visible = layout = self.layout(w)
        app.put(y, x, " " * w, A["header"])
        cx = x
        for col, cw in layout:
            title = col.title() if callable(col.title) else col.title
            attr = A["header"]
            if col.key == self.sort_key:
                title += G["down"] if self.sort_rev else G["up"]
                attr |= curses.A_BOLD
            app.put(y, cx, pad(title, cw, col.align), attr)
            if col.sortable:
                app.add_click(y, cx, cx + cw, (lambda k: lambda dbl: self.set_sort(k))(col.key))
            cx += cw + 1
        body = max(0, h - 1)
        self.height = body
        n = len(self.rows)
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + body:
            self.top = self.sel - body + 1
        self.top = max(0, min(self.top, max(0, n - body)))
        for i in range(body):
            idx = self.top + i
            if idx >= n:
                break
            r = self.rows[idx]
            yy = y + 1 + i
            selected = idx == self.sel
            if selected:
                app.put(yy, x, " " * w, A["sel"])
            cx = x
            for col, cw in layout:
                attr = A["sel"] if selected else (col.attr(r) if col.attr else 0)
                app.put(yy, cx, pad(col.get(r), cw, col.align), attr)
                cx += cw + 1
            app.add_click(yy, x, x + w, (lambda j: lambda dbl: self.click_row(j, dbl))(idx))
        if n == 0 and body > 1:
            app.put(y + 2, x + 2, self.empty_text, A["dim"])

    def click_row(self, idx, dbl):
        self.select(idx)
        if dbl and self.on_activate:
            self.on_activate()


class DRow(object):
    """A displayed row of the process table: a process or an application group."""
    __slots__ = ("kind", "p", "prefix", "group")

    def __init__(self, kind, p, prefix="", group=None):
        self.kind = kind
        self.p = p
        self.prefix = prefix
        self.group = group


class Popup(object):
    title = ""

    def box(self, app, h, w, y=None, x=None):
        h = max(3, min(h, app.h - 2))
        w = max(10, min(w, app.w))
        y = (app.h - h) // 2 if y is None else y
        x = (app.w - w) // 2 if x is None else x
        border = app.A["border"]
        for yy in range(y, y + h):
            app.put(yy, x, " " * w, 0)
        app.put(y, x, G["tl"] + G["h"] * (w - 2) + G["tr"], border)
        for yy in range(y + 1, y + h - 1):
            app.put(yy, x, G["v"], border)
            app.put(yy, x + w - 1, G["v"], border)
        app.put(y + h - 1, x, G["bl"] + G["h"] * (w - 2) + G["br"], border)
        if self.title:
            app.put(y, x + 2, " " + self.title + " ", border | curses.A_BOLD, w - 4)
        return y, x, h, w

    def draw(self, app):
        pass

    def key(self, app, ch):
        pass

    def wheel(self, app, delta):
        pass


class ConfirmPopup(Popup):
    def __init__(self, title, message, on_yes):
        self.title = title
        self.message = message
        self.on_yes = on_yes

    def draw(self, app):
        w = min(app.w - 4, max(46, text_width(self.message) + 6))
        lines = wrap_text(self.message, w - 4)
        y, x, h, w = self.box(app, len(lines) + 5, w)
        for i, line in enumerate(lines):
            app.put(y + 2 + i, x + 2, line, 0, w - 4)
        app.put(y + h - 2, x + 2, "[y] Yes      [n] No", curses.A_BOLD, w - 4)

    def key(self, app, ch):
        if ch in ("y", "Y"):
            app.popup = None
            self.on_yes()
        elif ch in ("n", "N", "q", "\x1b", "\n", "\r", curses.KEY_ENTER):
            app.popup = None


class MenuPopup(Popup):
    def __init__(self, title, items, on_select):
        self.title = title
        self.items = items
        self.on_select = on_select
        self.sel = 0

    def draw(self, app):
        w = max([text_width(l) for l, _ in self.items] + [text_width(self.title)]) + 10
        y, x, h, w = self.box(app, len(self.items) + 4, w)
        for i, (label, _) in enumerate(self.items):
            attr = app.A["sel"] if i == self.sel else 0
            app.put(y + 2 + i, x + 2, pad(" %d  %s" % (i + 1, label), w - 4), attr)

    def key(self, app, ch):
        if ch == curses.KEY_UP:
            self.sel = (self.sel - 1) % len(self.items)
        elif ch == curses.KEY_DOWN:
            self.sel = (self.sel + 1) % len(self.items)
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            app.popup = None
            self.on_select(self.items[self.sel][1])
        elif isinstance(ch, str) and ch.isdigit() and 1 <= int(ch) <= len(self.items):
            app.popup = None
            self.on_select(self.items[int(ch) - 1][1])
        elif ch in ("q", "\x1b"):
            app.popup = None


class InputPopup(Popup):
    def __init__(self, title, initial, on_submit, on_change=None, on_cancel=None, inline=False, hint=""):
        self.title = title
        self.text = initial
        self.on_submit = on_submit
        self.on_change = on_change
        self.on_cancel = on_cancel
        self.inline = inline
        self.hint = hint

    def draw(self, app):
        if self.inline:
            y = app.h - 1
            app.put(y, 0, " " * app.w, app.A["input"])
            n = app.put(y, 0, " %s: %s" % (self.title, self.text), app.A["input"], app.w - 1)
            app.put(y, n, "_", app.A["input"] | curses.A_BLINK)
            hint = "Enter keep   Esc cancel   Ctrl-U clear "
            if app.w - n > len(hint) + 4:
                app.put(y, app.w - len(hint) - 1, hint, app.A["input"])
            return
        y, x, h, w = self.box(app, 7, max(50, text_width(self.title) + 8))
        app.put(y + 2, x + 2, self.hint, app.A["dim"], w - 4)
        app.put(y + 3, x + 2, pad(self.text + "_", w - 4), app.A["input"])
        app.put(y + 5, x + 2, "Enter apply   Esc cancel", 0, w - 4)

    def key(self, app, ch):
        if ch in ("\n", "\r", curses.KEY_ENTER):
            app.popup = None
            self.on_submit(self.text)
            return
        if ch == "\x1b":
            app.popup = None
            if self.on_cancel:
                self.on_cancel()
            return
        if ch in (curses.KEY_BACKSPACE, "\x7f", "\b", "\x08"):
            self.text = self.text[:-1]
        elif ch == "\x15":
            self.text = ""
        elif isinstance(ch, str) and ch.isprintable():
            self.text += ch
        else:
            return
        if self.on_change:
            self.on_change(self.text)


class TextPopup(Popup):
    """A scrollable window of text lines; a line is a string or [(text, style)]."""

    def __init__(self, title, lines, hint="Up/Down/PgUp/PgDn scroll   Esc close"):
        self.title = title
        self.lines = lines
        self.hint = hint
        self.top = 0
        self.page = 10

    def get_lines(self, app, width):
        return self.lines

    def draw(self, app):
        y, x, h, w = self.box(app, app.h - 2, app.w, 1, 0)
        inner_w, inner_h = w - 4, h - 2
        lines = self.get_lines(app, inner_w)
        self.page = inner_h
        self.top = max(0, min(self.top, max(0, len(lines) - inner_h)))
        for i in range(inner_h):
            idx = self.top + i
            if idx >= len(lines):
                break
            app.put_segments(y + 1 + i, x + 2, lines[idx], inner_w)
        if len(lines) > inner_h:
            pos = " %d-%d of %d " % (self.top + 1, min(len(lines), self.top + inner_h), len(lines))
            app.put(y, x + w - len(pos) - 2, pos, app.A["border"])
        app.put(y + h - 1, x + 2, " " + self.hint + " ", app.A["border"], w - 4)

    def key(self, app, ch):
        if ch == curses.KEY_UP:
            self.top -= 1
        elif ch == curses.KEY_DOWN:
            self.top += 1
        elif ch == curses.KEY_PPAGE:
            self.top -= self.page - 1
        elif ch in (curses.KEY_NPAGE, " "):
            self.top += self.page - 1
        elif ch == curses.KEY_HOME:
            self.top = 0
        elif ch == curses.KEY_END:
            self.top = 1 << 30
        elif ch in ("q", "\x1b", "\n", "\r", curses.KEY_ENTER):
            app.popup = None
        self.top = max(0, self.top)

    def wheel(self, app, delta):
        self.top = max(0, self.top + delta)


class DetailPopup(TextPopup):
    def __init__(self, app, pid, starttime):
        TextPopup.__init__(self, "Process %d" % pid, [],
                           hint="Up/Down scroll   x End task   k Signal   n Nice   Esc close")
        self.pid = pid
        self.starttime = starttime
        self.tick = -1
        self.width = 0
        self.data = None
        self.gone = False

    def get_lines(self, app, width):
        if self.tick != app.mon.ticks:
            self.tick = app.mon.ticks
            d = process_details(self.pid, self.data, app.mon)
            if d is None or d["starttime"] != self.starttime:
                self.gone = True
            else:
                self.data = d
                self.title = "%s (PID %d)" % (d["name"], self.pid)
            self.width = 0
        if width != self.width:
            self.width = width
            if self.data is None:
                self.lines = ["Process %d has exited." % self.pid]
            else:
                self.lines = ([[("This process has exited. Last known state:", "crit")], []] if self.gone else []) + \
                    details_lines(self.data, width)
        return self.lines

    def key(self, app, ch):
        targets = [(self.pid, self.starttime)]
        if ch in ("x", curses.KEY_DC):
            app.end_task(targets)
        elif ch == "k":
            app.signal_menu(targets)
        elif ch == "n":
            app.renice_prompt(targets)
        else:
            TextPopup.key(self, app, ch)


def same_process(pid, starttime):
    st = parse_pid_stat(read_text("%s/%d/stat" % (PROC, pid)))
    return st is not None and st["starttime"] == starttime


class App(object):
    TABS = ("Processes", "Performance", "Users", "Services", "Startup")
    PROC_SORTS = {
        "pid": lambda p, a: p.pid, "user": lambda p, a: p.user.lower(), "name": lambda p, a: p.name.lower(),
        "state": lambda p, a: p.state, "cpu": lambda p, a: p.cpu_pct, "mem": lambda p, a: a.mem_of(p),
        "disk": lambda p, a: p.io_rate if p.io_rate is not None else -1.0,
        "gpu": lambda p, a: p.gpu_pct if p.gpu_pct is not None else -1.0,
        "threads": lambda p, a: p.threads, "nice": lambda p, a: p.nice, "time": lambda p, a: p.cpu_ticks,
        "cmd": lambda p, a: p.cmd.lower(),
    }

    def __init__(self, scr, mon, args):
        self.scr = scr
        self.mon = mon
        self.init_colors(not args.no_color and not os.environ.get("NO_COLOR"))
        self.interval = max(0.25, args.interval or 1.0)
        self.paused = False
        self.tab = self.TABS.index(args.tab.capitalize()) if args.tab else 0
        self.popup = None
        self.msg = ""
        self.msg_until = 0.0
        self.clicks = []
        self.h, self.w = scr.getmaxyx()
        self.filters = [""] * len(self.TABS)
        self.filters[0] = " ".join(x for x in (args.filter, "user:" + args.user if args.user else None) if x)
        self.tree = bool(args.tree)
        self.group = False
        self.show_kernel = False
        self.only_mine = False
        self.mem_mode = "private"
        self.cpu_norm = True
        self.expanded = set()
        self.collapsed = set()
        self.my_uid = os.getuid()
        self.ncpu = mon.procs.ncpu
        self.ptable = TableView(self.proc_columns, self.proc_row_id, args.sort or "cpu",
                                (args.sort or "cpu") not in TEXT_SORT_KEYS)
        self.ptable.on_activate = self.proc_activate
        self.ptable.empty_text = "No process matches the filter."
        self.perf_sel = 0
        self.perf_top = 0
        self.per_core = False
        self.utable = TableView(self.user_columns(), lambda r: r["uid"], "cpu", True, sorts={
            "user": lambda r: r["user"], "uid": lambda r: r["uid"], "procs": lambda r: r["procs"],
            "threads": lambda r: r["threads"], "cpu": lambda r: r["cpu"], "mem": lambda r: r["private"],
            "rss": lambda r: r["rss"], "disk": lambda r: (r["read_rate"] or 0) + (r["write_rate"] or 0),
            "gpu": lambda r: r["gpu"] or 0})
        self.utable.on_activate = self.user_activate
        self.show_inactive = True
        self.stable = TableView(self.service_columns(), lambda r: r["unit"], "mem", True, sorts={
            "unit": lambda r: r["unit"].lower(), "state": lambda r: (r["active"], r["sub"]), "pids": lambda r: r["pids"],
            "cpu": lambda r: r["cpu"], "mem": lambda r: r["private"],
            "disk": lambda r: r["io_rate"] if r["io_rate"] is not None else -1, "description": lambda r: r["description"].lower()})
        self.stable.on_activate = self.service_status
        self.sttable = TableView(self.startup_columns(), lambda r: (r["type"], r["path"]), "type", False, sorts={
            "name": lambda r: r["name"].lower(), "type": lambda r: (r["type"], r["name"].lower()),
            "status": lambda r: (not r["enabled"], r["name"].lower()), "command": lambda r: r["command"].lower()})
        self.sttable.on_activate = self.startup_show

    # --- drawing primitives ---------------------------------------------------

    def init_colors(self, use_color):
        A = {}
        bold, rev = curses.A_BOLD, curses.A_REVERSE
        dim = getattr(curses, "A_DIM", 0)
        A.update(bold=bold, dim=dim)
        colored = False
        if use_color and curses.has_colors():
            try:
                curses.start_color()
                try:
                    curses.use_default_colors()
                    bg = fg = -1
                except curses.error:
                    bg, fg = curses.COLOR_BLACK, curses.COLOR_WHITE
                K = curses
                spec = [("title", K.COLOR_BLACK, K.COLOR_CYAN, bold), ("tabbar", K.COLOR_WHITE, K.COLOR_BLUE, 0),
                        ("tab_active", K.COLOR_BLACK, K.COLOR_CYAN, bold), ("header", K.COLOR_BLACK, K.COLOR_GREEN, 0),
                        ("sel", K.COLOR_BLACK, K.COLOR_CYAN, 0), ("fkey", K.COLOR_BLACK, K.COLOR_CYAN, 0),
                        ("footer", fg, bg, 0), ("msg", K.COLOR_BLACK, K.COLOR_YELLOW, 0),
                        ("input", K.COLOR_BLACK, K.COLOR_YELLOW, 0), ("border", K.COLOR_CYAN, bg, 0),
                        ("cpu", K.COLOR_CYAN, bg, 0), ("mem", K.COLOR_MAGENTA, bg, 0), ("disk", K.COLOR_GREEN, bg, 0),
                        ("net", K.COLOR_YELLOW, bg, 0), ("gpu", K.COLOR_BLUE, bg, bold), ("sensor", K.COLOR_RED, bg, 0),
                        ("bat", K.COLOR_GREEN, bg, 0), ("good", K.COLOR_GREEN, bg, 0), ("warn", K.COLOR_YELLOW, bg, 0),
                        ("crit", K.COLOR_RED, bg, bold), ("head", K.COLOR_CYAN, bg, bold)]
                for i, (name, f, b, extra) in enumerate(spec, 1):
                    curses.init_pair(i, f, b)
                    A[name] = curses.color_pair(i) | extra
                colored = True
            except curses.error:
                colored = False
        if not colored:
            A.update(title=rev | bold, tabbar=rev, tab_active=bold, header=rev, sel=rev | bold, fkey=rev, footer=0,
                     msg=rev, input=rev, border=0, cpu=bold, mem=bold, disk=bold, net=bold, gpu=bold, sensor=bold,
                     bat=bold, good=0, warn=bold, crit=bold | rev, head=bold)
        self.A = A
        self.styles = {"h": A["head"], "k": A["dim"], "v": 0, "b": bold, "warn": A["warn"], "crit": A["crit"],
                       "good": A["good"], None: 0}

    def put(self, y, x, s, attr=0, w=None):
        if y < 0 or y >= self.h or x >= self.w or x < 0:
            return 0
        maxw = self.w - x if w is None else min(w, self.w - x)
        s = clip(s, maxw)
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass   # writing the bottom-right cell raises after drawing it
        return text_width(s)

    def put_right(self, y, right, s, attr=0):
        s = clip(s, max(0, right))
        self.put(y, right - text_width(s), s, attr)

    def put_segments(self, y, x, segs, maxw):
        if isinstance(segs, str):
            self.put(y, x, segs, 0, maxw)
            return
        used = 0
        for text, style in segs:
            if used >= maxw:
                break
            used += self.put(y, x + used, text, self.styles.get(style, 0), maxw - used)

    def add_click(self, y, x0, x1, fn):
        self.clicks.append((y, x0, x1, fn))

    def flash(self, text, seconds=4.0):
        self.msg = text
        self.msg_until = time.monotonic() + seconds

    def heat(self, core_pct):
        if core_pct >= 90:
            return self.A["crit"]
        if core_pct >= 40:
            return self.A["warn"]
        return 0

    def bar(self, y, x, w, frac, attr):
        frac = max(0.0, min(1.0, frac or 0.0))
        n = int(round(frac * w))
        self.put(y, x, G["full"] * n, attr)
        self.put(y, x + n, G["empty"] * (w - n), self.A["dim"])

    def graph(self, y, x, h, w, values, vmax, attr, top_left="", top_right="", bottom_left="", bottom_right=""):
        """Bordered area chart; the newest sample is on the right."""
        if h < 3 or w < 4:
            return
        b = self.A["border"] | self.A["dim"]
        self.put(y, x, G["tl"] + G["h"] * (w - 2) + G["tr"], b)
        for yy in range(y + 1, y + h - 1):
            self.put(yy, x, G["v"], b)
            self.put(yy, x + w - 1, G["v"], b)
        self.put(y + h - 1, x, G["bl"] + G["h"] * (w - 2) + G["br"], b)
        if top_left:
            self.put(y, x + 2, " %s " % top_left, self.A["dim"], w - 4)
        if top_right:
            self.put_right(y, x + w - 2, " %s " % top_right, self.A["dim"])
        if bottom_left:
            self.put(y + h - 1, x + 2, " %s " % bottom_left, self.A["dim"], w - 4)
        if bottom_right:
            self.put_right(y + h - 1, x + w - 2, " %s " % bottom_right, self.A["dim"])
        ih, iw = h - 2, w - 2
        vals = list(values)[-iw:]
        levels = []
        for v in vals:
            f = 0.0 if not vmax or v is None else max(0.0, min(1.0, v / float(vmax)))
            lv = int(round(f * ih * 8))
            levels.append(1 if lv == 0 and f > 0 else lv)
        blocks = G["blocks"]
        padn = iw - len(levels)
        for row in range(ih):
            below = (ih - 1 - row) * 8
            chars = [" "] * padn
            for lv in levels:
                k = lv - below
                chars.append(blocks[8] if k >= 8 else blocks[k] if k > 0 else " ")
            self.put(y + 1 + row, x + 1, "".join(chars), attr)

    def sparkline(self, y, x, w, values, vmax, attr, floor=0):
        vals = list(values)[-w:]
        if vmax is None:   # autoscale, but keep tiny noise (a few packets) looking tiny
            vmax = max(vals + [floor])
        blocks = G["blocks"]
        out = [" "] * (w - len(vals))
        for v in vals:
            lv = int(round(max(0.0, min(1.0, v / float(vmax))) * 8)) if vmax else 0
            out.append(blocks[max(1, lv)])
        self.put(y, x, "".join(out), attr)

    def kv_grid(self, y, x, w, pairs, max_rows, ncols=None):
        pairs = [(k, v) for k, v in pairs if v not in (None, "")]
        if not pairs or max_rows <= 0:
            return 0
        ncols = ncols or (3 if w >= 120 else 2 if w >= 64 else 1)
        rows = min(max_rows, (len(pairs) + ncols - 1) // ncols)
        colw = w // ncols
        lw = min(22, max(text_width(k) for k, _ in pairs) + 2)
        for i, (k, v) in enumerate(pairs):
            r, c = i % rows, i // rows
            if c >= ncols:
                break
            self.put(y + r, x + c * colw, k, self.A["dim"], lw)
            self.put(y + r, x + c * colw + lw, str(v), curses.A_BOLD, colw - lw - 1)
        return rows

    def span_label(self, samples):
        sec = samples * self.interval
        return "%d seconds" % sec if sec < 120 else "%.0f minutes" % (sec / 60.0)

    # --- main loop ------------------------------------------------------------

    def run(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(100)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(200)
        except curses.error:
            pass
        self.mon.update()
        next_at = time.monotonic() + min(self.interval, 0.5)
        dirty = True
        while True:
            now = time.monotonic()
            if not self.paused and now >= next_at:
                self.mon.update()
                next_at = now + self.interval
                dirty = True
            if self.msg and now > self.msg_until:
                self.msg = ""
                dirty = True
            if dirty:
                self.draw()
                dirty = False
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return
            dirty = True
            if ch == curses.KEY_RESIZE:
                try:
                    curses.update_lines_cols()
                except (AttributeError, curses.error):
                    pass
                continue
            if not self.on_key(ch):
                return

    def draw(self):
        self.h, self.w = self.scr.getmaxyx()
        self.clicks = []
        self.scr.erase()
        if self.h < 12 or self.w < 60:
            self.put(0, 0, "ltm needs at least 60x12 (now %dx%d). q quits." % (self.w, self.h))
            self.scr.refresh()
            return
        self.draw_title(0)
        self.draw_tabs(1)
        self.draw_summary(2)
        body_h = self.h - 4
        (self.draw_processes, self.draw_performance, self.draw_users, self.draw_services,
         self.draw_startup)[self.tab](3, body_h)
        self.draw_footer(self.h - 1)
        if self.popup is not None:
            self.popup.draw(self)
        self.scr.noutrefresh()
        curses.doupdate()

    def draw_title(self, y):
        st = self.mon.static
        left = " ltm  %s  %s  %s  kernel %s" % (st["hostname"], G["dash"], st["os"], st["kernel"])
        if st["virt"]:
            left += "  (%s)" % st["virt"]
        right = "up %s   %s " % (fmt_duration(self.mon.uptime), time.strftime("%H:%M:%S"))
        if self.paused:
            right = "PAUSED   " + right
        self.put(y, 0, " " * self.w, self.A["title"])
        self.put(y, 0, left, self.A["title"], self.w - text_width(right) - 2)
        self.put_right(y, self.w, right, self.A["title"])

    def draw_tabs(self, y):
        self.put(y, 0, " " * self.w, self.A["tabbar"])
        x = 0
        for i, name in enumerate(self.TABS):
            label = " %d %s " % (i + 1, name)
            n = self.put(y, x, label, self.A["tab_active"] if i == self.tab else self.A["tabbar"])
            self.add_click(y, x, x + n, (lambda j: lambda dbl: self.set_tab(j))(i))
            x += n + 1
        self.put_right(y, self.w, "update every %gs " % self.interval, self.A["tabbar"])

    def draw_summary(self, y):
        m, A = self.mon, self.A
        x = [1]
        bw = 10 if self.w >= 150 else 6 if self.w >= 124 else 0

        def seg(text, attr=0):
            x[0] += self.put(y, x[0], text, attr)

        def mini(frac, attr):
            if bw:
                self.bar(y, x[0], bw, frac, attr)
                x[0] += bw + 1
        cpu = m.cpu.usage
        seg("CPU ", A["bold"] | A["cpu"])
        mini(cpu / 100.0, A["cpu"])
        seg("%3.0f%%" % cpu, self.heat(cpu))
        seg("   ")
        mi = m.mem.info
        seg("Memory ", A["bold"] | A["mem"])
        mini(mi["used_pct"] / 100.0, A["mem"])
        seg("%s/%s" % (fmt_bytes(mi["used"], True), fmt_bytes(mi["total"], True)))
        if mi["swap_total"]:
            seg("  swap %.0f%%" % mi["swap_pct"], A["warn"] if mi["swap_pct"] > 50 else A["dim"])
        seg("   ")
        r, wr = m.disk_totals()
        seg("Disk ", A["bold"] | A["disk"])
        seg("R %s W %s" % (fmt_rate(r, True), fmt_rate(wr, True)))
        seg("   ")
        rx, tx = m.net_totals()
        seg("Net ", A["bold"] | A["net"])
        seg("%s%s %s%s" % (G["rx"], fmt_bits(rx), G["tx"], fmt_bits(tx)))
        utils = [g["util"] for g in m.gpu.gpus if g["util"] is not None]
        if utils:
            seg("   ")
            seg("GPU ", A["bold"] | A["gpu"])
            seg("%.0f%%" % max(utils))
        if m.sensors.cpu_temp is not None and self.w - x[0] > 12:
            seg("   ")
            seg(fmt_temp(m.sensors.cpu_temp), A["crit"] if m.sensors.cpu_temp >= 90 else A["warn"] if m.sensors.cpu_temp >= 75 else 0)

    def footer_keys(self):
        common_end = [("p", "Pause"), ("F1", "Help"), ("q", "Quit")]
        if self.tab == 0:
            return [("/", "Filter"), ("Enter", "Details"), ("x", "End task"), ("k", "Signal"), ("n", "Nice"),
                    ("t", "Tree"), ("a", "Apps"), ("c/m/d/g", "Sort"), ("K", "Kernel")] + common_end
        if self.tab == 1:
            return [("Up/Down", "Resource"), ("l", "Per-core graphs"), ("+/-", "Speed")] + common_end
        if self.tab == 2:
            return [("Enter", "Show processes"), ("/", "Filter")] + common_end
        if self.tab == 3:
            return [("/", "Filter"), ("Enter", "Status"), ("S", "Start"), ("T", "Stop"), ("R", "Restart"),
                    ("A", "Inactive")] + common_end
        return [("Enter", "Show"), ("F5", "Reload")] + common_end

    def draw_footer(self, y):
        self.put(y, 0, " " * self.w, self.A["footer"])
        if self.msg:
            self.put(y, 0, " " + self.msg + " ", self.A["msg"])
            return
        x = 0
        for k, label in self.footer_keys():
            if x + len(k) + len(label) + 2 > self.w:
                break
            x += self.put(y, x, k, self.A["fkey"])
            x += self.put(y, x, " " + label + " ", self.A["footer"])

    # --- input --------------------------------------------------------------

    def set_tab(self, i):
        self.tab = i % len(self.TABS)
        if self.tab == 3 and self.mon.units_task:
            self.mon.units_task.get()
        if self.tab == 4:
            self.mon.startup_task.get()

    def on_key(self, ch):
        if ch == curses.KEY_MOUSE:
            self.on_mouse()
            return True
        if self.popup is not None:
            self.popup.key(self, ch)
            return True
        if ch in ("q", "Q", curses.KEY_F10):
            return False
        if ch in ("?", "h", curses.KEY_F1):
            self.popup = TextPopup("Help", (HELP_TEXT % __version__).splitlines())
        elif ch == "\t":
            self.set_tab(self.tab + 1)
        elif ch == curses.KEY_BTAB:
            self.set_tab(self.tab - 1)
        elif isinstance(ch, str) and ch in "12345":
            self.set_tab(int(ch) - 1)
        elif ch == "p":
            self.paused = not self.paused
            self.flash("Paused: press p to resume" if self.paused else "Resumed")
        elif ch in ("+", "="):
            self.interval = min(10.0, self.interval + (0.25 if self.interval < 1 else 0.5 if self.interval < 3 else 1.0))
            self.flash("Updating every %gs" % self.interval)
        elif ch in ("-", "_"):
            self.interval = max(0.25, self.interval - (0.25 if self.interval <= 1 else 0.5 if self.interval <= 3 else 1.0))
            self.flash("Updating every %gs" % self.interval)
        elif ch == curses.KEY_F5:
            self.mon.update()
            for task in (self.mon.units_task, self.mon.startup_task, self.mon.fs_task):
                if task:
                    task.refresh()
        else:
            (self.key_processes, self.key_performance, self.key_users, self.key_services,
             self.key_startup)[self.tab](ch)
        return True

    def on_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        up = getattr(curses, "BUTTON4_PRESSED", 0x80000)
        down = getattr(curses, "BUTTON5_PRESSED", 0x200000)
        if bstate & (up | down):
            delta = -3 if bstate & up else 3
            if self.popup is not None:
                self.popup.wheel(self, delta)
            elif self.tab == 1:
                self.perf_sel = max(0, self.perf_sel + (1 if delta > 0 else -1))
            else:
                t = (self.ptable, None, self.utable, self.stable, self.sttable)[self.tab]
                t.select(t.sel + delta)
            return
        if self.popup is not None:
            return
        clicked = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED
        if bstate & clicked:
            dbl = bool(bstate & curses.BUTTON1_DOUBLE_CLICKED)
            for y, x0, x1, fn in reversed(self.clicks):
                if y == my and x0 <= mx < x1:
                    fn(dbl)
                    break

    def open_filter(self, tab):
        old = self.filters[tab]

        def change(s):
            self.filters[tab] = s

        def cancel():
            self.filters[tab] = old
        self.popup = InputPopup("Filter", old, change, change, cancel, inline=True)

    # --- Processes tab ----------------------------------------------------------

    def mem_of(self, p):
        return p.rss if self.mem_mode == "rss" else p.private

    def proc_row_id(self, r):
        return ("g", r.p.name) if r.kind == "group" else ("p", r.p.pid, r.p.starttime)

    def proc_columns(self):
        A = self.A
        total_mem = float(self.mon.mem.info["total"] or 1)
        ncpu = self.ncpu

        def cpu_text(r):
            v = r.p.cpu_pct / ncpu if self.cpu_norm else r.p.cpu_pct
            return "%.1f" % v

        def mem_attr(r):
            frac = self.mem_of(r.p) / total_mem
            return A["crit"] if frac >= 0.2 else A["warn"] if frac >= 0.05 else 0

        def name_text(r):
            if r.kind == "group":
                return "%s%s (%d)" % (r.prefix, r.p.name, r.p.count)
            return r.prefix + r.p.name

        cols = [
            Column("pid", "PID", 7, lambda r: str(r.p.pid) if r.kind == "proc" else "", "r"),
            Column("user", "USER", 10, lambda r: r.p.user, prio=3),
            Column("name", "NAME", 26, name_text, flex=True),
            Column("state", "S", 1, lambda r: r.p.state, prio=6),
            Column("cpu", lambda: "CPU%" if self.cpu_norm else "CORE%", 6, cpu_text, "r",
                   attr=lambda r: self.heat(r.p.cpu_pct)),
            Column("mem", lambda: "RSS" if self.mem_mode == "rss" else "MEMORY", 8,
                   lambda r: fmt_bytes(self.mem_of(r.p), True), "r", attr=mem_attr),
            Column("disk", "DISK/s", 8, lambda r: fmt_rate(r.p.io_rate, True) if r.p.io_rate else
                   ("0" if r.p.io_rate == 0 else "-"), "r", prio=2,
                   attr=lambda r: A["warn"] if (r.p.io_rate or 0) > 10 * 1048576 else 0),
        ]
        if self.mon.gpu.gpus or self.mon.gpu.cards:
            cols.append(Column("gpu", "GPU%", 5, lambda r: "%.0f" % r.p.gpu_pct if r.p.gpu_pct is not None else "-",
                               "r", prio=4))
        cols += [
            Column("threads", "THR", 4, lambda r: str(r.p.threads), "r", prio=5),
            Column("nice", "NI", 3, lambda r: str(r.p.nice) if r.kind == "proc" else "", "r", prio=7),
            Column("time", "TIME+", 9, lambda r: fmt_cputime(r.p.cpu_ticks / float(CLK_TCK)), "r", prio=5),
            Column("cmd", "COMMAND", 20, lambda r: r.p.cmd or "[%s]" % r.p.comm, prio=8, flex=True),
        ]
        return cols

    def visible_procs(self):
        procs = list(self.mon.procs.procs.values())
        if not self.show_kernel:
            procs = [p for p in procs if not p.kernel]
        if self.only_mine:
            procs = [p for p in procs if p.uid == self.my_uid]
        return procs

    def build_proc_rows(self):
        procs = self.visible_procs()
        match = make_matcher(self.filters[0])
        key = self.ptable.sort_key if self.ptable.sort_key in self.PROC_SORTS else "cpu"
        getter = self.PROC_SORTS[key]
        rev = self.ptable.sort_rev

        def sortfn(p):
            return getter(p, self)
        if self.tree:
            if match:
                by_pid = dict((p.pid, p) for p in procs)
                keep = set()
                for p in procs:
                    if match(p):
                        q = p
                        while q is not None and q.pid not in keep:
                            keep.add(q.pid)
                            q = by_pid.get(q.ppid)
                procs = [p for p in procs if p.pid in keep]
            rows = []
            for p, prefix, kids in build_tree(procs, sortfn, rev, self.collapsed):
                marker = (G["closed"] + " ") if kids and p.pid in self.collapsed else ""
                rows.append(DRow("proc", p, prefix + marker))
            return rows, len(procs)
        if match:
            procs = [p for p in procs if match(p)]
        if self.group:
            groups = build_groups(procs)
            groups.sort(key=lambda g: g.name.lower())
            groups.sort(key=sortfn, reverse=rev)
            rows = []
            for g in groups:
                if g.count == 1:
                    rows.append(DRow("proc", g.members[0], "  "))
                    continue
                opened = g.name in self.expanded
                rows.append(DRow("group", g, (G["open"] if opened else G["closed"]) + " "))
                if opened:
                    members = list(g.members)
                    members.sort(key=lambda p: p.pid)
                    members.sort(key=sortfn, reverse=rev)
                    rows.extend(DRow("proc", p, "    ", g.name) for p in members)
            return rows, len(procs)
        procs.sort(key=lambda p: p.pid)
        procs.sort(key=sortfn, reverse=rev)
        return [DRow("proc", p) for p in procs], len(procs)

    def draw_processes(self, y, h):
        rows, nmatch = self.build_proc_rows()
        pr = self.mon.procs
        info = " %d processes   %d threads   %d running" % (len(pr.procs), pr.total_threads, self.mon.cpu.procs_running)
        if self.filters[0]:
            info += "   filter \"%s\": %d" % (self.filters[0], nmatch)
        modes = [m for m, on in (("tree", self.tree), ("grouped by app", self.group), ("kernel threads", self.show_kernel),
                                 ("only mine", self.only_mine), ("memory = RSS", self.mem_mode == "rss"),
                                 ("CPU% per core", not self.cpu_norm)) if on]
        if modes:
            info += "   [%s]" % ", ".join(modes)
        self.put(y, 0, info, self.A["dim"])
        hint = None
        if not self.mon.procs.io_ok:
            hint = "kernel has no per-process I/O accounting"
        elif not self.mon.is_root:
            hint = "run as root to see every process's disk I/O"
        if hint and self.w - text_width(info) > len(hint) + 3:
            self.put_right(y, self.w - 1, hint, self.A["dim"])
        self.ptable.set_rows(rows, presorted=True)
        self.ptable.draw(self, y + 1, 0, h - 1, self.w)

    def row_targets(self, r):
        if r is None:
            return []
        members = r.p.members if r.kind == "group" else [r.p]
        return [(p.pid, p.starttime) for p in members]

    def describe_targets(self, targets):
        procs = self.mon.procs.procs
        if len(targets) == 1:
            pid = targets[0][0]
            p = procs.get(pid)
            return "%s (PID %d)" % (p.name, pid) if p else "PID %d" % pid
        names = set(procs[pid].name for pid, _ in targets if pid in procs)
        return "%d processes (%s)" % (len(targets), ", ".join(sorted(names))[:60])

    def end_task(self, targets):
        if not targets:
            return
        self.popup = ConfirmPopup("End task", "Ask %s to quit (SIGTERM)?" % self.describe_targets(targets),
                                  lambda: self.send_signal(targets, signal.SIGTERM))

    def signal_menu(self, targets):
        if not targets:
            return
        desc = self.describe_targets(targets)
        items = []
        for name, what in (("SIGTERM", "ask to quit"), ("SIGKILL", "force quit, cannot be ignored"),
                           ("SIGINT", "interrupt, like Ctrl-C"), ("SIGHUP", "hang up; many daemons reload"),
                           ("SIGSTOP", "pause (freeze)"), ("SIGCONT", "resume after pause"),
                           ("SIGUSR1", "user signal 1"), ("SIGUSR2", "user signal 2")):
            sig = getattr(signal, name, None)
            if sig is not None:
                items.append(("%-8s %2d  %s" % (name, int(sig), what), sig))

        def chosen(sig):
            self.popup = ConfirmPopup("Send signal", "Send %s to %s?" % (signal.Signals(sig).name, desc),
                                      lambda: self.send_signal(targets, sig))
        self.popup = MenuPopup("Signal for " + desc, items, chosen)

    def send_signal(self, targets, sig):
        ok, errors = 0, []
        for pid, starttime in targets:
            if not same_process(pid, starttime):
                errors.append("%d has already exited" % pid)
                continue
            try:
                os.kill(pid, sig)
                ok += 1
            except ProcessLookupError:
                errors.append("%d has already exited" % pid)
            except PermissionError:
                errors.append("%d: permission denied (run ltm with sudo)" % pid)
            except OSError as e:
                errors.append("%d: %s" % (pid, e.strerror))
        name = signal.Signals(sig).name
        text = "%s sent to %d process%s" % (name, ok, "" if ok == 1 else "es") if ok else "Nothing sent"
        if errors:
            text += "; " + "; ".join(errors[:3])
        self.flash(text, 6.0)

    def renice_prompt(self, targets):
        if not targets:
            return
        p = self.mon.procs.procs.get(targets[0][0])
        self.popup = InputPopup("Priority of %s" % self.describe_targets(targets), str(p.nice if p else 0),
                                lambda s: self.renice(targets, s),
                                hint="nice: -20 (highest) .. 19 (lowest); lowering it needs root")

    def renice(self, targets, text):
        try:
            value = int(text.strip())
        except ValueError:
            self.flash("Not a number: %r" % text)
            return
        if not -20 <= value <= 19:
            self.flash("Nice must be between -20 and 19")
            return
        ok, errors = 0, []
        for pid, starttime in targets:
            if not same_process(pid, starttime):
                errors.append("%d has exited" % pid)
                continue
            try:
                for tid in listdir("%s/%d/task" % (PROC, pid)) or [str(pid)]:
                    os.setpriority(os.PRIO_PROCESS, int(tid), value)
                ok += 1
            except PermissionError:
                errors.append("%d: permission denied (raising priority needs root)" % pid)
            except OSError as e:
                errors.append("%d: %s" % (pid, e.strerror))
        self.flash(("Nice %d set on %d process%s" % (value, ok, "" if ok == 1 else "es") if ok else "Nothing changed") +
                   ("; " + "; ".join(errors[:2]) if errors else ""), 6.0)

    def proc_activate(self):
        r = self.ptable.selected()
        if r is None:
            return
        if r.kind == "group":
            self.toggle_expand(r)
            return
        self.popup = DetailPopup(self, r.p.pid, r.p.starttime)

    def toggle_expand(self, r, want=None):
        if r is None:
            return
        if self.group:
            name = r.group or (r.p.name if r.kind == "group" else None)
            if name is None:
                return
            opened = name in self.expanded
            target = (not opened) if want is None else want
            if target:
                self.expanded.add(name)
            else:
                self.expanded.discard(name)
                if r.group:   # collapsing from a member row: select the group row
                    self.ptable.sel_id = ("g", name)
        elif self.tree and r.kind == "proc":
            closed = r.p.pid in self.collapsed
            target = closed if want is None else want
            if target:
                self.collapsed.discard(r.p.pid)
            else:
                self.collapsed.add(r.p.pid)

    def key_processes(self, ch):
        t = self.ptable
        r = t.selected()
        if ch == "/":
            self.open_filter(0)
        elif ch == "\x1b":
            if self.filters[0]:
                self.filters[0] = ""
                self.flash("Filter cleared")
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            self.proc_activate()
        elif ch == " ":
            self.toggle_expand(r)
        elif ch == curses.KEY_RIGHT:
            self.toggle_expand(r, True)
        elif ch == curses.KEY_LEFT:
            self.toggle_expand(r, False)
        elif ch in ("x", curses.KEY_DC):
            self.end_task(self.row_targets(r))
        elif ch == "k":
            self.signal_menu(self.row_targets(r))
        elif ch == "n":
            self.renice_prompt(self.row_targets(r))
        elif ch == "t":
            self.tree = not self.tree
            if self.group and r is not None and r.kind == "group":
                t.want = [("p", r.p.members[0].pid, r.p.members[0].starttime)]
            self.group = False
        elif ch == "a":
            self.group = not self.group
            self.tree = False
            if r is not None and self.group and r.kind == "proc":
                t.want = [("g", r.p.name)]          # the process's application, or itself if alone
            elif r is not None and not self.group and r.kind == "group":
                p = max(r.p.members, key=lambda q: q.cpu_pct)
                t.want = [("p", p.pid, p.starttime)]
        elif ch == "K":
            self.show_kernel = not self.show_kernel
        elif ch == "u":
            self.only_mine = not self.only_mine
        elif ch == "M":
            self.mem_mode = "rss" if self.mem_mode == "private" else "private"
        elif ch == "%":
            self.cpu_norm = not self.cpu_norm
        elif isinstance(ch, str) and ch in "cmdg":
            t.sort_key = {"c": "cpu", "m": "mem", "d": "disk", "g": "gpu"}[ch]
            t.sort_rev = True
        elif ch in ("<", ","):
            t.cycle_sort(-1)
        elif ch in (">", "."):
            t.cycle_sort(1)
        elif ch == "r":
            t.sort_rev = not t.sort_rev
        else:
            t.nav(ch)

    # --- Performance tab --------------------------------------------------------

    def perf_items(self):
        m = self.mon
        items = [("cpu", None), ("mem", None)]
        items += [("disk", n) for n in m.disks.order]
        items += [("net", n) for n in m.net.order]
        items += [("gpu", i) for i in range(len(m.gpu.gpus))]
        if any(s.get("value") is not None for s in m.sensors.sensors):
            items.append(("sensors", None))
        items += [("battery", b["name"]) for b in m.power.batteries]
        return items

    def perf_entry(self, kind, key):
        """(title, value, history key, graph maximum or None to autoscale, color)."""
        m = self.mon
        if kind == "cpu":
            return "CPU", "%.0f%%  %s" % (m.cpu.usage, fmt_freq(m.cpu.freq_khz) if m.cpu.freq_khz else ""), "cpu", 100, "cpu"
        if kind == "mem":
            mi = m.mem.info
            return "Memory", "%s/%s (%.0f%%)" % (fmt_bytes(mi["used"], True), fmt_bytes(mi["total"], True), mi["used_pct"]), \
                "mem", 100, "mem"
        if kind == "disk":
            d = m.disks.devices[key]
            return "Disk %s" % key, "%s  %.0f%%" % (d["kind"], d["active"]), "disk.%s.active" % key, 100, "disk"
        if kind == "net":
            n = m.net.ifaces[key]
            return "%s %s" % (n["type"], key), "%s %s  %s %s" % (G["tx"], fmt_bits(n["tx_bps"]), G["rx"], fmt_bits(n["rx_bps"])), \
                "net.%s.total" % key, None, "net"
        if kind == "gpu":
            g = m.gpu.gpus[key]
            return "GPU %d" % key, "%s %s" % (g["name"], fmt_pct(g["util"], 0) if g["util"] is not None else ""), \
                "gpu.%s.util" % g["id"], 100, "gpu"
        if kind == "sensors":
            if m.sensors.max_temp is None:
                n = sum(1 for s in m.sensors.sensors if s.get("value") is not None)
                return "Sensors", "%d readings" % n, "temp.max", 100, "sensor"
            return "Sensors", fmt_temp(m.sensors.max_temp) + " hottest", "temp.max", 100, "sensor"
        b = next((x for x in m.power.batteries if x["name"] == key), None) or {"capacity": None, "status": "?"}
        return "Battery %s" % key, "%s%% %s" % (b["capacity"], b["status"].lower()), "bat.%s" % key, 100, "bat"

    def draw_performance(self, y, h):
        items = self.perf_items()
        self.perf_sel = max(0, min(self.perf_sel, len(items) - 1))
        side_w = 30 if self.w >= 110 else 24
        per = 3 if len(items) * 3 <= h else 2
        visible = max(1, h // per)
        if self.perf_sel < self.perf_top:
            self.perf_top = self.perf_sel
        if self.perf_sel >= self.perf_top + visible:
            self.perf_top = self.perf_sel - visible + 1
        self.perf_top = max(0, min(self.perf_top, max(0, len(items) - visible)))
        for i in range(self.perf_top, min(len(items), self.perf_top + visible)):
            title, value, hkey, vmax, color = self.perf_entry(*items[i])
            yy = y + (i - self.perf_top) * per
            sel = i == self.perf_sel
            base = self.A["sel"] if sel else 0
            for k in range(per - 1):
                self.put(yy + k, 0, " " * side_w, base)
            if per == 3:
                self.put(yy, 1, title, base | curses.A_BOLD, side_w - 2)
                self.put(yy + 1, 1, value, base, side_w - 2)
            else:
                n = self.put(yy, 1, title, base | curses.A_BOLD, side_w - 2)
                self.put_right(yy, side_w - 1, clip(value, max(0, side_w - n - 3)), base)
            self.sparkline(yy + per - 1, 1, side_w - 2, self.mon.hist[hkey], vmax, self.A[color],
                           floor=1250 if items[i][0] == "net" else 0)   # 10 Kbps
            for k in range(per):
                self.add_click(yy + k, 0, side_w, (lambda j: lambda dbl: setattr(self, "perf_sel", j))(i))
        for yy in range(y, y + h):
            self.put(yy, side_w, G["v"], self.A["dim"])
        x0 = side_w + 2
        w0 = self.w - x0 - 1
        if not items or w0 < 20:
            return
        kind, key = items[self.perf_sel]
        getattr(self, "perf_" + kind)(y, x0, h, w0, key)

    def perf_header(self, y, x, w, title, right, color):
        self.put(y, x, title, self.A[color] | curses.A_BOLD)
        if right:
            self.put_right(y, x + w, right, curses.A_BOLD)

    MIN_GRAPH = 7

    def stat_rows(self, pairs, w, h, reserve=0):
        """Rows for the figures under the graphs: all of them if they fit, otherwise
        what is left once the graph has MIN_GRAPH rows (small terminals)."""
        n = len([p for p in pairs if p[1] not in (None, "")])
        ncols = 3 if w >= 120 else 2 if w >= 64 else 1
        return max(0, min((n + ncols - 1) // ncols, h - 2 - reserve - self.MIN_GRAPH))

    def perf_cpu(self, y, x, h, w, _):
        m, c = self.mon, self.mon.cpu
        s = c.static
        self.perf_header(y, x, w, "CPU", s["model"], "cpu")
        b = c.breakdown
        caches = "  ".join("%s %s" % (n, ("%gM" % round(v / 1048576.0, 1)) if v >= 1048576 else "%dK" % (v // 1024))
                           for n, v in s["caches"])
        virt = "VT-x supported" if "vmx" in s["flags"] else "AMD-V supported" if "svm" in s["flags"] else None
        if "hypervisor" in s["flags"]:
            virt = "running in a VM" + (" (%s)" % m.static["virt"] if m.static["virt"] else "")
        topo = "%s socket%s, %s cores" % (s["sockets"] or "?", "" if s["sockets"] == 1 else "s", s["cores"] or "?")
        if s["hybrid"]:
            topo += " (P: %s, E: %s)" % s["hybrid"]
        pairs = [("Utilization", "%.1f%%" % c.usage), ("Speed", fmt_freq(c.freq_khz)),
                 ("Max speed", fmt_freq(s["max_khz"]) if s["max_khz"] else None),
                 ("Topology", topo), ("Logical processors", s["logical"]),
                 ("Processes", len(m.procs.procs)), ("Threads", m.procs.total_threads),
                 ("Running / blocked", "%d / %d" % (c.procs_running, c.procs_blocked)),
                 ("Up time", fmt_duration(m.uptime)), ("Load average", "%.2f  %.2f  %.2f" % c.loadavg),
                 ("User / system", "%.1f%% / %.1f%%" % (b["user"], b["system"])),
                 ("I/O wait / steal", "%.1f%% / %.1f%%" % (b["iowait"], b["steal"])),
                 ("Context switches/s", "%d" % c.rates["ctxt"]), ("Interrupts/s", "%d" % c.rates["intr"]),
                 ("New processes/s", "%.1f" % c.rates["forks"]), ("Caches", caches or None),
                 ("Virtualization", virt), ("Temperature", fmt_temp(m.sensors.cpu_temp) if m.sensors.cpu_temp is not None else None)]
        srows = self.stat_rows(pairs, w, h)
        avail = h - 2 - srows
        cpus = sorted(c.per_cpu)
        if avail >= 3:
            if self.per_core and cpus and self.core_grid(y + 1, x, avail, w, cpus):
                pass
            else:
                bar_cols = max(1, w // 22)
                bar_rows = (len(cpus) + bar_cols - 1) // bar_cols if cpus else 0
                show_bars = bar_rows and avail - bar_rows - 1 >= 6
                gh = avail - (bar_rows + 1 if show_bars else 0)
                self.graph(y + 1, x, gh, w, m.hist["cpu"], 100.0, self.A["cpu"], "% utilization", "100%",
                           self.span_label(w - 2), "0")
                if show_bars:
                    self.core_bars(y + 1 + gh + 1, x, w, cpus, bar_cols)
        self.kv_grid(y + h - srows, x, w, pairs, srows)

    def core_bars(self, y, x, w, cpus, ncols):
        cw = w // ncols
        rows = (len(cpus) + ncols - 1) // ncols
        for i, idx in enumerate(cpus):
            r, col = i % rows, i // rows
            v = self.mon.cpu.per_cpu.get(idx, 0.0)
            xx = x + col * cw
            self.put(y + r, xx, "%3d " % idx, self.A["dim"])
            self.bar(y + r, xx + 4, cw - 10, v / 100.0, self.A["cpu"])
            self.put(y + r, xx + cw - 6, "%4.0f%%" % v, self.heat(v))

    def core_grid(self, y, x, h, w, cpus):
        n = len(cpus)
        best = None
        for cols in range(1, n + 1):
            rows = (n + cols - 1) // cols
            cw = (w - (cols - 1)) // cols
            ch = h // rows
            if cw < 8 or ch < 3:
                continue
            score = min(cw / 2.5, ch)
            if best is None or score > best[0]:
                best = (score, cols, cw, ch)
        if best is None:
            return False
        _, cols, cw, ch = best
        for i, idx in enumerate(cpus):
            r, col = divmod(i, cols)
            self.graph(y + r * ch, x + col * (cw + 1), ch, cw, self.mon.hist["cpu.%d" % idx], 100.0, self.A["cpu"],
                       str(idx), "%.0f%%" % self.mon.cpu.per_cpu.get(idx, 0.0))
        return True

    def perf_mem(self, y, x, h, w, _):
        mi = self.mon.mem.info
        self.perf_header(y, x, w, "Memory", fmt_bytes(mi["total"]), "mem")
        pairs = [("In use", "%s (%.1f%%)" % (fmt_bytes(mi["used"]), mi["used_pct"])),
                 ("Available", fmt_bytes(mi["available"])),
                 ("Committed", "%s / %s" % (fmt_bytes(mi["committed"]), fmt_bytes(mi["commit_limit"]))),
                 ("Cached", fmt_bytes(mi["cached"])), ("Buffers", fmt_bytes(mi["buffers"])),
                 ("Shared (shmem)", fmt_bytes(mi["shared"])), ("Kernel slab", fmt_bytes(mi["slab"])),
                 ("Page tables", fmt_bytes(mi["page_tables"])), ("Dirty / writeback", "%s / %s" % (
                     fmt_bytes(mi["dirty"]), fmt_bytes(mi["writeback"]))),
                 ("Swap", "%s / %s (%.0f%%)" % (fmt_bytes(mi["swap_used"]), fmt_bytes(mi["swap_total"]), mi["swap_pct"])
                  if mi["swap_total"] else "none"),
                 ("Swap cached", fmt_bytes(mi["swap_cached"]) if mi["swap_total"] else None),
                 ("Huge pages", "%d free of %d (%s each)" % (mi["hugepages_free"], mi["hugepages_total"],
                                                            fmt_bytes(mi["hugepage_size"])) if mi["hugepages_total"] else None)]
        srows = self.stat_rows(pairs, w, h, 3)
        avail = h - 2 - srows - 3
        if avail >= 3:
            self.graph(y + 1, x, avail, w, self.mon.hist["mem"], 100.0, self.A["mem"], "Memory in use",
                       fmt_bytes(mi["total"], True), self.span_label(w - 2), "0")
            cy = y + 1 + avail
            comp = mi["composition"]
            total = float(mi["total"] or 1)
            parts = [("In use", comp["in_use"], G["full"], self.A["mem"]),
                     ("Modified", comp["modified"], G["mod"], self.A["warn"]),
                     ("Standby (cache)", comp["standby"], G["stby"], self.A["cpu"]),
                     ("Free", comp["free"], G["free"], self.A["dim"])]
            self.put(cy, x, "Memory composition", self.A["dim"])
            xx = x
            for i, (_, v, ch, attr) in enumerate(parts):
                n = int(round(v / total * w)) if i < 3 else max(0, x + w - xx)
                n = min(n, x + w - xx)
                self.put(cy + 1, xx, ch * n, attr)
                xx += n
            xx = x
            for label, v, ch, attr in parts:
                xx += self.put(cy + 2, xx, ch + " ", attr)
                xx += self.put(cy + 2, xx, "%s %s   " % (label, fmt_bytes(v, True)), 0)
        self.kv_grid(y + h - srows, x, w, pairs, srows)

    def perf_disk(self, y, x, h, w, name):
        d = self.mon.disks.devices.get(name)
        if d is None:
            return
        self.perf_header(y, x, w, "Disk %s" % name + (" (%s)" % d["label"] if d["label"] else ""),
                         d["model"] or d["kind"], "disk")
        pairs = [("Active time", "%.1f%%" % d["active"]), ("Average response", "%.2f ms" % d["response_ms"]),
                 ("Read speed", fmt_rate(d["read_bps"])), ("Write speed", fmt_rate(d["write_bps"])),
                 ("Reads / writes per s", "%.0f / %.0f" % (d["read_iops"], d["write_iops"])),
                 ("Requests in flight", d["inflight"]), ("Capacity", fmt_bytes(d["size"])), ("Type", d["kind"]),
                 ("Read since boot", fmt_bytes(d["read_total"])), ("Written since boot", fmt_bytes(d["write_total"]))]
        fss = [f for f in (self.mon.fs_task.get() or []) if name in f["disks"]]
        fs_rows = min(len(fss), 5 if h >= 32 else 2 if h >= 24 else 0)
        reserve = fs_rows + 1 if fs_rows else 0
        srows = self.stat_rows(pairs, w, h, reserve)
        avail = h - 2 - srows - reserve
        if avail >= 6:
            g1 = avail // 2
            g2 = avail - g1
            self.graph(y + 1, x, g1, w, self.mon.hist["disk.%s.active" % name], 100.0, self.A["disk"], "Active time",
                       "100%", "", "0")
            hist = self.mon.hist["disk.%s.rw" % name]
            vmax = nice_scale(max(list(hist)[-(w - 2):] + [100 * 1024]), 1024)
            self.graph(y + 1 + g1, x, g2, w, hist, vmax, self.A["disk"], "Transfer rate (read + write)",
                       fmt_rate(vmax, True), self.span_label(w - 2), "0")
        elif avail >= 3:
            self.graph(y + 1, x, avail, w, self.mon.hist["disk.%s.active" % name], 100.0, self.A["disk"], "Active time", "100%")
        fy = y + h - srows - fs_rows - 1
        if fs_rows and fy > y + 1:
            self.put(fy, x, "Filesystems on this disk", self.A["dim"])
            for i, f in enumerate(fss[:fs_rows]):
                yy = fy + 1 + i
                left = "%-18s %-6s %9s of %-9s" % (clip(f["mount"], 18), f["fstype"][:6], fmt_bytes(f["used"], True),
                                                   fmt_bytes(f["total"], True))
                n = self.put(yy, x, left)
                bw = max(4, min(30, w - n - 8))
                self.bar(yy, x + n + 1, bw, f["pct"] / 100.0, self.A["crit"] if f["pct"] >= 90 else self.A["disk"])
                self.put(yy, x + n + 2 + bw, "%3.0f%%" % f["pct"])
        self.kv_grid(y + h - srows, x, w, pairs, srows)

    def perf_net(self, y, x, h, w, name):
        n = self.mon.net.ifaces.get(name)
        if n is None:
            return
        right = n["state"] + (", %d Mbps" % n["speed_mbps"] if n["speed_mbps"] else "")
        self.perf_header(y, x, w, "%s (%s)" % (n["type"], name), right, "net")
        pairs = [("Send", fmt_bits(n["tx_bps"])), ("Receive", fmt_bits(n["rx_bps"])),
                 ("Packets/s", "%.0f sent, %.0f received" % (n["tx_pps"], n["rx_pps"])),
                 ("Sent since boot", fmt_bytes(n["tx_bytes"])), ("Received since boot", fmt_bytes(n["rx_bytes"])),
                 ("Errors / drops", "%d / %d in, %d / %d out" % (n["rx_errs"], n["rx_drop"], n["tx_errs"], n["tx_drop"])),
                 ("IPv4", n["ipv4"]), ("IPv6", ", ".join(n["ipv6"][:2]) if n["ipv6"] else None),
                 ("Gateway", n["gateway"]), ("MAC", n["mac"]), ("MTU", n["mtu"]),
                 ("Signal", "%.0f dBm" % n["signal_dbm"] if n["signal_dbm"] is not None else None)]
        srows = self.stat_rows(pairs, w, h)
        avail = h - 2 - srows
        rx, tx = self.mon.hist["net.%s.rx" % name], self.mon.hist["net.%s.tx" % name]
        peak = max(list(rx)[-(w - 2):] + list(tx)[-(w - 2):] + [1250])   # at least 10 Kbps
        vmax = nice_scale(peak * 8, 1000) / 8.0
        if avail >= 6:
            g1 = avail // 2
            self.graph(y + 1, x, g1, w, rx, vmax, self.A["net"], "Receive", fmt_bits(vmax), "", "0")
            self.graph(y + 1 + g1, x, avail - g1, w, tx, vmax, self.A["good"], "Send", fmt_bits(vmax),
                       self.span_label(w - 2), "0")
        elif avail >= 3:
            self.graph(y + 1, x, avail, w, self.mon.hist["net.%s.total" % name], vmax, self.A["net"], "Throughput", fmt_bits(vmax))
        self.kv_grid(y + h - srows, x, w, pairs, srows)

    def perf_gpu(self, y, x, h, w, idx):
        if idx >= len(self.mon.gpu.gpus):
            return
        g = self.mon.gpu.gpus[idx]
        self.perf_header(y, x, w, "GPU %d" % idx, g["name"], "gpu")
        pairs = [("Utilization", fmt_pct(g["util"]) if g["util"] is not None else "not reported"),
                 ("Video memory", "%s / %s" % (fmt_bytes(g["vram_used"]), fmt_bytes(g["vram_total"])) if g["vram_total"] else None),
                 ("Temperature", fmt_temp(g["temp"]) if g["temp"] is not None else None),
                 ("Power", "%.1f W" % g["power"] if g["power"] else None),
                 ("Clock", "%.0f MHz" % g["clock_mhz"] if g["clock_mhz"] else None),
                 ("Fan", ("%.0f%%" % g["fan"] if g["source"] == "nvidia-smi" else "%d RPM" % g["fan"]) if g["fan"] is not None else None),
                 ("Driver", g["driver"]), ("PCI slot", g["pci"]), ("Source", g["source"])]
        users = [p for p in self.mon.procs.procs.values() if p.gpu_pct or p.gpu_mem]
        users.sort(key=lambda p: (p.gpu_pct or 0, p.gpu_mem or 0), reverse=True)
        urows = min(len(users), 6 if h >= 32 else 2 if h >= 24 else 0)
        reserve = urows + 1 if urows else 0
        srows = self.stat_rows(pairs, w, h, reserve)
        avail = h - 2 - srows - reserve
        has_vram = bool(g["vram_total"])
        if avail >= 3:
            g1 = avail // 2 if has_vram and avail >= 8 else avail
            self.graph(y + 1, x, g1, w, self.mon.hist["gpu.%s.util" % g["id"]], 100.0, self.A["gpu"], "Utilization", "100%",
                       self.span_label(w - 2) if g1 == avail else "", "0")
            if g1 < avail:
                self.graph(y + 1 + g1, x, avail - g1, w, self.mon.hist["gpu.%s.vram" % g["id"]], 100.0, self.A["gpu"],
                           "Video memory", fmt_bytes(g["vram_total"], True), self.span_label(w - 2), "0")
        if urows:
            uy = y + h - srows - urows - 1
            self.put(uy, x, "Processes using a GPU   %-7s %s" % ("GPU%", "VIDEO MEMORY"), self.A["dim"])
            for i, p in enumerate(users[:urows]):
                self.put(uy + 1 + i, x, "%-23s %-7s %s" % (clip("%s (%d)" % (p.name, p.pid), 23),
                                                          fmt_pct(p.gpu_pct, 0) if p.gpu_pct is not None else "-",
                                                          fmt_bytes(p.gpu_mem) if p.gpu_mem else "-"))
        self.kv_grid(y + h - srows, x, w, pairs, srows)

    def perf_sensors(self, y, x, h, w, _):
        sensors = [s for s in self.mon.sensors.sensors if s.get("value") is not None]
        self.perf_header(y, x, w, "Sensors", "hottest %s" % fmt_temp(self.mon.sensors.max_temp), "sensor")
        rows_avail = max(0, h - 2 - 8)
        gh = max(3, min(10, h - 2 - min(len(sensors) + 1, rows_avail)))
        self.graph(y + 1, x, gh, w, self.mon.hist["temp.max"], 100.0, self.A["sensor"], "Hottest temperature",
                   "100 %sC" % G["deg"], self.span_label(w - 2), "0")
        yy = y + 1 + gh + 1
        units = {"temp": lambda v: fmt_temp(v), "fan": lambda v: "%d RPM" % v, "in": lambda v: "%.3f V" % v,
                 "power": lambda v: "%.2f W" % v, "curr": lambda v: "%.3f A" % v}
        colw = 52
        ncols = max(1, w // colw)
        per_col = max(1, y + h - yy)
        for i, s in enumerate(sensors):
            r, c = i % per_col, i // per_col
            if c >= ncols:
                self.put(y + h - 1, x, "... %d more (ltm summary --json lists them all)" % (len(sensors) - i), self.A["dim"])
                break
            text = units[s["kind"]](s["value"])
            attr = 0
            if s["kind"] == "temp":
                limit = s["crit"] or s["max"]
                if limit and s["value"] >= limit - 5 or s["value"] >= 90:
                    attr = self.A["crit"]
                elif s["value"] >= 75:
                    attr = self.A["warn"]
            xx = x + c * colw
            self.put(yy + r, xx, pad("%s %s" % (s["chip"], s["label"]), 34), self.A["dim"])
            self.put(yy + r, xx + 35, pad(text, 10, "r"), attr | curses.A_BOLD)
            if s["kind"] == "temp" and (s["crit"] or s["max"]):
                self.put(yy + r, xx + 46, "/%.0f" % (s["crit"] or s["max"]), self.A["dim"])

    def perf_battery(self, y, x, h, w, name):
        b = next((v for v in self.mon.power.batteries if v["name"] == name), None)
        if b is None:
            return
        self.perf_header(y, x, w, "Battery %s" % name, b["model"], "bat")
        pairs = [("Charge", "%s%%" % b["capacity"]), ("Status", b["status"]),
                 ("Time", "%s %s" % (fmt_hours(b["hours_left"]), "to full" if b["status"] == "Charging" else "left")
                  if b["hours_left"] is not None else None),
                 ("Power draw", "%.1f W" % b["power_w"] if b["power_w"] else None),
                 ("Energy", "%.1f / %.1f Wh" % (b["energy_now_wh"], b["energy_full_wh"]) if b["energy_now_wh"] and b["energy_full_wh"] else None),
                 ("Health", "%.0f%% of design (%.1f Wh)" % (b["health_pct"], b["energy_design_wh"]) if b["health_pct"] else None),
                 ("Cycles", b["cycles"] or None), ("Technology", b["technology"]),
                 ("AC adapter", {True: "plugged in", False: "unplugged", None: None}[self.mon.power.ac_online])]
        srows = self.stat_rows(pairs, w, h)
        avail = h - 2 - srows
        if avail >= 3:
            self.graph(y + 1, x, avail, w, self.mon.hist["bat.%s" % name], 100.0, self.A["bat"], "Charge", "100%",
                       self.span_label(w - 2), "0")
        self.kv_grid(y + h - srows, x, w, pairs, srows)

    def key_performance(self, ch):
        n = len(self.perf_items())
        if ch == curses.KEY_UP:
            self.perf_sel = max(0, self.perf_sel - 1)
        elif ch == curses.KEY_DOWN:
            self.perf_sel = min(n - 1, self.perf_sel + 1)
        elif ch == curses.KEY_HOME:
            self.perf_sel = 0
        elif ch == curses.KEY_END:
            self.perf_sel = n - 1
        elif ch == "l":
            self.per_core = not self.per_core
            self.perf_sel = 0

    # --- Users tab --------------------------------------------------------------

    def user_columns(self):
        ncpu = self.ncpu
        return [
            Column("user", "USER", 18, lambda r: r["user"]),
            Column("uid", "UID", 7, lambda r: str(r["uid"]), "r"),
            Column("procs", "PROCS", 6, lambda r: str(r["procs"]), "r"),
            Column("threads", "THREADS", 8, lambda r: str(r["threads"]), "r", prio=2),
            Column("cpu", "CPU%", 6, lambda r: "%.1f" % (r["cpu"] / ncpu), "r", attr=lambda r: self.heat(r["cpu"] / ncpu)),
            Column("mem", "MEMORY", 9, lambda r: fmt_bytes(r["private"], True), "r"),
            Column("rss", "RSS", 9, lambda r: fmt_bytes(r["rss"], True), "r", prio=3),
            Column("disk", "DISK/s", 9, lambda r: fmt_rate((r["read_rate"] or 0) + (r["write_rate"] or 0), True)
                   if r["read_rate"] is not None else "-", "r", prio=1),
            Column("gpu", "GPU%", 5, lambda r: "%.0f" % r["gpu"] if r["gpu"] is not None else "-", "r", prio=4),
        ]

    def draw_users(self, y, h):
        rows = user_rows(self.mon.procs.procs)
        f = self.filters[2].lower()
        if f:
            rows = [r for r in rows if f in r["user"].lower() or f == str(r["uid"])]
        self.put(y, 0, " %d users have processes   Enter shows a user's processes" % len(rows), self.A["dim"])
        self.utable.set_rows(rows)
        self.utable.draw(self, y + 1, 0, h - 1, self.w)

    def user_activate(self):
        r = self.utable.selected()
        if r:
            self.filters[0] = "user:" + r["user"]
            self.set_tab(0)

    def key_users(self, ch):
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.user_activate()
        elif ch == "/":
            self.open_filter(2)
        elif ch == "\x1b":
            self.filters[2] = ""
        elif isinstance(ch, str) and ch in "<,":
            self.utable.cycle_sort(-1)
        elif isinstance(ch, str) and ch in ">.":
            self.utable.cycle_sort(1)
        elif ch == "r":
            self.utable.sort_rev = not self.utable.sort_rev
        else:
            self.utable.nav(ch)

    # --- Services tab -----------------------------------------------------------

    def service_columns(self):
        A = self.A
        ncpu = self.ncpu

        def state_attr(r):
            if r["sub"] == "failed" or r["active"] == "failed":
                return A["crit"]
            if r["sub"] == "running":
                return A["good"]
            if r["active"] in ("inactive", "dead"):
                return A["dim"]
            return A["warn"]
        return [
            Column("unit", "SERVICE", 34, lambda r: r["unit"][:-8] if r["unit"].endswith(".service") else r["unit"]),
            Column("state", "STATE", 16, lambda r: "%s/%s" % (r["active"], r["sub"]) if r["active"] != "active" else r["sub"],
                   attr=state_attr),
            Column("pids", "PIDS", 5, lambda r: str(r["pids"]) if r["pids"] else "", "r", prio=3),
            Column("cpu", "CPU%", 6, lambda r: "%.1f" % (r["cpu"] / ncpu) if r["pids"] else "", "r"),
            Column("mem", "MEMORY", 8, lambda r: fmt_bytes(r["private"], True) if r["pids"] else "", "r"),
            Column("disk", "DISK/s", 8, lambda r: fmt_rate(r["io_rate"], True) if r["io_rate"] is not None else "", "r", prio=2),
            Column("description", "DESCRIPTION", 20, lambda r: r["description"], flex=True, prio=1),
        ]

    def draw_services(self, y, h):
        m = self.mon
        if m.units_task is None:
            self.put(y + 1, 2, "The Services tab needs systemd, which is not running on this system.", self.A["dim"])
            self.put(y + 2, 2, "(OpenRC, runit and s6 are not supported yet; processes and performance work.)", self.A["dim"])
            return
        units = m.units_task.get()
        if units is None:
            self.put(y + 1, 2, m.units_task.error or "Asking systemd for the list of services...", self.A["dim"])
            return
        rows = service_rows(units, m.procs.procs, self.show_inactive)
        f = self.filters[3].lower()
        if f:
            rows = [r for r in rows if f in r["unit"].lower() or f in r["description"].lower()]
        running = sum(1 for r in rows if r["sub"] == "running")
        failed = sum(1 for r in rows if r["sub"] == "failed")
        info = " %d services   %d running   %d failed" % (len(rows), running, failed)
        if f:
            info += "   filter \"%s\"" % self.filters[3]
        if not self.show_inactive:
            info += "   [inactive hidden]"
        self.put(y, 0, info, self.A["dim"])
        self.stable.set_rows(rows)
        self.stable.draw(self, y + 1, 0, h - 1, self.w)

    def service_status(self):
        r = self.stable.selected()
        if r is None:
            return
        _, out, err = run_cmd_full(["systemctl", "status", "--no-pager", "--lines=40", "--full", r["unit"]], timeout=10)
        lines = [clean(l) for l in ((out or "") + (err or "")).splitlines()] or ["(no output)"]
        self.popup = TextPopup("systemctl status " + r["unit"], lines)

    def service_action(self, action):
        r = self.stable.selected()
        if r is None:
            return
        unit = r["unit"]

        def work():
            rc, out, err = run_cmd_full(["systemctl", "--no-ask-password", action, unit], timeout=90)
            if rc == 0:
                self.flash("%s %s: done" % (action.capitalize(), unit), 6)
            else:
                msg = (err or out or "failed").strip().splitlines()
                hint = " (run ltm with sudo)" if "access denied" in (err or "").lower() or "interactive authentication" in (err or "").lower() else ""
                self.flash("%s %s failed: %s%s" % (action.capitalize(), unit, msg[-1] if msg else "?", hint), 10)
            if self.mon.units_task:
                self.mon.units_task.refresh()

        def go():
            self.flash("%s %s ..." % (action.capitalize(), unit), 30)
            t = threading.Thread(target=work)
            t.daemon = True
            t.start()
        self.popup = ConfirmPopup("%s service" % action.capitalize(), "%s %s?" % (action.capitalize(), unit), go)

    def key_services(self, ch):
        t = self.stable
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.service_status()
        elif ch == "/":
            self.open_filter(3)
        elif ch == "\x1b":
            self.filters[3] = ""
        elif ch == "S":
            self.service_action("start")
        elif ch == "T":
            self.service_action("stop")
        elif ch == "R":
            self.service_action("restart")
        elif ch == "A":
            self.show_inactive = not self.show_inactive
        elif isinstance(ch, str) and ch in "<,":
            t.cycle_sort(-1)
        elif isinstance(ch, str) and ch in ">.":
            t.cycle_sort(1)
        elif ch == "r":
            t.sort_rev = not t.sort_rev
        elif isinstance(ch, str) and ch in "cm":
            t.sort_key = {"c": "cpu", "m": "mem"}[ch]
            t.sort_rev = True
        else:
            t.nav(ch)

    # --- Startup tab ------------------------------------------------------------

    def startup_columns(self):
        return [
            Column("name", "NAME", 32, lambda r: r["name"]),
            Column("type", "TYPE", 18, lambda r: r["type"]),
            Column("status", "STATUS", 9, lambda r: "enabled" if r["enabled"] else "disabled",
                   attr=lambda r: self.A["good"] if r["enabled"] else self.A["dim"]),
            Column("command", "COMMAND / DESCRIPTION", 20,
                   lambda r: (r["command"] + ("   (%s)" % r["note"] if r["note"] else "")).strip(), flex=True, prio=1),
        ]

    def draw_startup(self, y, h):
        rows = self.mon.startup_task.get()
        if rows is None:
            self.put(y + 1, 2, self.mon.startup_task.error or "Reading autostart entries and enabled services...", self.A["dim"])
            return
        rows = list(rows)
        f = self.filters[4].lower()
        if f:
            rows = [r for r in rows if f in r["name"].lower() or f in r["command"].lower() or f in r["type"]]
        enabled = sum(1 for r in rows if r["enabled"])
        self.put(y, 0, " %d entries, %d enabled: login autostart (.desktop) and services enabled at boot" % (len(rows), enabled),
                 self.A["dim"])
        self.sttable.set_rows(rows)
        self.sttable.draw(self, y + 1, 0, h - 1, self.w)

    def startup_show(self):
        r = self.sttable.selected()
        if r is None:
            return
        if r["type"].startswith("systemd"):
            argv = ["systemctl"] + (["--user"] if "user" in r["type"] else []) + ["cat", "--no-pager", r["path"]]
            _, out, err = run_cmd_full(argv, timeout=10)
            text = (out or "") + (err or "")
        else:
            text = "# %s\n\n%s" % (r["path"], read_text(r["path"], "(not readable)"))
        self.popup = TextPopup(r["name"], [clean(l) for l in text.splitlines()] or ["(empty)"])

    def key_startup(self, ch):
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.startup_show()
        elif ch == "/":
            self.open_filter(4)
        elif ch == "\x1b":
            self.filters[4] = ""
        else:
            self.sttable.nav(ch)


def run_tui(args):
    if curses is None:
        sys.stderr.write("ltm: the interactive view needs Python's curses module, which is missing.\n"
                         "  openSUSE: sudo zypper install python3-curses\n"
                         "  Other commands work without it: ltm summary, ltm top, ltm proc PID\n")
        return 1
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        sys.stderr.write("ltm: the interactive view needs a terminal; try `ltm summary` or `ltm top`.\n")
        return 1
    os.environ.setdefault("ESCDELAY", "25")
    mon = Monitor(args.all_disks, args.all_nics)

    def main(scr):
        App(scr, mon, args).run()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    return 0

# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


EPILOG = """\
commands:
  ltm                     interactive task manager (when run in a terminal)
  ltm summary             CPU, memory, disks, network, GPU, sensors, battery, top processes
  ltm top [-n N] [-s KEY] processes sorted by cpu, mem, disk, gpu, time, pid, name ...
  ltm proc PID|NAME       everything about one process
  ltm users | services | startup

Every command takes --json for scripts, e.g.  ltm top -n 5 --json | jq '.[].name'
Run as root (sudo ltm) to see disk I/O and open files of every user's processes.
"""


def add_common(p, json_flag=True):
    p.add_argument("-i", "--interval", type=float, metavar="SEC", help="sampling interval in seconds (default 1)")
    p.add_argument("--no-color", action="store_true", help="no colors (also honours NO_COLOR)")
    p.add_argument("--ascii", action="store_true", help="plain ASCII instead of Unicode bars and lines")
    if json_flag:
        p.add_argument("--json", action="store_true", help="machine-readable output")


def add_device_opts(p):
    p.add_argument("--all-disks", action="store_true", help="also show loop, RAM and device-mapper disks")
    p.add_argument("--all-nics", action="store_true", help="also show loopback and veth adapters")


def add_tui_opts(p):
    p.add_argument("--tab", choices=[t.lower() for t in App.TABS], help="tab to open on")
    p.add_argument("--sort", choices=sorted(App.PROC_SORTS), help="process sort column (default cpu)")
    p.add_argument("--filter", metavar="TEXT", help="start with this process filter")
    p.add_argument("--user", metavar="NAME", help="only this user's processes")
    p.add_argument("--tree", action="store_true", help="start in tree view")
    add_device_opts(p)


def build_parser():
    p = argparse.ArgumentParser(prog="ltm", description="ltm: task manager for the Linux terminal.",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version="ltm " + __version__)
    add_common(p, json_flag=False)
    add_tui_opts(p)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    s = sub.add_parser("tui", help="interactive task manager (the default)")
    add_common(s, json_flag=False)
    add_tui_opts(s)
    s = sub.add_parser("summary", aliases=["sys", "overview"], help="one-shot system overview")
    add_common(s)
    add_device_opts(s)
    s = sub.add_parser("top", aliases=["ps"], help="processes, sorted")
    add_common(s)
    s.add_argument("-n", "--limit", type=int, metavar="N", help="show N processes (0 = all; default fits the terminal)")
    s.add_argument("-s", "--sort", choices=sorted(CLI_SORTS), default="cpu", help="sort key (default cpu)")
    s.add_argument("-r", "--reverse", action="store_true", help="reverse the sort order")
    s.add_argument("-u", "--user", metavar="NAME", help="only this user's processes")
    s.add_argument("-f", "--filter", metavar="TEXT", help="only processes whose name/command/PID contains TEXT")
    s.add_argument("-t", "--tree", action="store_true", help="parent/child tree")
    s.add_argument("-k", "--kernel", action="store_true", help="include kernel threads")
    s.add_argument("-w", "--wide", action="store_true", help="do not cut long command lines")
    s = sub.add_parser("proc", aliases=["info"], help="everything about one process")
    add_common(s)
    s.add_argument("target", metavar="PID|NAME")
    for name, text in (("users", "resource use per user"), ("services", "systemd services with their CPU and memory"),
                       ("startup", "programs started at login or boot")):
        s = sub.add_parser(name, help=text)
        add_common(s)
        if name == "services":
            s.add_argument("-a", "--all", action="store_true", help="include inactive services")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux") and "LTM_PROC" not in os.environ:
        sys.stderr.write("ltm reads /proc and /sys, so it only runs on Linux.\n")
        return 1
    utf8 = terminal_is_utf8()
    cmd = args.cmd
    if cmd is None:
        cmd = "tui" if sys.stdout.isatty() and sys.stdin.isatty() else "summary"
    if cmd == "tui":
        set_unicode(utf8 and not args.ascii)
        for name in ("tab", "sort", "filter", "user", "tree"):
            if not hasattr(args, name):
                setattr(args, name, None)
        return run_tui(args)
    enc = getattr(sys.stdout, "encoding", None) or ""
    set_unicode("UTF" in enc.upper() and not getattr(args, "ascii", False))
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `ltm top | head` should not print a traceback
    except (AttributeError, ValueError):
        pass
    handlers = {"summary": cmd_summary, "sys": cmd_summary, "overview": cmd_summary, "top": cmd_top, "ps": cmd_top,
                "proc": cmd_proc, "info": cmd_proc, "users": cmd_users, "services": cmd_services, "startup": cmd_startup}
    try:
        return handlers[cmd](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
