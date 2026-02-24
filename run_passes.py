import os
import sys
import yaml
import pandas as pd
import matplotlib.pyplot as plt

from datetime import datetime, timezone
from skyfield.api import EarthSatellite, load, wgs84

def parse_utc(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)

def mb_from_mbps(mbps: float, seconds: float, efficiency: float) -> float:
    return (mbps * 1e6 / 8.0) * seconds * efficiency / 1e6

def ensure_outputs_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path

def make_satellite(sat_cfg: dict, ts):
    # Preferred: fetch TLE from URL (avoids spacing/column issues)
    if "tle_url" in sat_cfg:
        sats = load.tle_file(sat_cfg["tle_url"])
        if not sats:
            raise RuntimeError("No satellites returned from tle_url")
        if len(sats) == 1:
            sat = sats[0]
            sat.name = sat_cfg.get("name", sat.name)
            return sat

        target = sat_cfg.get("name", "").strip()
        for s in sats:
            if s.name.strip() == target:
                return s
        return sats[0]

    # Fallback: inline TLE
    return EarthSatellite(sat_cfg["tle1"], sat_cfg["tle2"], sat_cfg["name"], ts)

def compute_passes(cfg: dict) -> pd.DataFrame:
    ts = load.timescale()

    t0_dt = parse_utc(cfg["time"]["start_utc"])
    t1_dt = parse_utc(cfg["time"]["end_utc"])
    t0 = ts.from_datetime(t0_dt)
    t1 = ts.from_datetime(t1_dt)

    downlink_mbps = float(cfg["link"]["downlink_mbps"])
    efficiency = float(cfg["link"]["efficiency"])
    min_dur_s = float(cfg.get("analysis", {}).get("min_pass_duration_s", 0))

    rows = []

    for sat_cfg in cfg["satellites"]:
        sat = make_satellite(sat_cfg, ts)

        for gs in cfg["ground_stations"]:
            station = wgs84.latlon(gs["lat_deg"], gs["lon_deg"], elevation_m=gs["alt_m"])
            mask = float(gs.get("mask_deg", 0.0))

            # event codes: 0 = rise, 1 = culmination, 2 = set
            times, events = sat.find_events(station, t0, t1, altitude_degrees=mask)

            i = 0
            while i < len(events) - 2:
                if events[i] == 0 and events[i + 1] == 1 and events[i + 2] == 2:
                    t_rise = times[i]
                    t_culm = times[i + 1]
                    t_set = times[i + 2]

                    dur_s = (t_set.utc_datetime() - t_rise.utc_datetime()).total_seconds()

                    # Filter short passes
                    if dur_s < min_dur_s:
                        i += 3
                        continue

                    alt, az, dist = (sat.at(t_culm) - station.at(t_culm)).altaz()
                    max_elev_deg = float(alt.degrees)

                    data_mb = mb_from_mbps(downlink_mbps, dur_s, efficiency)

                    rows.append({
                        "satellite": getattr(sat, "name", sat_cfg.get("name", "sat")),
                        "station": gs["name"],
                        "mask_deg": mask,
                        "aos_utc": t_rise.utc_iso(),
                        "los_utc": t_set.utc_iso(),
                        "duration_s": round(dur_s, 1),
                        "max_elev_deg": round(max_elev_deg, 2),
                        "downlink_mbps": downlink_mbps,
                        "efficiency": efficiency,
                        "data_mb_est": round(data_mb, 2),
                    })
                    i += 3
                else:
                    i += 1

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["aos_dt"] = pd.to_datetime(df["aos_utc"], utc=True)
    df["date_utc"] = df["aos_dt"].dt.date
    return df

def make_outputs(df: pd.DataFrame, outdir: str) -> None:
    outdir = ensure_outputs_dir(outdir)

    passes_path = os.path.join(outdir, "passes.csv")
    df.to_csv(passes_path, index=False)

    daily = (df.groupby(["date_utc", "satellite", "station"], as_index=False)
             .agg(total_contact_s=("duration_s", "sum"),
                  total_data_mb=("data_mb_est", "sum"),
                  passes=("duration_s", "count")))

    daily_path = os.path.join(outdir, "daily_summary.csv")
    daily.to_csv(daily_path, index=False)

    daily_total = (daily.groupby("date_utc", as_index=False)
                   .agg(total_contact_s=("total_contact_s", "sum"),
                        total_data_mb=("total_data_mb", "sum"),
                        passes=("passes", "sum")))

    plt.figure()
    plt.plot(daily_total["date_utc"], daily_total["total_contact_s"] / 60.0, marker="o")
    plt.xlabel("Date (UTC)")
    plt.ylabel("Total contact time (min)")
    plt.title("Daily Ground Contact Time")
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(os.path.join(outdir, "daily_contact_time.png"), dpi=200)
    plt.close()

    plt.figure()
    plt.plot(daily_total["date_utc"], daily_total["total_data_mb"], marker="o")
    plt.xlabel("Date (UTC)")
    plt.ylabel("Estimated data return (MB)")
    plt.title("Daily Data Return Estimate")
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(os.path.join(outdir, "daily_data_return.png"), dpi=200)
    plt.close()

    print(f"Saved:\n- {passes_path}\n- {daily_path}\n- {outdir}/daily_contact_time.png\n- {outdir}/daily_data_return.png")

def main():
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    outdir = sys.argv[1] if len(sys.argv) > 1 else "outputs"
    df = compute_passes(cfg)

    if df.empty:
        print("No passes found. Try: lower mask_deg (e.g., 5.0 or 0.0) or extend the time window.")
        return

    make_outputs(df, outdir=outdir)

if __name__ == "__main__":
    main()
