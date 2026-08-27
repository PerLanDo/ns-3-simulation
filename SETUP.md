# NS-3 Setup Guide — MSU-IIT Campus Wi-Fi Thesis

Companion to the *NS-3 Simulation Guide & Thesis Implementation Roadmap*.
Target: model high-density campus Wi-Fi, measure QoS (throughput, latency, jitter, packet loss),
and compare optimization strategies offline without touching live campus infrastructure.

Simulator version in this repo: **ns-3.48** (`ns-3.48/`).

---

## 0. Status of this machine

The environment is **fully installed and verified**. Nothing in this section needs redoing; it is
recorded so the setup is reproducible and so the thesis can state the exact platform.

| Component | Version |
| --- | --- |
| WSL | 2.7.12, kernel 6.18.33.2-2 |
| Distribution | Ubuntu 24.04.4 LTS (Noble Numbat), WSL version 2 |
| Compiler | g++ 13.3.0 |
| Build tools | CMake 3.28.3, Ninja 1.11.1, ccache 4.9.1 |
| Python | 3.12.3 |
| Linux user | `msuiit` (default login, passwordless `sudo`) |

The commands that produced it, for the record:

```powershell
wsl --install -d Ubuntu-24.04 --no-launch
```

`--no-launch` skips the interactive first-run account prompt, so the user was created
non-interactively instead:

```bash
useradd -m -s /bin/bash -G sudo msuiit
printf 'msuiit ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-msuiit
printf '[user]\ndefault=msuiit\n\n[boot]\nsystemd=true\n' > /etc/wsl.conf
```

Passwordless `sudo` is safe here specifically because Windows can already reach root in any WSL
distribution with `wsl -u root`, so it grants nothing that was not already available locally. Set a
password with `passwd` if you prefer the normal prompt.

### Resource limits

This laptop has ~7.4 GB RAM, which WSL halves by default. That is not enough headroom for ns-3's
link steps, so `C:\Users\Asus\.wslconfig` pins the budget:

```ini
[wsl2]
memory=4GB
swap=4GB
processors=16
```

Run `wsl --shutdown` after editing that file for it to take effect. Build with `-j 6` rather than
the full 16 cores; 16 concurrent compiles will exhaust 4 GB and get the build OOM-killed.

Verify at any time:

```powershell
wsl --list --verbose      # Ubuntu-24.04 should show VERSION=2
```

---

## 1. Week 1 — Environment and core basics

### 1.1 Install the build prerequisites

Open Ubuntu (Start menu → Ubuntu) and run:

```bash
sudo apt update
sudo apt install -y \
  g++ python3 python3-pip cmake ninja-build ccache git \
  libsqlite3-dev sqlite3 libxml2-dev libgsl-dev gsl-bin \
  libeigen3-dev tcpdump
```

ns-3.48 requires g++ >= 11.1, Python >= 3.10, CMake >= 3.25. Ubuntu 24.04 LTS satisfies all three.
Check with:

```bash
g++ --version && python3 -V && cmake --version && ninja --version
```

### 1.2 Copy ns-3 into the Linux filesystem

**Do not build from `/mnt/c/...`.** Compiling across the Windows filesystem boundary is many
times slower and can fail on file locking. Copy the tree into your Linux home directory:

```bash
mkdir -p ~/thesis
cp -a "/mnt/c/Users/Asus/Desktop/SCHOOL FILES/THESIS/NS-3 SIMULATION/ns-3.48" ~/thesis/
cd ~/thesis/ns-3.48
```

Also copy the thesis tooling that lives beside `ns-3.48/` in this repo:

```bash
cp -a "/mnt/c/Users/Asus/Desktop/SCHOOL FILES/THESIS/NS-3 SIMULATION/data" ~/thesis/
cp -a "/mnt/c/Users/Asus/Desktop/SCHOOL FILES/THESIS/NS-3 SIMULATION/tools" ~/thesis/
cp -a "/mnt/c/Users/Asus/Desktop/SCHOOL FILES/THESIS/NS-3 SIMULATION/dashboard" ~/thesis/
cp "/mnt/c/Users/Asus/Desktop/SCHOOL FILES/THESIS/NS-3 SIMULATION/requirements.txt" ~/thesis/
```

Resulting layout:

```
~/thesis/
├── ns-3.48/
│   └── scratch/campus-wifi-msuiit.cc
├── data/dummy/*.csv
├── tools/run_sweeps.py
├── dashboard/app.py
└── requirements.txt
```

### 1.3 Configure and build

This is the configuration actually used, and it deliberately differs from the stock advice in two
ways:

```bash
cd ~/thesis/ns-3.48
./ns3 configure --enable-examples --build-profile=default \
  --enable-modules='wifi;internet;applications;point-to-point;csma;flow-monitor;mobility;propagation;spectrum;stats;traffic-control;config-store;buildings;energy;internet-apps;bridge;antenna;network;core'
./ns3 build -j 6
```

**Why the module list.** Building all of ns-3 pulls in LTE, mesh, UAN, zigbee and more, none of
which this thesis uses. Restricting to these 19 modules cut the build to **1.7 GB and ~19 minutes**
on this machine. Add a module name to the list and reconfigure if you later need one.

**Why `-j 6`.** See the memory note in section 0.

**Tests are off.** `--enable-tests` builds ns-3's own regression suite, which validates the
simulator rather than the thesis model, and costs significant time and disk. Add it if you want to
confirm your ns-3 build is sound: `./ns3 configure ... --enable-tests && ./test.py`.

### Build profiles: pick one per task

| Profile | Asserts / `NS_LOG` | Use for |
| --- | --- | --- |
| `default` | on | Weeks 1–2 learning, debugging a new scenario |
| `optimized` | **off** | Week 3–4 density sweeps |

The difference is not cosmetic. In the `default` profile a single 10-second `ax` run with only 10
stations took **210 s**, because OFDMA scheduling is expensive with asserts and logging compiled
in. A full sweep at that speed is impractical, so switch before sweeping:

```bash
./ns3 configure --enable-examples --build-profile=optimized --enable-modules='...same list...'
./ns3 build -j 6
```

Switch back with `--build-profile=default` when you need `NS_LOG` output. Each switch triggers a
full rebuild, so avoid flip-flopping mid-session.

### 1.4 Run the tutorial scripts

```bash
./ns3 run first          # two nodes, point-to-point, UDP echo
./ns3 run third          # Wi-Fi STAs + AP + point-to-point + CSMA LAN
./ns3 run "third --tracing=1"
tcpdump -r third-0-0.pcap -nn -tt | head
```

`third.cc` is the closest analog to a Packet Tracer lab: one AP, several wireless clients,
and a wired segment behind a router.

### 1.5 Edit from Cursor, build in Linux

Install the **WSL** extension, then either use the Remote-WSL command
(`Ctrl+Shift+P` → "WSL: Connect to WSL") or open the folder path
`\\wsl$\Ubuntu\home\<your-username>/thesis/ns-3.48`. Edits happen in the editor; compilation and
execution happen inside Linux.

---

## 2. Week 2 — 802.11 wireless architecture

Study these stock examples before touching the campus script. Each one isolates a variable you
will later justify in the thesis methodology chapter.

| Example | Command | What it teaches |
|---|---|---|
| `wifi-simple-infra.cc` | `./ns3 run "wifi-simple-infra --rss=-80 --numPackets=20"` | Reception cliff: retry at `-81`, `-82`, `-90` and watch packets stop arriving |
| `wifi-ap.cc` | `./ns3 run wifi-ap` | Association, beacons, `ApWifiMac` vs `StaWifiMac` |
| `wifi-rate-adaptation-distance.cc` | `./ns3 run "wifi-rate-adaptation-distance --staManager=ns3::MinstrelHtWifiManager"` | Rate adaptation vs distance |
| same | `./ns3 run "wifi-rate-adaptation-distance --staManager=ns3::IdealWifiManager"` | Upper bound for comparison |
| `wifi-multi-tos.cc` | `./ns3 run "wifi-multi-tos --nWifi=8"` | Four access categories (BE/BK/VI/VO) sharing one radio |
| `wifi-he-network.cc` | `./ns3 run "wifi-he-network --frequency=5 --nStations=8 --dlAckType=AGGR-MU-BAR"` | 802.11ax with DL OFDMA |

### 2.1 The knobs that matter for the thesis

**Band and channel width** are expressed as a single `ChannelSettings` string,
`{channel, width, band, primary20}`:

| Intent | String |
|---|---|
| 2.4 GHz, 20 MHz, channel 1 | `{1, 20, BAND_2_4GHZ, 0}` |
| 2.4 GHz, 40 MHz | `{0, 40, BAND_2_4GHZ, 0}` |
| 5 GHz, 80 MHz, lower block | `{42, 80, BAND_5GHZ, 0}` |
| 5 GHz, 80 MHz, upper block | `{106, 80, BAND_5GHZ, 0}` |

Channel `0` means "let ns-3 pick the first valid channel for that width". Valid 80 MHz centre
channels in 5 GHz are 42, 58, 106, 122, 138, 155, 171 — non-overlapping, which is why the
multi-AP scenario uses 42 and 106.

**Rate adaptation:** `IdealWifiManager` picks the best MCS from perfect SNR knowledge, so results
are deterministic and reproducible — good for controlled A/B comparison.
`MinstrelHtWifiManager` probes the channel like real hardware, so it is more realistic but noisier.
Report which one you used; the campus script defaults to `ideal`.

**Propagation:** `LogDistance` (indoor, exponent ~3.0), `Friis` (free space, frequency aware,
reasonable for Campus Lawn), and `Nakagami` (fast fading layered on top of a distance model).

Record every flag you change; those become your methodology table.

---

## 3. Week 3 — High-density contention and the three scenarios

The campus model lives in [`ns-3.48/scratch/campus-wifi-msuiit.cc`](ns-3.48/scratch/campus-wifi-msuiit.cc).
Anything in `scratch/` is compiled automatically by `./ns3 build` — no CMake edits needed.

### 3.1 Three-scenario comparative framework

| Scenario | Flag | Configuration | Expected outcome |
|---|---|---|---|
| 1. Baseline | `--scenario=baseline` | Single AP, 802.11n, 2.4 GHz, 20 MHz | Heavy contention, latency > 150 ms, throughput collapse |
| 2. RF tuning | `--scenario=rftuning` | Dual-band AP: 2.4 GHz 20 MHz + 5 GHz 80 MHz, 75% of clients offloaded to 5 GHz | Contention drops, latency toward < 40 ms |
| 3. Next-gen | `--scenario=ax` | Two 802.11ax APs, 5 GHz 80 MHz, DL/UL OFDMA, load split across channels 42 and 106 | Stable jitter, near-zero loss |

### 3.2 Running

```bash
cd ~/thesis/ns-3.48

# smoke test: small and fast
./ns3 run "campus-wifi-msuiit --scenario=baseline --nSta=10 --simTime=10s"

# the density ladder from the roadmap
for n in 10 30 60 100; do
  ./ns3 run "campus-wifi-msuiit --scenario=baseline --nSta=$n --simTime=20s"
done
```

Useful flags:

| Flag | Default | Meaning |
|---|---|---|
| `--scenario` | `baseline` | `baseline`, `rftuning`, `ax` |
| `--nSta` | `30` | Station count (the roadmap ladder is 10/30/60/100) |
| `--zone` | `library` | `library`, `ccs_hub`, `gym`, `lawn`, `college` — sets radius and path-loss defaults |
| `--simTime` | `20s` | Simulated duration |
| `--direction` | `both` | `downlink`, `uplink`, `both` |
| `--rateManager` | `ideal` | `ideal` or `minstrel` |
| `--propagation` | `logdistance` | `logdistance`, `friis`, `nakagami` |
| `--fiveToTwoRatio` | `0.75` | Fraction of clients on 5 GHz in `rftuning` |
| `--apSpacing` | `30.0` | Metres between the two APs in `ax` |
| `--uplinkRatio` | `0.2` | Uplink load as a fraction of each client's downlink rate |
| `--browsingShare` / `--videoShare` | `0.6` / `0.3` | Application mix; the remainder is VoIP |
| `--useRts` | `false` | RTS/CTS for hidden-node mitigation |
| `--seed` / `--run` | `1` / `1` | RNG control; vary `--run` for repeated trials |
| `--outDir` | `results` | Where CSV/XML land |
| `--pcap` | `false` | Write per-device PCAP (large files) |

The per-class offered load and marking are also overridable, which is how `run_sweeps.py` feeds
`traffic_mix.csv` into the model without a rebuild: `--browsingRateKbps`, `--videoRateKbps`,
`--voipRateKbps`, `--browsingBytes`, `--videoBytes`, `--voipBytes`, `--browsingTos`,
`--videoTos`, `--voipTos`. Run `./ns3 run "campus-wifi-msuiit --PrintHelp"` for the full list.

### 3.3 Output

Each run writes into `--outDir` (default `~/thesis/ns-3.48/results/`):

- `<tag>-flowmon.xml` — full FlowMonitor record
- `<tag>-flows.csv` — one row per flow, tagged with its traffic class and direction
- `summary.csv` — appended rows, the file the analysis tools read

`<tag>` encodes scenario, zone, station count and run number, so repeated runs never overwrite
each other. Because runs append to `summary.csv`, a whole sweep accumulates into one tidy table.

Each run appends **four rows** to `summary.csv`, distinguished by the `traffic_class` column:
one each for `browsing`, `video` and `voip`, plus an `all` row covering the whole run. This is
what lets you show that voice survives while bulk traffic collapses — the single most useful
result for the QoS-to-QoE argument. Filter on `traffic_class == "all"` for headline numbers.

---

## 4. Week 4 — Python wrapper and dashboard

Install the Python dependencies inside WSL:

```bash
cd ~/thesis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The virtualenv already exists at `~/thesis/.venv`. Either activate it or call it directly, as the
examples below do with `./.venv/bin/python`. Ubuntu 24.04 refuses `pip install` outside a venv, so
this step is not optional there.

Four pieces, sharing one QoS/QoE module (`tools/qos_metrics.py`):

| Script | Role | Runs on |
|---|---|---|
| `tools/run_sweeps.py` | Reads `data/dummy/*.csv`, turns each zone into command-line flags, calls `./ns3 run` | WSL (stdlib only) |
| `tools/analyze.py` | Reads `summary.csv`, prints the comparison tables, writes CSV and PNG figures | either |
| `dashboard/app.py` | Interactive Streamlit view of the same data | either |
| `tools/check_scratch.py` | Static sanity check of the C++ scratch script before a slow rebuild | either |

`run_sweeps.py` must run inside WSL because it invokes `./ns3`. The other three work from Windows
or WSL; the analysis reads the same `summary.csv` through `\\wsl.localhost` either way.

### 4.1 Sweep runner

`--ns3-dir` is required. The zone's propagation values come from `zones.csv` and the application
mix from `traffic_mix.csv`, so the dummy dataset can be edited without recompiling.

```bash
# preview the commands without running anything
python3 tools/run_sweeps.py --ns3-dir ~/thesis/ns-3.48 --dry-run

# a quick check first: one scenario, smallest load
python3 tools/run_sweeps.py --ns3-dir ~/thesis/ns-3.48 --zone library \
    --scenarios baseline --sta-counts 10 --runs 1 --sim-time 5s

# the full roadmap experiment: 3 scenarios x the zone ladder x 3 repetitions
python3 tools/run_sweeps.py --ns3-dir ~/thesis/ns-3.48 --zone library --runs 3 --sim-time 20s
```

With no `--sta-counts`, it uses the rows flagged `sweep_ladder=yes` for that zone in
`sta_counts.csv` (10/30/60/100 for the Library and CCS Study Hub).

#### Budget the sweep before starting it

Measured on this laptop, 10 s of simulated time at N=10:

| Scenario | `default` profile | `optimized` profile |
| --- | --- | --- |
| `baseline` | ~35 s | ~13 s |
| `ax` | **210 s** | **98 s** |

`ax` is far more expensive because OFDMA schedules every transmission opportunity, and cost grows
quickly with station count — N=100 is nowhere near ten times N=10. The full roadmap grid
(3 scenarios x 5 zones x 4 densities x 3 runs = 180 runs) is an overnight job at best, so start
narrow and widen deliberately:

```bash
# pilot: one zone, three densities, all scenarios, single run
python3 tools/run_sweeps.py --ns3-dir ~/thesis/ns-3.48 --zone library \
    --sta-counts 10 30 60 --runs 1 --sim-time 10s
```

Because results are appended, a sweep can be stopped with `Ctrl+C` and resumed later without
losing completed runs. Use `--runs 3` only for the configurations that end up in the thesis, since
error bars need replicates but exploration does not.

### 4.2 Analysis and figures

```bash
cd ~/thesis
./.venv/bin/python tools/analyze.py --summary ~/thesis/ns-3.48/results/summary.csv \
    --export ~/thesis/ns-3.48/results/analysis.csv \
    --plots ~/thesis/ns-3.48/results/figures
```

This prints aggregate QoS per scenario and density, checks each scenario against its roadmap
target (baseline latency above 150 ms, RF tuning below 40 ms, 802.11ax stable), breaks results
down per traffic class, and writes `qos-<zone>.png` and `voip-mos.png` for the thesis document.

### 4.3 Dashboard

Run it from **Windows** (the Python packages are installed there):

```powershell
cd "C:\Users\Asus\Desktop\SCHOOL FILES\THESIS\NS-3 SIMULATION"
python -m streamlit run dashboard/app.py
```

Then open <http://localhost:8501>. Simulations write inside WSL, so the summary path defaults to

```
\\wsl.localhost\Ubuntu-24.04\home\msuiit\thesis\ns-3.48\results\summary.csv
```

which the app resolves automatically; the sidebar box overrides it. It plots throughput, latency,
jitter, loss and MOS against station count per scenario, with a traffic-class selector so you can
isolate VoIP.

To check the dashboard without opening a browser:

```powershell
python tools/test_dashboard.py
```

### 4.4 Duplicate runs

`summary.csv` is append-only, which is what makes a long sweep safe to interrupt. The trade-off is
that re-running an identical configuration leaves both copies in the file. The loader drops the
stale ones automatically — matching on every parameter *including* `run`, so genuine replicates
(`--runs 3`) are always kept — and reports how many it ignored. To start from a clean slate:

```bash
rm -rf ~/thesis/ns-3.48/results
```

The MOS calculation is a simplified ITU-T G.107 E-model. Treat it as provisional until you
replace it with the correlational model your thesis defines — the formula is isolated in one
function, `estimate_mos` in `tools/qos_metrics.py`, precisely so it is easy to swap.

---

## 5. Calibrating dummy data with real measurements

Everything in `data/dummy/` is a **placeholder**. The roadmap's method is to start from real
baseline measurements in the high-density zones and map them into ns-3 parameters.

| Zone | What to measure on site | Which parameter it feeds |
|---|---|---|
| Main Library | AP positions, client counts at peak | `--nSta`, AP coordinates |
| CCS Study Hub | Throughput and latency per client | Validation target |
| University Gymnasium | Room dimensions, AP height | Radius, path-loss exponent |
| Campus Lawn | Outdoor distance to nearest AP | `--propagation=friis`, radius |
| College building | Wall material, floors | Path-loss exponent, reference loss |

Workflow: run the baseline scenario with dummy values, compare the simulated latency and
throughput against your survey, then adjust `path_loss_exponent` and `reference_loss_db` in
`data/dummy/zones.csv` and the client counts in `sta_counts.csv` until the baseline reproduces
observed conditions. Only then do scenarios 2 and 3 carry any weight — their credibility rests
entirely on a calibrated baseline.

Edit the CSVs rather than the C++ defaults: `run_sweeps.py` passes them through as command-line
flags, so recalibrating never requires a rebuild.

---

## 6. Troubleshooting

**`wsl --install` says WSL is not installed.** You ran it without administrator rights. Open
PowerShell via right-click → "Run as administrator".

**WSL2 will not start, virtualization not enabled.** Restart Windows. If it persists after a
reboot, enable SVM Mode (AMD) or VT-x (Intel) in the BIOS/UEFI.

**Build fails with a CMake version error.** Ubuntu 22.04 ships CMake 3.22 but ns-3.48 needs 3.25+.
Either use Ubuntu 24.04 or `pip3 install cmake==3.25.2`.

**`./ns3 run campus-wifi-msuiit` reports the program was not found.** Run `./ns3 build` first;
scratch programs are only registered at build time.

**Simulation is extremely slow at `--nSta=100`.** Expected. Use the optimized build profile, keep
`--simTime` at 10–20 s, and disable `--pcap`.

**Throughput is reported as zero.** The stations never associated. Reduce the zone radius or raise
`--txPower` so the stations are inside the coverage area.

---

## 7. Reference documentation

- ns-3 tutorial: https://www.nsnam.org/docs/tutorial/html/
- Wi-Fi model design: https://www.nsnam.org/docs/models/html/wifi-design.html
- FlowMonitor: https://www.nsnam.org/docs/models/html/flow-monitor.html
- ns-3.48 Wi-Fi model: https://www.nsnam.org/docs/release/3.48/models/html/wifi.html
- Local installation notes: [`ns-3.48/doc/installation/source/quick-start.rst`](ns-3.48/doc/installation/source/quick-start.rst)
