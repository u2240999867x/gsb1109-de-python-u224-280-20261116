"""Build an analysis-ready table from PSP FIELDS MAG RTN 1-min CDF data.

Rules implemented (per data-engineering spec):

1. Measurements and quality flags each carry their own DEPEND_0 with
   different sampling rates; joining is by time interval, never by index.
2. A flag record is valid over [t_i, t_i+1); the last record gets one
   nominal cadence. Expired flags are NOT carried forward: a measurement
   outside every flag interval gets quality "undetermined".
3. Flags sharing one timestamp: identical values collapse; conflicting
   values stay "undetermined" (no silent pick). Distinct flags combine
   bitwise (OR) only when the same source lists them as separate bits.
4. Missing raw codes are identified by exact typed comparison against
   FILLVAL *before* any scaling; a value that merely equals FILLVAL after
   scaling is not missing.
5. VALIDMIN/VALIDMAX gate the raw values (component-wise for vectors);
   NaN is not FILLVAL but fails the validity gate.
6. A gap in one quality source only affects measurements depending on
   that source; every raw observation is retained record-by-record in
   the output, admitted or not, with its filtering rationale.
"""

from pathlib import Path

import numpy as np

import cdflib
from cdflib.epochs import CDFepoch

CDF_FILE = Path(__file__).resolve().parent.parent / "tests" / "testfiles" / "psp_fld_l2_mag_rtn_1min_20200104_v02.cdf"
OUT_CSV = Path(__file__).resolve().parent / "analysis_table.csv"

MEASURE_VAR = "psp_fld_l2_mag_RTN_1min"
FLAG_VAR = "psp_fld_l2_quality_flags"

FLAG_BITS = {
    1: "FIELDS antenna bias sweep",
    2: "PSP thruster firing",
    4: "SCM calibration",
    8: "MAG rolls",
    16: "MAG calibration sequence",
    32: "SWEAP SPC electron mode",
    64: "SLS test",
    128: "off umbra pointing",
}

UNDETERMINED = "UNDETERMINED"


def is_fillval(raw: np.ndarray, fillval: np.generic) -> np.ndarray:
    """Exact typed comparison on the RAW values, before any scaling."""
    return raw.astype(fillval.dtype, copy=False) == fillval


def in_valid_range(raw: np.ndarray, vmin: np.ndarray, vmax: np.ndarray) -> np.ndarray:
    """Component-wise VALIDMIN/MAX gate; NaN fails by construction."""
    raw = np.asarray(raw)
    return np.all((raw >= vmin) & (raw <= vmax), axis=-1) if raw.ndim > 1 else (raw >= vmin) & (raw <= vmax)


def decode_flags(value: int) -> str:
    if value == 0:
        return "none"
    return "|".join(name for bit, name in FLAG_BITS.items() if value & bit) or f"unknown({value})"


def build_flag_intervals(flag_epochs: np.ndarray, flag_values: np.ndarray, flag_fillval: np.generic):
    """Collapse duplicates, mark same-time conflicts, emit [start, end) intervals."""
    order = np.argsort(flag_epochs, kind="stable")
    epochs, values = flag_epochs[order], flag_values[order]

    uniq_epochs, starts = np.unique(epochs, return_index=True)
    combined = np.empty(len(uniq_epochs), dtype=object)
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(epochs)
        group = values[s:e]
        group = group[~is_fillval(group, flag_fillval)]  # drop typed missing codes
        distinct = np.unique(group)
        if len(distinct) == 0:
            combined[i] = UNDETERMINED  # only FILLVAL present at this time
        elif len(distinct) == 1:
            combined[i] = int(distinct[0])
        else:
            combined[i] = UNDETERMINED  # same-time conflict: keep undetermined

    diffs = np.diff(uniq_epochs)
    cadence = np.median(diffs) if len(diffs) else 0
    ends = np.append(uniq_epochs[1:], uniq_epochs[-1] + cadence if len(uniq_epochs) else 0)
    return uniq_epochs, ends, combined


def lookup_flag(t: np.int64, starts: np.ndarray, ends: np.ndarray, combined: np.ndarray):
    """Flag whose [start, end) interval covers t; None if expired/gap (no carry-forward)."""
    i = np.searchsorted(starts, t, side="right") - 1
    if i < 0 or t >= ends[i]:
        return None
    return combined[i]


def main() -> None:
    cdf = cdflib.CDF(CDF_FILE)

    meas_attrs = cdf.varattsget(MEASURE_VAR)
    meas_epoch_name = meas_attrs["DEPEND_0"]
    flag_attrs = cdf.varattsget(FLAG_VAR)
    flag_epoch_name = flag_attrs["DEPEND_0"]
    assert meas_epoch_name != flag_epoch_name, "spec: each has its own DEPEND_0"

    meas_epochs = np.asarray(cdf.varget(meas_epoch_name))
    meas = np.asarray(cdf.varget(MEASURE_VAR))
    flag_epochs = np.asarray(cdf.varget(flag_epoch_name))
    flags = np.asarray(cdf.varget(FLAG_VAR))

    meas_epoch_attrs = cdf.varattsget(meas_epoch_name)
    flag_epoch_attrs = cdf.varattsget(flag_epoch_name)

    starts, ends, combined = build_flag_intervals(flag_epochs, flags, flag_attrs["FILLVAL"])

    meas_fill = is_fillval(meas, meas_attrs["FILLVAL"])
    meas_ok_range = in_valid_range(meas, meas_attrs["VALIDMIN"], meas_attrs["VALIDMAX"])
    epoch_fill = is_fillval(meas_epochs, meas_epoch_attrs["FILLVAL"])
    epoch_ok = (meas_epochs >= meas_epoch_attrs["VALIDMIN"]) & (meas_epochs <= meas_epoch_attrs["VALIDMAX"])

    utc = [str(t) for t in CDFepoch.to_datetime(meas_epochs)]

    rows = []
    for i in range(len(meas_epochs)):
        reasons = []
        admitted = True

        if epoch_fill[i]:
            admitted = False
            reasons.append("epoch is typed FILLVAL (raw code match)")
        elif not epoch_ok[i]:
            admitted = False
            reasons.append("epoch outside VALIDMIN/VALIDMAX")

        if meas_fill[i].any():
            admitted = False
            reasons.append("B component equals typed FILLVAL raw code")
        elif not meas_ok_range[i]:
            admitted = False
            bad = ",".join(c for c, ok in zip("RTN", (meas[i] >= meas_attrs["VALIDMIN"]) & (meas[i] <= meas_attrs["VALIDMAX"])) if not ok)
            reasons.append(f"B component(s) {bad} fail VALIDMIN/MAX gate (NaN or out of range)")

        flag = lookup_flag(meas_epochs[i], starts, ends, combined)
        if flag is None:
            admitted = False
            flag_txt = "no covering flag interval (expired flags not carried forward)"
            reasons.append("quality undetermined: flag gap for this DEPEND_0 source")
        elif flag == UNDETERMINED:
            admitted = False
            flag_txt = "undetermined (same-time conflict or FILLVAL-only)"
            reasons.append("quality undetermined: conflicting flags at same time")
        else:
            flag_txt = f"{flag} ({decode_flags(flag)})"
            if flag != 0:
                admitted = False
                reasons.append(f"quality flag bits set: {decode_flags(flag)}")
            else:
                reasons.append("flag 0 within validity interval: no quality bits set")

        if admitted:
            reasons.insert(0, "ADMITTED")
        else:
            reasons.insert(0, "RETAINED-ONLY")

        rows.append(
            {
                "record": i,
                "epoch_utc": utc[i],
                "B_R": meas[i, 0],
                "B_T": meas[i, 1],
                "B_N": meas[i, 2],
                "flag_interval_start": "" if flag is None or flag == UNDETERMINED else str(CDFepoch.to_datetime(starts[np.searchsorted(starts, meas_epochs[i], side="right") - 1])[0]),
                "quality_flag": flag_txt,
                "status": "admitted" if admitted else "retained_only",
                "rationale": "; ".join(reasons),
            }
        )

    import csv

    with open(OUT_CSV, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    n_admit = sum(r["status"] == "admitted" for r in rows)
    print(f"raw records retained: {len(rows)}")
    print(f"admitted to analysis table: {n_admit}")
    print(f"retained-only (excluded from analysis): {len(rows) - n_admit}")
    for r in rows:
        if r["status"] != "admitted":
            print(f"  record {r['record']:>3} {r['epoch_utc']}: {r['rationale']}")
    print(f"wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
