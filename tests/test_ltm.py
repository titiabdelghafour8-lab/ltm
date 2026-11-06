#!/usr/bin/env python3
"""Tests for ltm. Standard library only:  python3 -m unittest discover -s tests -v

Parsers and samplers run against fake /proc and /sys trees, so the numbers are exact.
The last classes run the real commands and the curses interface on the machine itself
(Linux only).
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    mock = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ltm  # noqa: E402

LINUX = sys.platform.startswith("linux")


def write(path, text):
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w") as f:
        f.write(text)


class FakeTree(unittest.TestCase):
    """Points ltm at temporary /proc and /sys directories."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ltm-test-")
        self.proc = os.path.join(self.tmp, "proc")
        self.sys = os.path.join(self.tmp, "sys")
        os.makedirs(self.proc)
        os.makedirs(self.sys)
        self._saved = (ltm.PROC, ltm.SYS, list(ltm._IO_ACCOUNTING))
        ltm.PROC, ltm.SYS = self.proc, self.sys
        del ltm._IO_ACCOUNTING[:]
        write(self.proc + "/stat", "cpu  0 0 0 0 0 0 0 0 0 0\ncpu0 0 0 0 0 0 0 0 0 0 0\nbtime 1700000000\n")
        write(self.proc + "/self/io", "rchar: 0\n")

    def tearDown(self):
        ltm.PROC, ltm.SYS = self._saved[0], self._saved[1]
        ltm._IO_ACCOUNTING[:] = self._saved[2]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_proc(self, pid, comm="bash", state="S", ppid=1, utime=0, stime=0, starttime=100, threads=1, uid=1000,
                  rss=100, shared=40, io=None, args=None, cgroup="0::/user.slice/user-1000.slice/session-1.scope\n",
                  flags=0):
        d = "%s/%d" % (self.proc, pid)
        rest = [state, ppid, pid, pid, 0, -1, flags, 0, 0, 0, 0, utime, stime, 0, 0, 20, 0, threads, 0, starttime,
                1000000, rss] + [0] * 30
        write(d + "/stat", "%d (%s) %s\n" % (pid, comm, " ".join(str(x) for x in rest)))
        write(d + "/statm", "250 %d %d 10 0 50 0\n" % (rss, shared))
        write(d + "/status", "Name:\t%s\nUid:\t%d\t%d\t%d\t%d\nGid:\t%d\t%d\t%d\t%d\n" % ((comm,) + (uid,) * 8))
        write(d + "/cmdline", "\x00".join(args if args is not None else [comm]) + ("\x00" if args != [] else ""))
        write(d + "/cgroup", cgroup)
        if io is not None:
            write(d + "/io", "rchar: 0\nwchar: 0\nsyscr: 0\nsyscw: 0\nread_bytes: %d\nwrite_bytes: %d\n"
                             "cancelled_write_bytes: 0\n" % io)


class TestParsers(unittest.TestCase):
    def test_pid_stat_name_with_spaces_and_parens(self):
        st = ltm.parse_pid_stat("42 (a) (b c) R 7 42 42 0 -1 4194560 1 0 0 0 30 12 0 0 20 0 3 0 555 "
                                "8192 10 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 5 0 0 9\n")
        self.assertEqual(st["comm"], "a) (b c")
        self.assertEqual((st["state"], st["ppid"], st["utime"], st["stime"]), ("R", 7, 30, 12))
        self.assertEqual((st["threads"], st["starttime"], st["processor"], st["blkio_ticks"]), (3, 555, 5, 9))
        self.assertIsNone(ltm.parse_pid_stat("garbage"))
        self.assertIsNone(ltm.parse_pid_stat(""))

    def test_cpu_percent(self):
        busy, parts = ltm.cpu_percent([100, 0, 100, 800, 0, 0, 0, 0], [150, 0, 150, 900, 0, 0, 0, 0])
        self.assertAlmostEqual(busy, 50.0)
        self.assertAlmostEqual(parts["user"], 25.0)
        self.assertAlmostEqual(parts["system"], 25.0)
        busy, parts = ltm.cpu_percent([0] * 10, [0, 0, 0, 50, 50, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(busy, 0.0)
        self.assertAlmostEqual(parts["iowait"], 50.0)
        self.assertEqual(ltm.cpu_percent([5] * 10, [5] * 10)[0], 0.0)    # no time passed

    def test_proc_stat(self):
        st = ltm.parse_proc_stat("cpu  1 2 3 4 5 6 7 8 0 0\ncpu0 1 1 1 1 1 1 1 1 0 0\ncpu3 2 2 2 2 2 2 2 2\n"
                                 "intr 99 1 2\nctxt 1234\nbtime 1700000000\nprocesses 77\nprocs_running 3\n")
        self.assertEqual(st["cpu"][:4], [1, 2, 3, 4])
        self.assertEqual(sorted(st["cpus"]), [0, 3])
        self.assertEqual((st["ctxt"], st["intr"], st["btime"], st["processes"], st["procs_running"]),
                         (1234, 99, 1700000000, 77, 3))

    def test_meminfo_and_composition(self):
        mi = ltm.parse_meminfo("MemTotal: 1000 kB\nMemFree: 200 kB\nMemAvailable: 600 kB\nBuffers: 50 kB\n"
                               "Cached: 300 kB\nSReclaimable: 20 kB\nShmem: 10 kB\nDirty: 30 kB\nWriteback: 0 kB\n"
                               "SwapTotal: 400 kB\nSwapFree: 100 kB\nHugePages_Total: 0\n")
        self.assertEqual(mi["MemTotal"], 1024000)
        self.assertEqual(mi["HugePages_Total"], 0)
        s = ltm.memory_summary(mi)
        self.assertEqual(s["used"], 400 * 1024)
        self.assertAlmostEqual(s["used_pct"], 40.0)
        self.assertEqual(s["swap_used"], 300 * 1024)
        self.assertAlmostEqual(s["swap_pct"], 75.0)
        comp = s["composition"]
        self.assertEqual(sum(comp.values()), s["total"])
        self.assertEqual(comp["modified"], 30 * 1024)

    def test_meminfo_without_memavailable(self):
        mi = ltm.parse_meminfo("MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 10 kB\nCached: 200 kB\n"
                               "SReclaimable: 40 kB\nShmem: 50 kB\n")
        self.assertEqual(mi["MemAvailable"], (100 + 10 + 200 + 40 - 50) * 1024)

    def test_net_dev(self):
        text = ("Inter-|   Receive                            |  Transmit\n"
                " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
                "    lo: 100 2 0 0 0 0 0 0 100 2 0 0 0 0 0 0\n"
                "  eth0: 5000 50 1 2 0 0 0 0 7000 70 3 4 0 0 0 0\n")
        d = ltm.parse_net_dev(text)
        self.assertEqual(sorted(d), ["eth0", "lo"])
        self.assertEqual((d["eth0"]["rx_bytes"], d["eth0"]["tx_bytes"], d["eth0"]["rx_errs"], d["eth0"]["tx_drop"]),
                         (5000, 7000, 1, 4))

    def test_cgroup(self):
        self.assertEqual(ltm.parse_cgroup("0::/system.slice/sshd.service\n")[1:], ("sshd.service", None, None))
        self.assertEqual(ltm.parse_cgroup("0::/system.slice/system-getty.slice/getty@tty1.service\n")[1],
                         "getty@tty1.service")
        v1 = "12:memory:/system.slice/cron.service\n1:name=systemd:/system.slice/cron.service\n"
        self.assertEqual(ltm.parse_cgroup(v1)[1], "cron.service")
        cid = "a" * 64
        self.assertEqual(ltm.parse_cgroup("0::/system.slice/docker-%s.scope\n" % cid)[3], "a" * 12)
        self.assertEqual(ltm.parse_cgroup("11:cpu:/docker/%s\n" % cid)[3], "a" * 12)
        self.assertEqual(ltm.parse_cgroup("0::/kubepods.slice/kubepods-pod1.slice/cri-containerd-%s.scope\n" % cid)[3],
                         "a" * 12)
        path, unit, user_unit, _ = ltm.parse_cgroup(
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox-12.scope\n")
        self.assertEqual((unit, user_unit), ("user@1000.service", "app-firefox-12.scope"))
        self.assertEqual(ltm.parse_cgroup("0::/init.scope\n")[1], "init.scope")

    def test_net_addresses(self):
        self.assertEqual(ltm.decode_net_addr("0100007F:0035"), "127.0.0.1:53")
        self.assertEqual(ltm.decode_net_addr("00000000000000000000000001000000:0016"), "[::1]:22")
        self.assertEqual(ltm.decode_net_addr("zz:zz"), "zz:zz")
        table = ltm.parse_socket_table(
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 4242 1\n"
            "   1: 0100007F:0278 0100007F:1F90 01 00000000:00000000 00:00000000 00000000     0        0 0 1\n", "tcp")
        self.assertEqual(table, {4242: ("tcp", "127.0.0.1:631", "0.0.0.0:0", "LISTEN")})

    def test_drm_fdinfo_amdgpu_and_xe(self):
        a1 = ltm.parse_drm_fdinfo("drm-driver:\tamdgpu\ndrm-pdev:\t0000:03:00.0\ndrm-client-id:\t7\n"
                                  "drm-engine-gfx:\t1000000000 ns\ndrm-engine-compute:\t0 ns\ndrm-memory-vram:\t2048 KiB\n")
        a2 = ltm.parse_drm_fdinfo("drm-driver:\tamdgpu\ndrm-pdev:\t0000:03:00.0\ndrm-client-id:\t7\n"
                                  "drm-engine-gfx:\t1500000000 ns\ndrm-engine-compute:\t100000000 ns\n")
        self.assertEqual((a1["client"], a1["pdev"], a1["mem"]), ("7", "0000:03:00.0", 2048 * 1024))
        busy = ltm.drm_client_busy(a2, a1, 1.0)
        self.assertAlmostEqual(busy["gfx"], 0.5)
        self.assertAlmostEqual(busy["compute"], 0.1)
        self.assertEqual(ltm.drm_client_busy(a2, None, 1.0), {})
        x1 = ltm.parse_drm_fdinfo("drm-driver: xe\ndrm-client-id: 3\ndrm-cycles-rcs: 100\ndrm-total-cycles-rcs: 1000\n"
                                  "drm-resident-vram0: 4 MiB\n")
        x2 = ltm.parse_drm_fdinfo("drm-driver: xe\ndrm-client-id: 3\ndrm-cycles-rcs: 400\ndrm-total-cycles-rcs: 2000\n")
        self.assertAlmostEqual(ltm.drm_client_busy(x2, x1, 1.0)["rcs"], 0.3)
        self.assertEqual(x1["mem"], 4 * 1048576)
        self.assertIsNone(ltm.parse_drm_fdinfo("pos: 0\nflags: 02\n"))

    def test_systemctl_units(self):
        units = ltm.parse_systemctl_units(
            "  sshd.service  loaded active running OpenSSH server daemon\n"
            "● bad.service loaded failed failed Something broken\n"
            "* old.service loaded failed failed Old marker\n"
            "garbage\n")
        self.assertEqual([u["unit"] for u in units], ["sshd.service", "bad.service", "old.service"])
        self.assertEqual(units[0]["description"], "OpenSSH server daemon")
        self.assertEqual(units[1]["sub"], "failed")

    def test_desktop_entry(self):
        e = ltm.parse_desktop_entry("# c\n[Desktop Entry]\nName=Foo\nExec=foo --bar\nHidden=true\n"
                                    "[Desktop Action x]\nName=Other\n")
        self.assertEqual((e["Name"], e["Exec"], e["Hidden"]), ("Foo", "foo --bar", "true"))

    def test_tty(self):
        self.assertEqual(ltm.decode_tty(34816), "pts/0")      # major 136, minor 0
        self.assertEqual(ltm.decode_tty(34817 + 1), "pts/2")
        self.assertEqual(ltm.decode_tty(1025), "tty1")
        self.assertIsNone(ltm.decode_tty(0))

    def test_names(self):
        self.assertEqual(ltm.derive_name("chrome_crashpad", ["/opt/google/chrome/chrome_crashpad_handler"], False),
                         "chrome_crashpad_handler")
        self.assertEqual(ltm.derive_name("Isolated Web Co", ["/usr/lib/firefox/firefox", "-contentproc"], False),
                         "Isolated Web Co")
        self.assertEqual(ltm.derive_name("kworker/0:1", [], True), "kworker/0:1")


class TestText(unittest.TestCase):
    def setUp(self):
        ltm.set_unicode(True)

    def test_clean_removes_terminal_escapes(self):
        self.assertEqual(ltm.clean("evil\x1b[2Jname\x07\tx\ny"), "evil?[2Jname? x y")
        self.assertEqual(ltm.clean(None), "")

    def test_widths(self):
        self.assertEqual(ltm.text_width("abc"), 3)
        self.assertEqual(ltm.text_width(u"中文"), 4)      # two wide CJK characters
        self.assertEqual(ltm.clip("abcdef", 4), u"abc…")
        self.assertEqual(ltm.text_width(ltm.clip(u"中文中文", 5)), 5)
        self.assertEqual(ltm.pad("ab", 5, "r"), "   ab")
        self.assertEqual(ltm.pad("abcdefgh", 5), u"abcd…")
        ltm.set_unicode(False)
        self.assertEqual(ltm.clip("abcdef", 4), "abc~")

    def test_formatting(self):
        self.assertEqual(ltm.fmt_bytes(512), "512 B")
        self.assertEqual(ltm.fmt_bytes(1536), "1.50 KiB")
        self.assertEqual(ltm.fmt_bytes(5 * 1024 ** 3, True), "5.0G")
        self.assertEqual(ltm.fmt_bytes(None), "-")
        self.assertEqual(ltm.fmt_bits(125000), "1.0 Mbps")
        self.assertEqual(ltm.fmt_bits(0), "0 bps")
        self.assertEqual(ltm.fmt_duration(90061), "1d 01:01:01")
        self.assertEqual(ltm.fmt_cputime(75.5), "1:15.50")
        self.assertEqual(ltm.fmt_cputime(7200), "2h00m")
        self.assertEqual(ltm.fmt_freq(2400000), "2.40 GHz")
        self.assertEqual(ltm.compact_ranges([0, 1, 2, 3, 5, 7, 8]), "0-3,5,7-8")
        self.assertEqual(ltm.nice_scale(730, 1000), 1000)
        self.assertEqual(ltm.nice_scale(3 * 1024 * 1024, 1024), 5 * 1024 * 1024)
        self.assertEqual(ltm.nice_scale(0, 1024), 1.0)


class TestSamplers(FakeTree):
    def test_process_cpu_memory_io(self):
        self.make_proc(1, "systemd", ppid=0, uid=0, cgroup="0::/init.scope\n", io=(0, 0))
        self.make_proc(200, "worker", utime=10, stime=0, io=(0, 0), args=["/usr/bin/worker", "--fast"])
        ps = ltm.ProcSampler(ncpu=4)
        ps.update(now=0.0)
        self.assertEqual(sorted(ps.procs), [1, 200])
        p = ps.procs[200]
        self.assertEqual(p.cpu_pct, 0.0)                     # first sample: no rate yet
        self.assertEqual(p.private, 60 * ltm.PAGE_SIZE)      # 100 resident - 40 shared pages
        self.assertEqual((p.name, p.cmd, p.uid), ("worker", "/usr/bin/worker --fast", 1000))
        self.make_proc(200, "worker", utime=10 + ltm.CLK_TCK // 2, stime=0, io=(1048576, 2 * 1048576),
                       args=["/usr/bin/worker", "--fast"])
        ps.update(now=1.0)
        p = ps.procs[200]
        self.assertAlmostEqual(p.cpu_pct, 50.0, places=3)    # half a core
        self.assertAlmostEqual(p.read_rate, 1048576.0)
        self.assertAlmostEqual(p.write_rate, 2 * 1048576.0)
        self.assertEqual(ps.procs[1].unit, "init.scope")

    def test_pid_reuse_and_exec(self):
        self.make_proc(300, "old", utime=5, starttime=100)
        ps = ltm.ProcSampler(ncpu=1)
        ps.update(now=0.0)
        first = ps.procs[300]
        self.make_proc(300, "new", utime=7, starttime=900)    # different start time: another process
        ps.update(now=2.0)
        self.assertIsNot(ps.procs[300], first)
        self.assertEqual(ps.procs[300].name, "new")
        self.assertAlmostEqual(ps.procs[300].cpu_pct, 7.0 / ltm.CLK_TCK / 2.0 * 100.0)
        self.make_proc(300, "exec-ed", utime=7, starttime=900, args=["/bin/exec-ed"])  # same process, exec()
        ps.update(now=3.0)
        self.assertEqual(ps.procs[300].name, "exec-ed")
        self.assertEqual(ps.procs[300].cmd, "/bin/exec-ed")

    def test_kernel_threads_and_io_permission(self):
        self.make_proc(2, "kthreadd", ppid=0, uid=0, args=[], flags=ltm.KTHREAD_FLAG)
        self.make_proc(17, "kworker/0:1", ppid=2, uid=0, args=[], flags=ltm.KTHREAD_FLAG)
        self.make_proc(400, "secret", uid=0)                  # no io file, as for another user's process
        ps = ltm.ProcSampler(ncpu=1)
        ps.update(now=0.0)
        self.assertTrue(ps.procs[2].kernel and ps.procs[17].kernel)
        self.assertFalse(ps.procs[400].kernel)
        self.assertTrue(ps.procs[400].io_denied)
        self.assertIsNone(ps.procs[400].io_rate)

    def test_no_io_accounting(self):
        os.remove(self.proc + "/self/io")
        del ltm._IO_ACCOUNTING[:]
        self.make_proc(500, "x", io=(5, 5))
        ps = ltm.ProcSampler(ncpu=1)
        self.assertFalse(ps.io_ok)
        ps.update(now=0.0)
        self.assertIsNone(ps.procs[500].io_rate)

    def test_cpu_sampler(self):
        cs = ltm.CpuSampler()
        write(self.proc + "/stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0 0 0\n"
                                   "cpu1 50 0 50 400 0 0 0 0 0 0\nctxt 1000\nprocesses 10\n")
        write(self.proc + "/loadavg", "0.50 0.25 0.10 2/300 999\n")
        cs.update(None)
        write(self.proc + "/stat", "cpu  200 0 200 1000 0 0 0 0 0 0\ncpu0 150 0 50 400 0 0 0 0 0 0\n"
                                   "cpu1 50 0 150 600 0 0 0 0 0 0\nctxt 3000\nprocesses 14\n")
        cs.update(2.0)
        self.assertAlmostEqual(cs.usage, 50.0)
        self.assertAlmostEqual(cs.per_cpu[0], 100.0)
        self.assertAlmostEqual(cs.per_cpu[1], 100.0 / 3)
        self.assertAlmostEqual(cs.rates["ctxt"], 1000.0)
        self.assertAlmostEqual(cs.rates["forks"], 2.0)
        self.assertEqual(cs.loadavg, (0.5, 0.25, 0.1))

    def test_disks(self):
        os.makedirs(self.sys + "/block/sda/device")
        os.makedirs(self.sys + "/block/loop0")
        write(self.sys + "/block/sda/size", "2097152\n")
        write(self.sys + "/block/sda/queue/rotational", "0\n")
        write(self.sys + "/block/sda/device/model", "FakeDisk 9000\n")
        line = "   8       0 sda %d 0 %d %d %d 0 %d %d 0 %d %d 0 0 0 0\n"
        write(self.proc + "/diskstats", line % (10, 100, 5, 20, 400, 10, 100, 100) +
              "   7       0 loop0 1 0 1 1 0 0 0 0 0 1 1 0 0 0 0\n")
        ds = ltm.DiskSampler()
        ds.update(None)
        write(self.proc + "/diskstats", line % (110, 2148, 105, 120, 2448, 110, 600, 100))
        ds.update(1.0)
        self.assertEqual(ds.order, ["sda"])                   # loop devices are hidden
        d = ds.devices["sda"]
        self.assertEqual((d["kind"], d["model"], d["size"]), ("SSD", "FakeDisk 9000", 1024 ** 3))
        self.assertAlmostEqual(d["read_bps"], 1048576.0)      # 2048 sectors of 512 bytes
        self.assertAlmostEqual(d["write_bps"], 1048576.0)
        self.assertAlmostEqual(d["read_iops"], 100.0)
        self.assertAlmostEqual(d["active"], 50.0)             # 500 ms busy in 1 s
        self.assertAlmostEqual(d["response_ms"], 1.0)         # 200 ms over 200 requests

    def test_network_rates(self):
        os.makedirs(self.sys + "/class/net/eth0/device")
        write(self.sys + "/class/net/eth0/operstate", "up\n")
        write(self.sys + "/class/net/eth0/speed", "1000\n")
        header = "Inter-|\n face |\n"
        write(self.proc + "/net/dev", header + "eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
                                               "lo: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n")
        ns = ltm.NetSampler()
        ns.update(None)
        write(self.proc + "/net/dev", header + "eth0: 126000 110 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
                                               "lo: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n")
        ns.update(1.0)
        self.assertEqual(ns.order, ["eth0"])
        n = ns.ifaces["eth0"]
        self.assertEqual((n["type"], n["state"], n["speed_mbps"]), ("Ethernet", "up", 1000))
        self.assertAlmostEqual(n["rx_bps"], 125000.0)
        self.assertEqual(ltm.fmt_bits(n["rx_bps"]), "1.0 Mbps")

    def test_battery(self):
        b = self.sys + "/class/power_supply/BAT0/"
        for name, value in (("type", "Battery"), ("status", "Discharging"), ("capacity", "50"),
                            ("energy_now", "25000000"), ("energy_full", "50000000"),
                            ("energy_full_design", "62500000"), ("power_now", "12500000")):
            write(b + name, value + "\n")
        write(self.sys + "/class/power_supply/AC/type", "Mains\n")
        write(self.sys + "/class/power_supply/AC/online", "0\n")
        ps = ltm.PowerSampler()
        ps.update()
        bat = ps.batteries[0]
        self.assertEqual(bat["capacity"], 50)
        self.assertAlmostEqual(bat["hours_left"], 2.0)
        self.assertAlmostEqual(bat["health_pct"], 80.0)
        self.assertIs(ps.ac_online, False)

    def test_sensors(self):
        h = self.sys + "/class/hwmon/hwmon0/"
        write(h + "name", "coretemp\n")
        write(h + "temp1_input", "61000\n")
        write(h + "temp1_label", "Package id 0\n")
        write(h + "temp1_crit", "100000\n")
        write(h + "temp2_input", "70000\n")
        write(h + "temp2_label", "Core 0\n")
        write(self.sys + "/class/hwmon/hwmon1/name", "nct6775\n")
        write(self.sys + "/class/hwmon/hwmon1/fan1_input", "1200\n")
        ss = ltm.SensorSampler()
        ss.update()
        self.assertEqual(ss.cpu_temp, 61.0)                   # the package sensor, not the hottest core
        self.assertEqual(ss.max_temp, 70.0)
        fan = [s for s in ss.sensors if s["kind"] == "fan"][0]
        self.assertEqual(fan["value"], 1200)


class TestViews(FakeTree):
    def sample(self):
        self.make_proc(1, "systemd", ppid=0, uid=0, cgroup="0::/init.scope\n")
        self.make_proc(10, "sshd", ppid=1, uid=0, cgroup="0::/system.slice/sshd.service\n")
        self.make_proc(11, "sshd", ppid=10, uid=1000, cgroup="0::/system.slice/sshd.service\n")
        self.make_proc(12, "bash", ppid=11, uid=1000)
        self.make_proc(13, "vim", ppid=12, uid=1000)
        self.make_proc(14, "bash", ppid=1, uid=1000)
        ps = ltm.ProcSampler(ncpu=2)
        ps.update(now=0.0)
        return ps

    def test_tree(self):
        ltm.set_unicode(True)
        ps = self.sample()
        rows = ltm.build_tree(list(ps.procs.values()), lambda p: p.pid, False)
        self.assertEqual([p.pid for p, _, _ in rows], [1, 10, 11, 12, 13, 14])
        prefixes = dict((p.pid, pre) for p, pre, _ in rows)
        self.assertEqual(prefixes[1], "")
        self.assertEqual(prefixes[10], u"├─ ")
        self.assertEqual(prefixes[11], u"│  └─ ")
        self.assertEqual(prefixes[12], u"│     └─ ")
        self.assertEqual(prefixes[13], u"│        └─ ")
        self.assertEqual(prefixes[14], u"└─ ")
        collapsed = ltm.build_tree(list(ps.procs.values()), lambda p: p.pid, False, collapsed={10})
        self.assertEqual([p.pid for p, _, _ in collapsed], [1, 10, 14])

    def test_groups_users_filter(self):
        ps = self.sample()
        groups = dict((g.name, g) for g in ltm.build_groups(list(ps.procs.values())))
        self.assertEqual(groups["sshd"].count, 2)
        self.assertEqual(groups["sshd"].user, "2 users")
        self.assertEqual(groups["bash"].private, 2 * 60 * ltm.PAGE_SIZE)
        users = dict((u["uid"], u) for u in ltm.user_rows(ps.procs))
        self.assertEqual(users[1000]["procs"], 4)
        match = ltm.make_matcher("user:root ssh")
        self.assertEqual(sorted(p.pid for p in ps.procs.values() if match(p)), [10])
        match = ltm.make_matcher("sshd.service")
        self.assertEqual(sorted(p.pid for p in ps.procs.values() if match(p)), [10, 11])
        self.assertIsNone(ltm.make_matcher("  "))

    def test_services(self):
        ps = self.sample()
        units = [{"unit": "sshd.service", "load": "loaded", "active": "active", "sub": "running", "description": "SSH"},
                 {"unit": "cups.service", "load": "loaded", "active": "inactive", "sub": "dead", "description": "CUPS"},
                 {"unit": "gone.service", "load": "not-found", "active": "inactive", "sub": "dead", "description": ""},
                 {"unit": "x.socket", "load": "loaded", "active": "active", "sub": "listening", "description": ""}]
        rows = ltm.service_rows(units, ps.procs)
        self.assertEqual([r["unit"] for r in rows], ["sshd.service", "cups.service"])
        self.assertEqual((rows[0]["pids"], rows[0]["pid_list"]), (2, [10, 11]))
        self.assertEqual([r["unit"] for r in ltm.service_rows(units, ps.procs, show_inactive=False)], ["sshd.service"])

    def test_startup_entries(self):
        home = os.path.join(self.tmp, "home")
        write(home + "/autostart/mine.desktop", "[Desktop Entry]\nName=Mine\nExec=mine --tray\n")
        write(home + "/autostart/shared.desktop", "[Desktop Entry]\nName=Shared\nHidden=true\n")
        write(self.tmp + "/xdg/autostart/shared.desktop", "[Desktop Entry]\nName=Shared\nExec=shared\n")
        write(self.tmp + "/xdg/autostart/gnomeonly.desktop",
              "[Desktop Entry]\nName=G\nExec=g\nOnlyShowIn=GNOME;\nX-GNOME-Autostart-enabled=false\n")
        env = {"XDG_CONFIG_HOME": home, "XDG_CONFIG_DIRS": self.tmp + "/xdg"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ltm.shutil, "which", return_value=None):
            rows = dict((r["name"], r) for r in ltm.startup_entries())
        self.assertEqual(sorted(rows), ["G", "Mine", "Shared"])
        self.assertTrue(rows["Mine"]["enabled"])
        self.assertFalse(rows["Shared"]["enabled"])            # the user's copy hides the system one
        self.assertEqual(rows["Shared"]["type"], "autostart (user)")
        self.assertFalse(rows["G"]["enabled"])
        self.assertEqual(rows["G"]["note"], "only in GNOME")

    def test_details_of_fake_process(self):
        self.make_proc(1, "systemd", ppid=0, uid=0)
        self.make_proc(77, "app", ppid=1, uid=1000, utime=100, stime=50, args=["/usr/bin/app", "-v"],
                       io=(4096, 8192), cgroup="0::/system.slice/app.service\n")
        write(self.proc + "/uptime", "1000.0 1.0\n")
        d = ltm.process_details(77)
        self.assertEqual((d["name"], d["ppid"], d["parent_name"], d["unit"]), ("app", 1, "systemd", "app.service"))
        self.assertEqual(d["cmdline"], "/usr/bin/app -v")
        self.assertEqual(d["io"]["write_bytes"], 8192)
        self.assertAlmostEqual(d["cpu_user_s"], 100.0 / ltm.CLK_TCK)
        lines = ltm.details_lines(d, 100)
        text = "\n".join("".join(seg[0] for seg in line) for line in lines)
        for needle in ("General", "app.service", "/usr/bin/app -v", "Disk I/O", "8.00 KiB"):
            self.assertIn(needle, text)
        json.dumps(ltm.details_public(d), default=str)
        self.assertIsNone(ltm.process_details(999999))


class TestSource(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 8), "ast feature_version needs Python 3.8+")
    def test_python36_grammar(self):
        """ltm promises Python 3.6+: the source must parse with the 3.6 grammar."""
        for name in ("ltm.py", "tests/tui_driver.py"):
            with open(os.path.join(ROOT, name)) as f:
                ast.parse(f.read(), name, feature_version=(3, 6))

    def test_no_third_party_imports(self):
        with open(os.path.join(ROOT, "ltm.py")) as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        allowed = {"__future__", "argparse", "collections", "json", "locale", "math", "os", "re", "shutil", "signal",
                   "socket", "struct", "subprocess", "sys", "textwrap", "threading", "time", "unicodedata", "pwd",
                   "grp", "curses", "fcntl"}
        self.assertEqual(names - allowed, set())


def run_ltm(*args, **kw):
    env = dict(os.environ, NO_COLOR="1")
    return subprocess.run([sys.executable, os.path.join(ROOT, "ltm.py")] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=kw.get("timeout", 60))


@unittest.skipUnless(LINUX, "needs a real /proc")
class TestOnThisMachine(unittest.TestCase):
    def test_summary(self):
        r = run_ltm("summary", "-i", "0.3")
        self.assertEqual(r.returncode, 0, r.stderr)
        for needle in ("CPU", "Memory", "Top CPU", "Top memory"):
            self.assertIn(needle, r.stdout)

    def test_summary_json(self):
        r = run_ltm("summary", "--json", "-i", "0.3")
        self.assertEqual(r.returncode, 0, r.stderr)
        snap = json.loads(r.stdout)
        self.assertGreater(snap["memory"]["total"], 0)
        self.assertGreaterEqual(snap["cpu"]["logical"], 1)
        self.assertEqual(len(snap["cpu"]["per_cpu_pct"]), snap["cpu"]["logical"])
        self.assertGreater(snap["processes"]["count"], 0)

    def test_top_json_sees_a_busy_child(self):
        busy = subprocess.Popen([sys.executable, "-c", "import time\nt=time.time()\nwhile time.time()-t<5: pass"])
        try:
            time.sleep(0.3)
            r = run_ltm("top", "--json", "-n", "0", "-i", "0.5")
            self.assertEqual(r.returncode, 0, r.stderr)
            procs = json.loads(r.stdout)
            mine = [p for p in procs if p["pid"] == busy.pid]
            self.assertEqual(len(mine), 1)
            self.assertGreater(mine[0]["cpu_pct_core"], 50.0)     # it spins one core
            self.assertGreater(mine[0]["memory_rss"], 0)
        finally:
            busy.kill()
            busy.wait()

    def test_top_text_tree_and_filters(self):
        r = run_ltm("top", "-t", "-n", "15", "-s", "mem", "-i", "0.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.splitlines()[0].startswith("PID"))
        r = run_ltm("top", "-f", "definitely-no-such-process-xyz", "-i", "0.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [l for l in r.stdout.splitlines()[1:] if l.strip()[:1].isdigit()]
        self.assertEqual(rows, [])          # ltm does not match its own command line

    def test_proc(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            r = run_ltm("proc", str(child.pid), "-i", "0.2")
            self.assertEqual(r.returncode, 0, r.stderr)
            for needle in ("General", "sleep 30", "Memory", "Threads"):
                self.assertIn(needle, r.stdout)
            r = run_ltm("proc", str(child.pid), "--json", "-i", "0.2")
            d = json.loads(r.stdout)
            self.assertEqual((d["pid"], d["name"]), (child.pid, "sleep"))
            self.assertIsNotNone(d["fds"])                        # our own child: readable
        finally:
            child.kill()
            child.wait()
        self.assertEqual(run_ltm("proc", "999999999").returncode, 1)

    def test_users_startup_services(self):
        r = run_ltm("users", "--json", "-i", "0.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(u["procs"] > 0 for u in json.loads(r.stdout)))
        r = run_ltm("startup", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        json.loads(r.stdout)
        r = run_ltm("services", "--json", "-i", "0.2")
        if os.path.isdir("/run/systemd/system"):
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(any(s["sub"] == "running" for s in json.loads(r.stdout)))
        else:
            self.assertEqual(r.returncode, 1)

    def test_help_and_version(self):
        self.assertEqual(run_ltm("--version").stdout.strip(), "ltm " + ltm.__version__)
        self.assertIn("ltm proc PID", run_ltm("--help").stdout)


@unittest.skipUnless(LINUX and ltm.curses is not None, "needs Linux and curses")
class TestInterface(unittest.TestCase):
    def test_every_tab_and_popup(self):
        sys.path.insert(0, HERE)
        import tui_driver
        out = tempfile.mkdtemp(prefix="ltm-screens-")
        try:
            steps = ["WAIT 1.2", "DUMP procs", "t", "DUMP tree", "a", "DUMP apps", "KEY_RIGHT", "a",
                     "\n", "WAIT 1.1", "DUMP details", "\x1b",
                     "/", "s", "y", "s", "DUMP filter", "\n", "\x1b", "M", "%", "K", "u", "u", "K", "%", "M",
                     ">", ">", "<", "r", "c",
                     "2", "WAIT 1.1", "DUMP cpu", "l", "DUMP cores", "l", "KEY_DOWN", "DUMP mem", "KEY_DOWN", "KEY_DOWN",
                     "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "KEY_END", "DUMP last",
                     "3", "DUMP users", "\n", "DUMP userprocs", "4", "WAIT 0.5", "DUMP services", "A", "5", "WAIT 1",
                     "DUMP startup", "KEY_F1", "DUMP help", "\x1b", "1", "k", "DUMP signals", "\x1b", "x", "DUMP confirm", "n",
                     "+", "-", "p", "p"]
            code = tui_driver.run(steps, 132, 40, out)
            err = os.path.join(out, "traceback.txt")
            self.assertFalse(os.path.exists(err), open(err).read() if os.path.exists(err) else "")
            self.assertEqual(code, 0)

            def screen(name):
                with open(os.path.join(out, name + ".txt")) as f:
                    return f.read()
            self.assertIn("1 Processes", screen("procs"))
            self.assertIn("PID", screen("procs"))
            self.assertIn("[tree]", screen("tree"))
            self.assertIn("[grouped by app]", screen("apps"))
            self.assertIn("General", screen("details"))
            self.assertIn("filter \"sys\"", screen("filter"))
            self.assertIn("% utilization", screen("cpu"))
            self.assertIn("Memory composition", screen("mem"))
            self.assertIn("users have processes", screen("users"))
            self.assertIn("filter \"user:", screen("userprocs"))
            self.assertIn("Help", screen("help"))
            self.assertIn("SIGKILL", screen("signals"))
            self.assertIn("SIGTERM", screen("confirm"))
            self.assertTrue("services" in screen("services") or "systemd" in screen("services"))
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_tiny_terminal(self):
        sys.path.insert(0, HERE)
        import tui_driver
        out = tempfile.mkdtemp(prefix="ltm-screens-")
        try:
            self.assertEqual(tui_driver.run(["WAIT 0.3", "DUMP tiny"], 40, 8, out), 0)
            with open(os.path.join(out, "tiny.txt")) as f:
                self.assertIn("needs at least", f.read())
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
