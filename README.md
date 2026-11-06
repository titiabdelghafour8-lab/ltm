# ltm: a task manager for the Linux terminal

`ltm` shows what your machine is doing and what each process uses: CPU, memory,
disk I/O, GPU, network adapters, temperatures, fans and battery. It has live graphs,
a process tree, per-user and per-service totals, and startup programs. It works like
the Windows Task Manager, but runs in a terminal.

It is one file. Its only requirement is Python 3.6 or newer, which every mainstream
distribution ships. There is nothing to compile and no `pip install`. It reads
`/proc` and `/sys` directly, so it works the same on Debian, Ubuntu, Fedora, RHEL,
Arch, openSUSE, Alpine, Void, Gentoo, over SSH, in WSL and in containers.

The Performance tab, captured with `tests/tui_driver.py` on Fedora 44 under WSL2
(112x30 terminal, a test load rising and falling; only the hostname was changed):

```
 ltm  myhost  —  Fedora Linux 44 (WSL)  kernel 6.18.54.1-microsoft-standard-W…  up 00:00:42   11:18:05
 1 Processes   2 Performance   3 Users   4 Services   5 Startup                                 update every 1s
 CPU  11%   Memory 621M/7.6G  swap 0%   Disk R 0B/s W 0B/s   Net ↓0 bps ↑0 bps   GPU 0%
 CPU            11%  2.42 GHz │ CPU                                                Intel(R) Core(TM) i7-14650HX
 ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▂▂▂▂▃▃▃▃▃▂▂▂▁▁ │ ┌─ % utilization ────────────────────────────────────────────────────── 100% ─┐
 Memory        621M/7.6G (8%) │ │                                                                             │
 ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ │ │                                                                             │
 Disk sda             HDD  0% │ │                                                                             │
 ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ │ │                                       ▂▃▄▅▅▅▅▅▃▁               ▁▃▄▅▅▆▅▅▃▁   │
 Disk sdb             HDD  0% │ │                                     ▇███████████▇▆▄▃▃▁▁▁ ▁▂▃▄▆████████████▆▄│
 ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ │ └─ 77 seconds ──────────────────────────────────────────────────────────── 0 ─┘
 Disk sdc             HDD  0% │
 ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ │   0 ██░░░░░░░░░░░░░░  12%   8 ███████░░░░░░░░░  47%  16 ░░░░░░░░░░░░░░░░   1%
 Disk sdd             HDD  0% │   1 ░░░░░░░░░░░░░░░░   0%   9 ░░░░░░░░░░░░░░░░   0%  17 ░░░░░░░░░░░░░░░░   0%
 ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ │   2 ░░░░░░░░░░░░░░░░   0%  10 ░░░░░░░░░░░░░░░░   0%  18 ░░░░░░░░░░░░░░░░   0%
 Ethernet eth0 ↑ 0 bps  ↓ 0 … │   3 ░░░░░░░░░░░░░░░░   0%  11 ████████░░░░░░░░  53%  19 ░░░░░░░░░░░░░░░░   2%
 █▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁█▁▁▁▁▁▁▁▁▁▁ │   4 ████████████████  99%  12 ░░░░░░░░░░░░░░░░   0%  20 ░░░░░░░░░░░░░░░░   0%
 GPU 0 NVIDIA GeForce RTX 40… │   5 ░░░░░░░░░░░░░░░░   0%  13 ░░░░░░░░░░░░░░░░   0%  21 ░░░░░░░░░░░░░░░░   0%
 ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ │   6 ░░░░░░░░░░░░░░░░   1%  14 ████████░░░░░░░░  47%  22 ░░░░░░░░░░░░░░░░   0%
 Sensors           2 readings │   7 ░░░░░░░░░░░░░░░░   0%  15 ░░░░░░░░░░░░░░░░   0%  23 ░░░░░░░░░░░░░░░░   0%
                              │
 Battery BAT1 60% not chargi… │ Utilization         11.0%              Load average        2.25  0.58  0.20
 ▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅ │ Speed               2.42 GHz           User / system       10.3% / 0.7%
                              │ Topology            1 socket, 12 cores I/O wait / steal    0.0% / 0.0%
                              │ Logical processors  24                 Context switches/s  2152
                              │ Processes           32                 Interrupts/s        2131
```

## Install

```bash
git clone <this repository> ltm && cd ltm
sudo ./install.sh        # puts it in /usr/local/bin/ltm for everyone
```

Use `./install.sh` without `sudo` to install it just for you, in `~/.local/bin/ltm`.
You can also skip installing and run `python3 ltm.py`, or copy `ltm.py` to any
machine. `./install.sh --uninstall` removes it.

If Python is missing: `sudo apt install python3` (Debian, Ubuntu), `sudo dnf install python3`
(Fedora, RHEL), `sudo pacman -S python` (Arch), `sudo apk add python3` (Alpine).
openSUSE ships curses separately, so also install `python3-curses`. Without curses,
every command except the interactive view still works.

## Use it

```bash
ltm                      # the interactive task manager
sudo ltm                 # the same, with disk I/O and open files of every user's processes
ltm summary              # one-shot overview: CPU, memory, disks, filesystems, network, GPU, sensors, battery
ltm top                  # processes by CPU;  -s mem|disk|gpu|time|pid|name, -n 20, -u alice, -f firefox, -t (tree)
ltm proc 1234            # everything about one process (also: ltm proc firefox)
ltm users                # totals per user
ltm services             # systemd services with their CPU and memory (-a: inactive ones too)
ltm startup              # what starts at login (.desktop autostart) and at boot (enabled services)
ltm top -n 5 --json      # every command has --json for scripts
```

Options for the interactive view: `--tab performance`, `--sort mem`, `--filter nginx`,
`--user www-data`, `--tree`, `-i 2` (seconds between updates), `--no-color`, `--ascii`.

### The five tabs

| Tab | What it shows |
|---|---|
| **Processes** | Every process with CPU%, memory, disk read+write per second, GPU%, threads, priority, CPU time and command line. Filter it, sort it, view it as a tree, or group it by application (all Chrome processes on one line, added together). Enter opens a live detail window: memory breakdown (RSS, PSS, USS, swap), open files, network connections, threads with their CPU%, limits, cgroup, service, container. |
| **Performance** | Task-Manager-style graphs, with a sidebar of sparklines. **CPU**: utilization, speed, per-core bars or one graph per logical processor (`l`), load, context switches, caches, temperature. **Memory**: in use, composition (in use / modified / cache / free), committed, swap. **Each disk**: active time, transfer rate, response time, IOPS, and the filesystems on it. **Each network adapter**: send and receive graphs, addresses, link speed, Wi-Fi signal. **Each GPU**: utilization, video memory, temperature, power, the processes using it. **Sensors**: every temperature, fan, voltage and power reading. **Battery**: charge, time left, power draw, health. |
| **Users** | Processes, threads, CPU, memory and disk use per user. Enter shows that user's processes. |
| **Services** | systemd services with state, process count, CPU, memory and disk I/O. Enter shows `systemctl status`. `S` / `T` / `R` start, stop and restart a service after asking. |
| **Startup** | XDG autostart entries (enabled or disabled) and services enabled at boot, for the system and for your user. |

### Keys

| Key | Action |
|---|---|
| `1`-`5`, `Tab` | switch tab |
| `↑` `↓` `PgUp` `PgDn` `Home` `End`, mouse wheel | move |
| `Enter`, double-click | details (process), status (service), show (startup entry) |
| `/`, `Esc` | filter, clear the filter. `user:root` matches a user, anything else a name, command, PID, service or container |
| `c` `m` `d` `g`, `<` `>`, `r`, click a header | sort by CPU, memory, disk, GPU; previous / next column; reverse |
| `t` / `a` | tree view / group by application. `Space`, `←`, `→` collapse and expand |
| `x` or `Delete` | end task (SIGTERM), after asking |
| `k` | send a signal: KILL, HUP, INT, STOP, CONT, USR1, USR2 |
| `n` | change priority (nice). Making it higher needs root |
| `K` / `u` | show kernel threads / only my processes |
| `M` / `%` | memory column private ↔ RSS / CPU% of the machine ↔ of one core |
| `l` | Performance tab: one CPU graph per logical processor |
| `p`, `+` `-`, `F5` | pause, slower / faster updates, update now |
| `F1` or `?`, `q` | help, quit |

## What the numbers mean

- **CPU%** is a share of the whole machine, as in Windows: on 8 cores, a process
  keeping one core busy shows 12.5%. Press `%` to see it per core instead, as `top`
  does (100% = one core).
- **MEMORY** is private memory: resident pages the process does not share with
  others (RSS minus file-backed and shared pages). This is close to Task Manager's
  "private working set" and does not count shared libraries many times over. Press
  `M` for RSS. The detail window also shows PSS and USS from `smaps_rollup`.
- **DISK/s** is bytes read from and written to storage per second, from
  `/proc/PID/io`. The kernel only lets you read it for your own processes, so run
  `sudo ltm` to see everyone's. Some kernels (WSL2, some embedded builds) do not
  collect it at all, and ltm says so.
- **GPU%** is the busiest engine of the GPU a process uses. It comes from the
  kernel's DRM fdinfo, which Intel (i915, xe), AMD (amdgpu), Qualcomm, Arm Mali
  (panfrost, panthor) and others provide since Linux 5.19. NVIDIA cards are read
  through `nvidia-smi` when it is installed.
- **Network** speeds are in bits per second (Kbps, Mbps), like Task Manager. Disk
  speeds are in bytes per second.

## Limitations

- **No per-process network bandwidth.** Linux does not expose it in `/proc`; tools
  that show it capture packets or use eBPF, which needs root and extra libraries.
  The detail window lists each process's TCP/UDP connections instead.
- **No NVIDIA per-process utilization.** On the proprietary driver, `nvidia-smi`
  only reports video memory per process (for compute apps).
- **The Services tab needs systemd.** On OpenRC, runit or s6 (Alpine, Void, Gentoo
  with OpenRC) it shows a notice. Everything else works.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

There are 41 tests, using the standard library only:
- The parsers and samplers run against fake `/proc` and `/sys` trees, so the CPU%,
  I/O rates, disk active time, battery time left and so on are checked exactly. That
  includes PID reuse, `exec()`, kernel threads, missing I/O accounting and
  permission errors.
- A check that the source parses with the Python 3.6 grammar and imports nothing
  outside the standard library.
- The commands run on the real machine, including `top --json` finding a busy child
  process at ~100% of one core.
- `tests/tui_driver.py` runs the real curses interface in a pseudo-terminal with
  scripted keys and dumps the screen text, so every tab and popup is exercised
  without a person at the keyboard.

You can use the driver to look at the interface yourself:

```bash
python3 tests/tui_driver.py --size 120x40 --out /tmp/screens "WAIT 2" "DUMP procs" 2 "WAIT 1" "DUMP perf"
cat /tmp/screens/perf.txt
```

Measured on Fedora 44 (WSL2, 24 threads): one update of everything takes 8.7 ms with
650 processes. The interface itself uses about 0.1% of one core while idle.
