"""
Readmission project: data profiling and check figures from the raw Synthea CSVs.

What it does, in order:
  1. Profiles each raw CSV (rows, columns, blanks).
  2. Counts encounters by class, then keeps inpatient stays only.
  3. Rebuilds the model's logic in pandas: days since prior discharge,
     the 30-day readmission flag, length of stay, planned chemo, risk bands.
  4. Prints the numbers the Power BI report should show, so you can check them.

How to run (Python 3 with pandas installed: pip install pandas):
    python readmission_eda.py "C:/path/to/your/csv/folder"

The folder must contain encounters.csv, patients.csv, conditions.csv,
payers.csv, procedures.csv and medications.csv from the Synthea export.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

LOCAL_TZ = "America/Los_Angeles"   # the model reads times in US Pacific time
WINDOW = 30                         # readmission window in days
TODAY = pd.Timestamp("2026-10-08")  # "as of" date for age; change if you like

folder = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def section(title):
    print("\n" + "=" * 70 + "\n" + title + "\n" + "=" * 70)


def pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "n/a"


# ---------------------------------------------------------------------------
# 1. Profile the raw files
# ---------------------------------------------------------------------------
section("1. Raw files")
for name in ["encounters", "patients", "conditions", "payers"]:
    df = pd.read_csv(folder / f"{name}.csv", nrows=None, low_memory=False)
    blanks = df.isna().sum()
    blanks = blanks[blanks > 0]
    print(f"\n{name}.csv: {len(df):,} rows, {df.shape[1]} columns")
    if len(blanks):
        print("  columns with blanks:", ", ".join(f"{c} ({n:,})" for c, n in blanks.items()))

for name in ["procedures", "medications"]:   # large files: count rows only
    n = sum(1 for _ in open(folder / f"{name}.csv", encoding="utf-8")) - 1
    print(f"\n{name}.csv: {n:,} rows")

# ---------------------------------------------------------------------------
# 2. Encounters: classes, then inpatient only
# ---------------------------------------------------------------------------
section("2. Encounter classes")
enc = pd.read_csv(folder / "encounters.csv",
                  usecols=["Id", "START", "STOP", "PATIENT", "PAYER",
                           "ENCOUNTERCLASS", "REASONDESCRIPTION"])
print(enc.ENCOUNTERCLASS.value_counts().to_string())

stays = enc[enc.ENCOUNTERCLASS == "inpatient"].dropna(subset=["START", "STOP", "PATIENT"]).copy()
for col in ["START", "STOP"]:   # UTC in the file -> local time, timezone dropped
    stays[col] = pd.to_datetime(stays[col], utc=True).dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)

patients = pd.read_csv(folder / "patients.csv", usecols=["Id", "BIRTHDATE", "DEATHDATE"])
print(f"\nInpatient stays: {len(stays):,}")
print(f"Patients in file: {len(patients):,}; admitted (1+ stay): {stays.PATIENT.nunique():,}; "
      f"never admitted: {len(patients) - stays.PATIENT.nunique():,}")

# ---------------------------------------------------------------------------
# 3. Days since prior discharge and the readmission flag
#    Prior discharge = the latest STOP of the same patient that is before this START.
# ---------------------------------------------------------------------------
stays = stays.sort_values(["PATIENT", "START"]).reset_index(drop=True)
prior = []
for _, g in stays.groupby("PATIENT", sort=False):
    stops, starts = g.STOP.values, g.START.values
    for s in starts:
        earlier = stops[stops < s]
        prior.append(earlier.max() if len(earlier) else np.datetime64("NaT"))
stays["PRIOR_STOP"] = pd.to_datetime(prior)
stays["DAYS_SINCE_PRIOR"] = (stays.START.dt.normalize() - stays.PRIOR_STOP.dt.normalize()).dt.days
stays["READMISSION"] = stays.DAYS_SINCE_PRIOR <= WINDOW          # blank (first stay) -> False
stays["LOS_DAYS"] = np.floor((stays.STOP - stays.START).dt.total_seconds() / 86400)
stays["PLANNED"] = stays.REASONDESCRIPTION.fillna("").str.contains("of lung")

N, R = len(stays), int(stays.READMISSION.sum())
section("3. Readmissions")
not_first = N - stays.PATIENT.nunique()
has_prior = int(stays.DAYS_SINCE_PRIOR.notna().sum())
print(f"Stays that are not a patient's first stay: {not_first:,}")
print(f"  ...of which start before the previous stay has ended (overlap, no prior discharge): {not_first - has_prior:,}")
print(f"Stays with a previous discharge (could be a readmission): {has_prior:,}")
print(f"Readmissions within {WINDOW} days: {R:,} of {N:,} = {pct(R, N)}")
print(f"Stays starting 0 days after the previous discharge: {(stays.DAYS_SINCE_PRIOR == 0).sum():,}")

print("\nCumulative windows (the page 4 curve):")
for n in [0, 7, 15, 27, 30, 33, 45, 60, 90, 120, 180]:
    k = int((stays.DAYS_SINCE_PRIOR <= n).sum())
    print(f"  within {n:>3} days: {k:>5,}  {pct(k, N)}")

print("\nGap since previous discharge, in bins:")
bins = pd.cut(stays.DAYS_SINCE_PRIOR, [-1, 30, 60, 90, np.inf], labels=["0-30", "31-60", "61-90", "90+"])
print(bins.value_counts().sort_index().to_string())

# ---------------------------------------------------------------------------
# 4. Length of stay
# ---------------------------------------------------------------------------
section("4. Length of stay")
los = stays.LOS_DAYS
print(f"Average {los.mean():.2f} days; P25 {los.quantile(.25):.0f}, median {los.median():.0f}, P75 {los.quantile(.75):.0f}")
print("\nStays by number of days:")
print(los.clip(upper=10).value_counts().sort_index().rename(index={10: "10+"}).to_string())

p25, p75 = los.quantile(.25), los.quantile(.75)
stays["LOS_BAND"] = np.select([los <= p25, los <= p75], ["Short", "Typical"], "Long")
stays["STAY_TYPE"] = np.where(stays.PLANNED, "Planned chemo", "Unplanned")
print(f"\nBands from the data: Short <= {p25:.0f} day, Typical up to {p75:.0f}, Long > {p75:.0f}")
t = stays.pivot_table(index="LOS_BAND", columns="STAY_TYPE", values="READMISSION",
                      aggfunc=["size", "mean"]).reindex(["Short", "Typical", "Long"])
t["mean"] = (t["mean"] * 100).round(1)
print("Stays (size) and readmission rate % (mean) by band and stay type:")
print(t.to_string())

# ---------------------------------------------------------------------------
# 5. Planned chemo vs unplanned
# ---------------------------------------------------------------------------
section("5. Planned chemo vs unplanned")
for label, g in stays.groupby("STAY_TYPE"):
    print(f"{label:>14}: {len(g):,} stays, {g.PATIENT.nunique():,} patients, "
          f"{int(g.READMISSION.sum()):,} readmissions, rate {pct(g.READMISSION.sum(), len(g))}")
print(f"Share of all readmissions from planned chemo: "
      f"{pct(stays[stays.PLANNED].READMISSION.sum(), R)}")

# ---------------------------------------------------------------------------
# 6. Chronic conditions and risk bands
# ---------------------------------------------------------------------------
section("6. Chronic conditions and risk bands")
cond = pd.read_csv(folder / "conditions.csv", usecols=["PATIENT", "CODE", "DESCRIPTION"])
codes = cond[["CODE", "DESCRIPTION"]].drop_duplicates("CODE")
include = ["diabetes", "hypertension", "hyperlipidemia", "chronic kidney", "renal disease",
           "coronary heart", "myocardial infarction", "heart failure", "atrial fibrillation",
           "asthma", "emphysema", "pulmonary disease", "osteoarthritis", "osteoporosis",
           "rheumatoid", "seizure", "epilepsy", "alzheimer", "dementia", "depress",
           "neoplasm", "carcinoma", "stroke", "hypothyroidism"]
exclude = ["(finding)", "(situation)", "acute", "due to",
           "kidney disease stage 1", "kidney disease stage 2"]
d = codes.DESCRIPTION.str.lower().str.replace("-", " ")
codes["CHRONIC"] = d.apply(lambda s: any(k in s for k in include) and not any(k in s for k in exclude))
print(f"Distinct condition codes: {len(codes):,}; flagged chronic: {codes.CHRONIC.sum()}")

bridge = cond[["PATIENT", "CODE"]].drop_duplicates()
print(f"Bridge rows (patient + condition pairs): {len(bridge):,}")
chronic = bridge.merge(codes[codes.CHRONIC], on="CODE").groupby("PATIENT").CODE.nunique()

pt = stays.groupby("PATIENT").agg(avg_los=("LOS_DAYS", "mean"))
pt["chronic"] = chronic.reindex(pt.index).fillna(0)
p95c, p75c = pt.chronic.quantile(.95), pt.chronic.quantile(.75)
p75l, medl = pt.avg_los.quantile(.75), pt.avg_los.median()
print(f"Thresholds from the data: P95 chronic {p95c:.0f}, P75 chronic {p75c:.0f}, "
      f"P75 avg stay {p75l:.0f}, median avg stay {medl:.2f}")

pt["band"] = np.select([(pt.chronic >= p95c) & (pt.avg_los > p75l),
                        (pt.chronic >= p75c) | (pt.avg_los > medl)], ["High", "Medium"], "Low")
stays["BAND"] = stays.PATIENT.map(pt.band)
b = stays.groupby("BAND").agg(stays=("Id", "size"), readm=("READMISSION", "sum"))
b["patients"] = pt.band.value_counts()
b["% patients"] = (b.patients / b.patients.sum() * 100).round(1)
b["% readmissions"] = (b.readm / R * 100).round(1)
b["rate %"] = (b.readm / b.stays * 100).round(1)
print(b.reindex(["High", "Medium", "Low"]).to_string())

# ---------------------------------------------------------------------------
# 7. Age and payer
# ---------------------------------------------------------------------------
section("7. Age band and payer")
age = (TODAY - pd.to_datetime(patients.set_index("Id").BIRTHDATE)).dt.days / 365.25
stays["AGE_BAND"] = pd.cut(stays.PATIENT.map(age), [0, 18, 35, 55, 75, 200], right=False,
                           labels=["0-17", "18-34", "35-54", "55-74", "75+"])
a = stays.groupby("AGE_BAND", observed=True).READMISSION.agg(["size", "sum", "mean"])
a["mean"] = (a["mean"] * 100).round(1)
print(a.rename(columns={"size": "stays", "sum": "readmissions", "mean": "rate %"}).to_string())

payers = pd.read_csv(folder / "payers.csv", usecols=["Id", "NAME"]).set_index("Id").NAME
group = {"Medicare": "Medicare", "Medicaid": "Medicaid", "Dual Eligible": "Dual",
         "NO_INSURANCE": "No insurance"}
stays["PAYER_GROUP"] = stays.PAYER.map(payers).map(lambda n: group.get(n, "Private"))
p = stays.groupby("PAYER_GROUP").READMISSION.agg(["size", "sum", "mean"])
p["mean"] = (p["mean"] * 100).round(1)
print()
print(p.rename(columns={"size": "stays", "sum": "readmissions", "mean": "rate %"}).to_string())

print("\nDone.")
