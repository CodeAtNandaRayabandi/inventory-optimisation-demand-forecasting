"""Inventory optimisation and demand forecasting on the UCI Online Retail dataset.

Steps
1. Clean transactions (drop cancellations, returns, non-product codes).
2. ABC-classify SKUs by revenue.
3. Forecast weekly demand for the steady top sellers with three simple models
   and compare them on an 8-week hold-out.
4. Compute EOQ, safety stock and reorder point per SKU and compare the
   ordering + holding cost against a fixed monthly-order baseline.

Run:  python src/analysis.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "online_retail.csv"
OUT = ROOT / "outputs"
FIG = OUT / "figures"
FIG.mkdir(parents=True, exist_ok=True)

# ---- assumptions (also listed in the Excel workbook and the README) ----
HOLDOUT_WEEKS = 8          # weeks held back to test the forecasts
TOP_N = 100                # top revenue SKUs considered for forecasting
MIN_ACTIVE_WEEKS = 0.9     # SKU must sell in at least 90% of weeks
ORDER_COST = 25.0          # GBP per purchase order (assumed)
HOLDING_RATE = 0.20        # annual holding cost as a fraction of unit price (assumed)
LEAD_TIME_WEEKS = 2        # supplier lead time (assumed)
Z = 1.645                  # 95% cycle service level

BLUE, ORANGE, AQUA, INK, MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#0b0b0b", "#52514e"
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.edgecolor": "#c9c8c2", "axes.labelcolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": MUTED, "figure.facecolor": "#fcfcfb",
                     "axes.facecolor": "#fcfcfb", "axes.grid": True, "grid.color": "#e8e7e2",
                     "grid.linewidth": 0.6, "axes.axisbelow": True})


def load_clean() -> pd.DataFrame:
    df = pd.read_csv(RAW, dtype={"InvoiceNo": str, "StockCode": str})
    n_raw = len(df)
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], format="%m/%d/%Y %H:%M")
    cancelled = df[df["InvoiceNo"].str.startswith("C")]
    # drop sales lines that were later cancelled in full (same customer, SKU, quantity)
    key = ["CustomerID", "StockCode", "Quantity"]
    canc_keys = cancelled.assign(Quantity=-cancelled["Quantity"])[key].dropna().drop_duplicates()
    df = df.merge(canc_keys.assign(_c=1), on=key, how="left")
    df = df[df["_c"].isna()].drop(columns="_c")
    df = df[~df["InvoiceNo"].str.startswith("C")]
    df = df[(df["Quantity"] > 0) & (df["UnitPrice"] > 0)]
    df = df[df["StockCode"].str.match(r"^\d{5}")]          # real products only
    df["Revenue"] = df["Quantity"] * df["UnitPrice"]
    print(f"rows: raw {n_raw:,} -> clean {len(df):,}")
    return df


def abc(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("StockCode").agg(Description=("Description", "first"),
                                    Units=("Quantity", "sum"), Revenue=("Revenue", "sum"))
    g = g.sort_values("Revenue", ascending=False)
    g["CumShare"] = g["Revenue"].cumsum() / g["Revenue"].sum()
    g["Class"] = np.where(g["CumShare"] <= 0.80, "A", np.where(g["CumShare"] <= 0.95, "B", "C"))
    return g


def weekly_demand(df: pd.DataFrame) -> pd.DataFrame:
    w = (df.set_index("InvoiceDate").groupby("StockCode")["Quantity"]
           .resample("W-SUN").sum().unstack(0).fillna(0))
    w = w.iloc[1:-1]                      # drop partial first and last weeks
    # the retailer was closed over Christmas/New Year: remove that all-zero week
    return w[w.sum(axis=1) > 0]


def ses(y: np.ndarray, alpha: float) -> np.ndarray:
    """One-step-ahead simple exponential smoothing forecasts (f[t] predicts y[t])."""
    f = np.empty(len(y)); f[0] = y[0]
    for t in range(1, len(y)):
        f[t] = alpha * y[t - 1] + (1 - alpha) * f[t - 1]
    return f


def forecasts(y: np.ndarray, n_train: int) -> dict:
    naive = np.r_[y[0], y[:-1]]
    ma4 = np.array([y[max(0, t - 4):t].mean() if t else y[0] for t in range(len(y))])
    alphas = np.arange(0.05, 1.0, 0.05)
    sse = [((y[1:n_train] - ses(y, a)[1:n_train]) ** 2).sum() for a in alphas]
    a = float(alphas[int(np.argmin(sse))])          # alpha tuned on training weeks only
    return {"Naive": naive, "Moving average (4 wk)": ma4, "Exp. smoothing": ses(y, a), "alpha": a}


def main():
    df = load_clean()
    cls = abc(df)
    summary = cls.groupby("Class").agg(SKUs=("Revenue", "size"), Revenue=("Revenue", "sum"))
    summary["SKU share %"] = 100 * summary["SKUs"] / summary["SKUs"].sum()
    summary["Revenue share %"] = 100 * summary["Revenue"] / summary["Revenue"].sum()
    summary.round(1).to_csv(OUT / "abc_summary.csv")
    cls.round(4).to_csv(OUT / "abc_classification.csv")
    print(summary.round(1))

    w = weekly_demand(df)
    n = len(w); n_train = n - HOLDOUT_WEEKS
    top = [s for s in cls.index[:TOP_N] if (w[s] > 0).mean() >= MIN_ACTIVE_WEEKS
           and (w[s].iloc[:8] > 0).any()]
    print(f"weeks: {n} (train {n_train}, test {HOLDOUT_WEEKS}); SKUs forecast: {len(top)}")
    w[top].to_csv(ROOT / "data" / "weekly_demand_top_skus.csv")

    methods = ["Naive", "Moving average (4 wk)", "Exp. smoothing"]
    abs_err = {m: 0.0 for m in methods}; actual = 0.0; rows = []; policy = []
    price = df.groupby("StockCode")["UnitPrice"].median()
    for s in top:
        y = w[s].to_numpy(float)
        f = forecasts(y, n_train)
        test = slice(n_train, n)
        actual += y[test].sum()
        row = {"StockCode": s, "alpha": round(f["alpha"], 2)}
        for m in methods:
            e = np.abs(y[test] - f[m][test]).sum()
            abs_err[m] += e
            row[f"WAPE % {m}"] = round(100 * e / y[test].sum(), 1)
        rows.append(row)
        resid = y[1:n_train] - f["Exp. smoothing"][1:n_train]
        policy.append({"StockCode": s, "Description": cls.loc[s, "Description"],
                       "Class": cls.loc[s, "Class"],
                       "WeeklyDemand": y.mean(), "SigmaWeekly": resid.std(ddof=1),
                       "UnitPrice": float(price[s])})
    acc = pd.DataFrame({"Method": methods,
                        "WAPE %": [round(100 * abs_err[m] / actual, 1) for m in methods]})
    acc.to_csv(OUT / "forecast_accuracy.csv", index=False)
    pd.DataFrame(rows).to_csv(OUT / "forecast_accuracy_by_sku.csv", index=False)
    print(acc)

    p = pd.DataFrame(policy)
    p["AnnualDemand"] = p["WeeklyDemand"] * 52
    p["HoldingCost"] = HOLDING_RATE * p["UnitPrice"]
    p["EOQ"] = np.sqrt(2 * p["AnnualDemand"] * ORDER_COST / p["HoldingCost"])
    p["SafetyStock"] = Z * p["SigmaWeekly"] * np.sqrt(LEAD_TIME_WEEKS)
    p["ReorderPoint"] = p["WeeklyDemand"] * LEAD_TIME_WEEKS + p["SafetyStock"]
    cost = lambda q: p["AnnualDemand"] / q * ORDER_COST + q / 2 * p["HoldingCost"]
    p["CostEOQ"] = cost(p["EOQ"])
    p["CostMonthly"] = cost(p["AnnualDemand"] / 12)
    p.round(2).to_csv(OUT / "inventory_policy.csv", index=False)
    saving = 100 * (1 - p["CostEOQ"].sum() / p["CostMonthly"].sum())
    print(f"ordering+holding cost: monthly GBP {p['CostMonthly'].sum():,.0f} -> "
          f"EOQ GBP {p['CostEOQ'].sum():,.0f}  ({saving:.1f}% lower)")

    # ---- figures ----
    fig, ax = plt.subplots(figsize=(7, 3.8))
    x = 100 * np.arange(1, len(cls) + 1) / len(cls)
    ax.plot(x, 100 * cls["CumShare"], color=BLUE, lw=2)
    for c, lim in (("A", 80), ("B", 95)):
        xc = 100 * (cls["Class"] <= c).mean()
        ax.plot([xc, xc], [0, lim], color=MUTED, lw=0.8, ls="--")
        ax.annotate(f"Class {c} ends: {xc:.0f}% of SKUs, {lim}% of revenue", (xc, lim),
                    xytext=(8, -14), textcoords="offset points", color=INK, fontsize=9)
    ax.set(xlabel="% of SKUs (ranked by revenue)", ylabel="Cumulative % of revenue",
           xlim=(0, 100), ylim=(0, 102))
    ax.set_title("ABC analysis: a fifth of SKUs carry 80% of revenue", loc="left", color=INK)
    fig.tight_layout(); fig.savefig(FIG / "abc_curve.png", dpi=160); plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 2.8))
    bars = ax.barh(acc["Method"][::-1], acc["WAPE %"][::-1], color=BLUE, height=0.55)
    ax.bar_label(bars, fmt="%.1f%%", padding=4, color=INK)
    ax.set(xlabel=f"Forecast error (WAPE) on last {HOLDOUT_WEEKS} weeks, {len(top)} SKUs",
           xlim=(0, acc["WAPE %"].max() * 1.15))
    ax.grid(axis="y", visible=False)
    ax.set_title("Forecast error by method (lower is better)", loc="left", color=INK)
    fig.tight_layout(); fig.savefig(FIG / "forecast_accuracy.png", dpi=160); plt.close(fig)

    s = top[0]; y = w[s]; f = forecasts(y.to_numpy(float), n_train)
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.plot(y.index, y.values, color=INK, lw=1.6, label="Actual")
    ax.plot(y.index, f["Exp. smoothing"], color=BLUE, lw=2, label="Exp. smoothing forecast")
    ax.axvline(y.index[n_train], color=MUTED, lw=0.8, ls="--")
    ax.annotate("hold-out starts", (y.index[n_train], ax.get_ylim()[1]), xytext=(4, -12),
                textcoords="offset points", color=MUTED, fontsize=9)
    ax.set(ylabel="Units per week"); ax.legend(frameon=False, loc="upper center", ncol=2)
    ax.set_title(f"Weekly demand, top SKU {s} ({cls.loc[s, 'Description'].title()})",
                 loc="left", color=INK)
    fig.tight_layout(); fig.savefig(FIG / "top_sku_forecast.png", dpi=160); plt.close(fig)


if __name__ == "__main__":
    main()
