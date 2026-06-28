"""
models/nrfi_model.py

Dedicated NRFI (No Run First Inning) prediction model using XGBoost.

Trained on historical MLB games (2022–2024) from scripts/build_nrfi_dataset.py.
Replaces the rough Poisson mu/9 approximation with a calibrated classifier
trained specifically on first-inning outcomes.

Features (all available from the live analysis pipeline):
  Pitcher quality (home + away):
    siera, xfip, fip, csw_pct, o_swing_pct, k_pct, bb_pct, gb_pct, hr_fb_pct
  Game context:
    park_factor, is_dome

Usage:
  # Train (run once after building dataset):
  python models/nrfi_model.py --train

  # Predict in code:
  from models.nrfi_model import predict_nrfi
  prob = predict_nrfi(home_starter=..., away_starter=..., home_team="NYY", ...)
"""
from __future__ import annotations
import argparse
import os
import pickle
from pathlib import Path
from typing import Optional

import numpy as np

_REPO          = Path(__file__).resolve().parent.parent
MODEL_PATH     = _REPO / "models" / "nrfi_xgb.json"
CALIBRATOR_PATH = _REPO / "models" / "nrfi_calibrator.pkl"
DATASET_PATH   = _REPO / "data" / "nrfi_dataset.csv"

# Features used at training AND prediction time — order must match
FEATURES = [
    # Rolling first-inning run rate per starter — most predictive signal
    # Computed by scripts/enrich_nrfi_fi_rates.py from within the dataset
    "home_starter_fi_rate", "away_starter_fi_rate",
    # Pitcher season stats (FanGraphs when available, BRef fallback for FIP/K%/BB%)
    "home_siera",      "home_xfip",      "home_fip",
    "home_csw_pct",    "home_o_swing_pct", "home_k_pct",
    "home_bb_pct",     "home_gb_pct",    "home_hr_fb_pct",
    "away_siera",      "away_xfip",      "away_fip",
    "away_csw_pct",    "away_o_swing_pct", "away_k_pct",
    "away_bb_pct",     "away_gb_pct",    "away_hr_fb_pct",
    # top-3 lineup wRC+ — only spots 1-3 bat in the 1st inning
    "home_top3_wrc",   "away_top3_wrc",
    "park_factor",     "is_dome",
]

# Fallback league-average values when a feature is missing
FEATURE_DEFAULTS = {
    "home_starter_fi_rate": 0.477,   # historical league avg: ~47.7% of starts allow ≥1 1st-inn run
    "away_starter_fi_rate": 0.477,
    "home_siera":        4.00,
    "home_xfip":         4.00,
    "home_fip":          4.00,
    "home_csw_pct":      0.285,
    "home_o_swing_pct":  0.295,
    "home_k_pct":        0.225,
    "home_bb_pct":       0.082,
    "home_gb_pct":       0.440,
    "home_hr_fb_pct":    0.115,
    "away_siera":        4.00,
    "away_xfip":         4.00,
    "away_fip":          4.00,
    "away_csw_pct":      0.285,
    "away_o_swing_pct":  0.295,
    "away_k_pct":        0.225,
    "away_bb_pct":       0.082,
    "away_gb_pct":       0.440,
    "away_hr_fb_pct":    0.115,
    "home_top3_wrc":     100.0,   # league average wRC+ = 100 by definition
    "away_top3_wrc":     100.0,
    "park_factor":       1.0,
    "is_dome":           0,
}

# Park factor and dome lookup for prediction-time use
_PARK_FACTORS: dict[str, float] = {
    "COL": 1.19, "CIN": 1.08, "BOS": 1.06, "TEX": 1.05,
    "PHI": 1.04, "CHW": 1.03, "ATL": 1.02, "BAL": 1.01,
    "HOU": 1.01, "LAA": 1.00, "MIA": 1.00, "MIL": 1.00,
    "DET": 0.99, "PIT": 0.99, "MIN": 0.99, "KC":  0.98,
    "NYY": 0.98, "TOR": 0.98, "NYM": 0.97, "STL": 0.97,
    "CLE": 0.97, "WSH": 0.96, "TB":  0.96, "OAK": 0.96,
    "CHC": 0.96, "ARI": 0.95, "LAD": 0.95, "SD":  0.94,
    "SEA": 0.93, "SF":  0.92,
}
_DOME_TEAMS = {"TB", "MIA", "MIL", "ARI", "HOU", "SEA", "TOR", "TEX"}


# ── Lazy-loaded model ─────────────────────────────────────────────────────────

_xgb_model    = None
_calibrator   = None
_model_loaded = False


def _load_model() -> bool:
    global _xgb_model, _calibrator, _model_loaded
    if _model_loaded:
        return _xgb_model is not None

    try:
        import xgboost as xgb
        if not MODEL_PATH.exists():
            _model_loaded = True
            return False
        _xgb_model = xgb.XGBClassifier()
        _xgb_model.load_model(str(MODEL_PATH))
        if CALIBRATOR_PATH.exists():
            with open(CALIBRATOR_PATH, "rb") as f:
                _calibrator = pickle.load(f)
        _model_loaded = True
        return True
    except Exception:
        _model_loaded = True
        return False


def model_available() -> bool:
    """True if the trained NRFI model file exists and loads successfully."""
    return _load_model()


# ── Public prediction API ─────────────────────────────────────────────────────

def predict_nrfi(
    home_starter: dict,
    away_starter: dict,
    home_team: str = "",
    park_factor: Optional[float] = None,
    home_top3_wrc: Optional[float] = None,
    away_top3_wrc: Optional[float] = None,
) -> Optional[float]:
    """
    Return calibrated NRFI probability (0–1) using the trained XGBoost model.
    Returns None if the model is not trained yet (falls back to Poisson in caller).

    home_starter / away_starter dicts accept any keys from the analysis pipeline:
      siera, xfip, fip, csw_pct, o_swing_pct, k_pct, bb_pct, gb_pct, hr_fb_pct
    home_top3_wrc / away_top3_wrc: avg wRC+ for lineup spots 1-3 (default: 100)
    Missing keys default to league-average values.
    """
    if not _load_model() or _xgb_model is None:
        return None

    pf      = park_factor or _PARK_FACTORS.get(home_team.upper(), 1.0)
    is_dome = 1 if home_team.upper() in _DOME_TEAMS else 0

    def _get(d: dict, key: str, default_key: Optional[str] = None) -> float:
        val = d.get(key)
        if val is None or (isinstance(val, float) and val != val):  # NaN
            val = d.get(default_key) if default_key else None
        if val is None or (isinstance(val, float) and val != val):
            return FEATURE_DEFAULTS.get(key, 0.0)
        try:
            return float(val)
        except (TypeError, ValueError):
            return FEATURE_DEFAULTS.get(key, 0.0)

    def _scalar(val: Optional[float], default: float) -> float:
        if val is None or (isinstance(val, float) and val != val):
            return default
        return float(val)

    # Build feature vector — order must match FEATURES list
    fv = [
        _get(home_starter, "siera"),
        _get(home_starter, "xfip"),
        _get(home_starter, "fip"),
        _get(home_starter, "csw_pct"),
        _get(home_starter, "o_swing_pct"),
        _get(home_starter, "k_pct",  "k_pct"),
        _get(home_starter, "bb_pct", "bb_pct"),
        _get(home_starter, "gb_pct", "gb_pct"),
        _get(home_starter, "hr_fb_pct"),
        _get(away_starter, "siera"),
        _get(away_starter, "xfip"),
        _get(away_starter, "fip"),
        _get(away_starter, "csw_pct"),
        _get(away_starter, "o_swing_pct"),
        _get(away_starter, "k_pct",  "k_pct"),
        _get(away_starter, "bb_pct", "bb_pct"),
        _get(away_starter, "gb_pct", "gb_pct"),
        _get(away_starter, "hr_fb_pct"),
        _scalar(home_top3_wrc, FEATURE_DEFAULTS["home_top3_wrc"]),
        _scalar(away_top3_wrc, FEATURE_DEFAULTS["away_top3_wrc"]),
        pf,
        float(is_dome),
    ]

    X = np.array([fv], dtype=np.float32)

    try:
        raw_prob = float(_xgb_model.predict_proba(X)[0][1])
        if _calibrator is not None:
            # Platt scaling: LogisticRegression.predict_proba expects 2-D input
            raw_prob = float(_calibrator.predict_proba([[raw_prob]])[0][1])
        return max(0.0, min(1.0, raw_prob))
    except Exception:
        return None


# ── Training ──────────────────────────────────────────────────────────────────

def train(dataset_path: Path = DATASET_PATH) -> None:
    """
    Train XGBoost on the historical dataset and save model + calibrator.

    Train/val split:
      Train: 2022, 2023
      Test:  2024  (held out — never seen during training)

    Calibration:
      Isotonic regression on the 2023 validation fold to correct
      probability over/under-confidence before testing on 2024.
    """
    try:
        import xgboost as xgb
        from sklearn.calibration import calibration_curve
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
        import pandas as pd
    except ImportError as e:
        print(f"ERROR: {e}\nInstall: pip install xgboost scikit-learn pandas")
        return

    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}")
        print("Run: python scripts/build_nrfi_dataset.py")
        return

    df = pd.read_csv(dataset_path, encoding="latin-1")
    print(f"Loaded {len(df)} rows from {dataset_path}")
    print(f"Seasons: {sorted(df['season'].unique())}")
    print(f"NRFI rate: {df['nrfi'].mean()*100:.1f}%")

    # Coverage diagnostic — warn if key features are missing
    print("\nFeature coverage:")
    diag_cols = [
        ("home_starter_fi_rate", 90, "CRITICAL — run: python scripts/enrich_nrfi_fi_rates.py"),
        ("away_starter_fi_rate", 90, "CRITICAL — run: python scripts/enrich_nrfi_fi_rates.py"),
        ("home_k_pct",           70, "BRef fallback"),
        ("home_fip",             70, "BRef fallback"),
        ("home_siera",           50, "FanGraphs (blocked — will default)"),
        ("home_top3_wrc",        70, "BRef OPS+ proxy"),
    ]
    for col, warn_pct, note in diag_cols:
        if col not in df.columns:
            print(f"  ✗ {col:<28} MISSING — {note}")
            continue
        n   = df[col].notna().sum()
        pct = n / len(df) * 100
        sym = "✓" if pct >= warn_pct else ("~" if pct >= 40 else "✗")
        print(f"  {sym} {col:<28} {n:>5}/{len(df)}  ({pct:.1f}%)  {note if pct < warn_pct else ''}")

    # Drop rows missing all pitcher quality signals — BRef provides fip/k_pct/bb_pct
    # but not siera/xfip, so filter on columns that are actually populated
    key_cols = ["home_fip", "home_k_pct", "away_fip", "away_k_pct"]
    df_clean = df.dropna(subset=key_cols, how="all")
    print(f"\nAfter dropping rows missing all key pitcher cols: {len(df_clean)} rows")

    # Fill remaining NaN with league averages
    df_clean = df_clean.copy()   # avoid SettingWithCopyWarning
    for feat in FEATURES:
        if feat in df_clean.columns:
            df_clean[feat] = df_clean[feat].fillna(FEATURE_DEFAULTS.get(feat, 0.0))
        else:
            df_clean[feat] = FEATURE_DEFAULTS.get(feat, 0.0)

    # Split by season
    train_df = df_clean[df_clean["season"].isin([2022, 2023])].copy()
    val_df   = df_clean[df_clean["season"] == 2023].copy()   # calibration fold
    test_df  = df_clean[df_clean["season"] == 2024].copy()

    X_train = train_df[FEATURES].values.astype(np.float32)
    y_train = train_df["nrfi"].values.astype(int)
    X_val   = val_df[FEATURES].values.astype(np.float32)
    y_val   = val_df["nrfi"].values.astype(int)
    X_test  = test_df[FEATURES].values.astype(np.float32)
    y_test  = test_df["nrfi"].values.astype(int)

    print(f"\nTrain: {len(X_train)} games (2022–2023)")
    print(f"Test:  {len(X_test)} games (2024 held-out)")

    # XGBoost — conservative hyperparams to avoid overfitting on small dataset
    model = xgb.XGBClassifier(
        n_estimators      = 400,
        max_depth         = 4,
        learning_rate     = 0.05,
        subsample         = 0.8,
        colsample_bytree  = 0.7,
        min_child_weight  = 10,   # prevents splits on very few samples
        gamma             = 1.0,  # min loss reduction for a split
        scale_pos_weight  = (y_train == 0).sum() / max(1, (y_train == 1).sum()),
        eval_metric       = "logloss",
        early_stopping_rounds = 30,
        random_state      = 42,
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=50,
    )

    # Platt scaling (logistic regression) on the 2023 validation fold
    # More robust than isotonic at <5000 samples; avoids staircase overfitting
    raw_val_probs = model.predict_proba(X_val)[:, 1]
    calibrator    = LogisticRegression(C=1.0, solver="lbfgs")
    calibrator.fit(raw_val_probs.reshape(-1, 1), y_val)
    print("\nPlatt scaling (logistic) fitted on 2023 val fold")

    # Test-set evaluation (2024 — never seen)
    raw_test_probs = model.predict_proba(X_test)[:, 1]
    cal_test_probs = calibrator.predict_proba(raw_test_probs.reshape(-1, 1))[:, 1]

    brier_raw = brier_score_loss(y_test, raw_test_probs)
    brier_cal = brier_score_loss(y_test, cal_test_probs)
    auc       = roc_auc_score(y_test, cal_test_probs)
    ll        = log_loss(y_test, cal_test_probs)

    # Accuracy at 0.5 threshold and at calibrated "BET" threshold (65%)
    preds_50  = (cal_test_probs >= 0.5).astype(int)
    preds_65  = cal_test_probs >= 0.65
    acc_50    = (preds_50 == y_test).mean()
    n_65      = preds_65.sum()
    acc_65    = (y_test[preds_65] == 1).mean() if n_65 > 0 else float("nan")

    print(f"\n{'='*50}")
    print(f"2024 HOLD-OUT TEST RESULTS")
    print(f"{'='*50}")
    print(f"  Brier score (raw):        {brier_raw:.4f}  (lower=better; 0.25=random)")
    print(f"  Brier score (calibrated): {brier_cal:.4f}")
    print(f"  AUC-ROC:                  {auc:.4f}  (0.5=random, 1.0=perfect)")
    print(f"  Log-loss:                 {ll:.4f}")
    print(f"  Accuracy @ 50% threshold: {acc_50*100:.1f}%  (on {len(y_test)} games)")
    print(f"  Accuracy @ 65% threshold: {acc_65*100:.1f}%  (on {n_65} games tagged BET)")
    print(f"  Base NRFI rate 2024:      {y_test.mean()*100:.1f}%")
    print(f"{'='*50}")

    if auc < 0.52:
        print("\nWARNING: AUC near random — model may not have learned real signal.")
        print("Consider: more seasons, additional features (weather, lineup), or")
        print("          check whether FanGraphs data is matching correctly.")
    elif acc_65 >= 0.65:
        print("\n✓ Model clears 65% accuracy at the BET threshold — edge is real.")
    elif acc_65 >= 0.60:
        print("\n~ 60-65% accuracy at BET threshold — modest edge, keep collecting data.")
    else:
        print("\n✗ <60% at BET threshold — base rate beats the filter. Do not use.")

    # Calibration curve — checks if predicted probabilities match actual frequencies
    print(f"\n{'='*50}")
    print(f"CALIBRATION CURVE (2024 hold-out)")
    print(f"{'='*50}")
    print(f"  {'Bin':>10}  {'Pred%':>7}  {'Actual%':>8}  {'n':>5}  {'Δ':>6}")
    try:
        frac_pos, mean_pred = calibration_curve(y_test, cal_test_probs, n_bins=8, strategy="quantile")
        # Compute bin counts manually
        import numpy as _np
        bin_edges = _np.percentile(cal_test_probs, _np.linspace(0, 100, 9))
        bin_counts = []
        for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
            mask = (cal_test_probs >= lo) & (cal_test_probs <= hi)
            bin_counts.append(mask.sum())
        for mp, fp, cnt in zip(mean_pred, frac_pos, bin_counts):
            delta = fp - mp
            bar = "+" * int(abs(delta) * 20) if delta > 0 else "-" * int(abs(delta) * 20)
            print(f"  {mp*100:>9.1f}%  {mp*100:>6.1f}%  {fp*100:>7.1f}%  {cnt:>5}  {delta:>+.3f}  {bar}")
        avg_delta = abs(frac_pos - mean_pred).mean()
        print(f"\n  Mean |calibration error|: {avg_delta:.4f}  (0=perfect, 0.05=acceptable)")
        if avg_delta < 0.04:
            print("  ✓ Well-calibrated — probabilities are trustworthy.")
        elif avg_delta < 0.08:
            print("  ~ Acceptable calibration — slight over/under-confidence in some bins.")
        else:
            print("  ✗ Poor calibration — probabilities don't reflect true frequencies.")
    except Exception as e:
        print(f"  (calibration curve error: {e})")

    # Feature importance
    importance = dict(zip(FEATURES, model.feature_importances_))
    top_feats  = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:8]
    print("\nTop features by importance:")
    for feat, imp in top_feats:
        print(f"  {feat:<25} {imp:.4f}")

    # Save
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(MODEL_PATH))
    with open(CALIBRATOR_PATH, "wb") as f:
        pickle.dump(calibrator, f)

    print(f"\nSaved model:      {MODEL_PATH}")
    print(f"Saved calibrator: {CALIBRATOR_PATH}")
    print("\nActivate in analyze_baseball.py:")
    print("  The NRFI model auto-loads on next app restart.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train",   action="store_true", help="Train the model")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH,
                        help="Path to the CSV from build_nrfi_dataset.py")
    args = parser.parse_args()

    if args.train:
        train(args.dataset)
    else:
        parser.print_help()
