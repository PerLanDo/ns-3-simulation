# Dummy campus data

Every number in this folder is a **placeholder**. Nothing here was measured. The files exist so
the simulation pipeline can be built, debugged, and demonstrated before the site survey is done.

Replace them with real MSU-IIT measurements before any result goes into the thesis.

| File | Purpose | Consumed by |
|---|---|---|
| `zones.csv` | One row per zone: radius, environment, path-loss exponent, reference loss, fading | `tools/run_sweeps.py` |
| `sta_counts.csv` | Client counts per zone and time period, including the 10/30/60/100 sweep ladder | `tools/run_sweeps.py` |
| `traffic_mix.csv` | Application mix, TOS/access-category mapping, per-class rates and QoS budgets | `tools/run_sweeps.py` |
| `aps.csv` | AP inventory: positions, bands, channels, transmit power per radio | Reference for the thesis methodology chapter |
| `qos_baseline_placeholders.csv` | Stand-in "measured" QoS used as a calibration target | Dashboard comparison view |

`zones.csv` and `aps.csv` overlap on purpose. The simulation needs exactly one propagation
environment per zone, which is what `zones.csv` provides and what `run_sweeps.py` turns into
`--pathLossExponent`, `--referenceLoss`, `--radius` and `--propagation` flags. `aps.csv` is the
richer per-radio inventory you will fill in from the controller export; it documents the real
deployment but is not read by the simulation. If you change a propagation value, change it in
`zones.csv` or the simulation will not see it.

One simplification worth stating in the thesis: `zones.csv` carries both `reference_loss_db`
(2.4 GHz) and `reference_loss_5g_db`, but ns-3's log-distance model takes a single reference loss
per channel object, so the 2.4 GHz value is used for both bands. This slightly flatters 5 GHz
range. Use `--propagation=friis` as a frequency-aware sensitivity check.

## Coordinate system

A flat plane in metres with the origin at an arbitrary campus corner. Only *relative* distances
matter to the simulation, so you can re-anchor the origin freely as long as you keep the zones
consistent with each other.

```
  y
  ^
  |            (60,110) Campus Lawn
  |
  |                       (90,90) College Building
  |
  |  (30,40) Library   (90,40) CCS Hub   (150,40) Gymnasium
  +-------------------------------------------------------> x
```

## How to replace with real data

1. **AP inventory.** Get the actual controller export: model, radios, channel, width, transmit
   power. Update `aps.csv`. Where an AP is dual-band, keep the two rows co-located at the same
   coordinates as done here.
2. **Client counts.** Pull peak association counts per AP from the controller over one or two
   weeks. Update `sta_counts.csv`, keeping the `offpeak` / `moderate` / `peak` labels.
3. **Traffic mix.** Estimate from controller application visibility or a survey of what students
   actually use. Update `share` values in `traffic_mix.csv`; they should sum to 1.0.
4. **Baseline QoS.** Run speed and latency tests in each zone at peak and off-peak. Update
   `qos_baseline_placeholders.csv` and rename it to `qos_baseline_measured.csv`.
5. **Calibrate.** Adjust `path_loss_exponent` and `reference_loss_db` until the simulated baseline
   matches the measured baseline. Document the final values — that calibration step is what makes
   scenarios 2 and 3 defensible.

## Propagation defaults used here

| Environment | Exponent | Rationale |
|---|---|---|
| Free space reference | 2.0 | Theoretical lower bound |
| Campus Lawn | 2.2 | Outdoor, near line of sight |
| Gymnasium | 2.6 | Large open hall, high ceiling |
| CCS Study Hub | 3.0 | Typical indoor office/lab partitioning |
| Library | 3.2 | Shelving and concrete walls |
| College Building | 3.4 | Corridors and multiple interior walls |

Reference loss at 1 m is 40 dB for 2.4 GHz and 46.68 dB for 5 GHz, matching the values ns-3 uses
in its own Wi-Fi examples.
