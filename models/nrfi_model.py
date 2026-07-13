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

from fetchers.baseball import PARK_FACTORS as _PARK_FACTORS

_REPO          = Path(__file__).resolve().parent.parent
MODEL_PATH     = _REPO / "models" / "nrfi_xgb.json"
CALIBRATOR_PATH = _REPO / "models" / "nrfi_calibrator.pkl"
DATASET_PATH   = _REPO / "data" / "nrfi_dataset.csv"

# Features used at training AND prediction time — order must match
FEATURES = [
    # Rolling first-inning run rate per starter (enrich_nrfi_fi_rates.py)
    "home_starter_fi_rate", "away_starter_fi_rate",
    # Savant Statcast quality metrics (enrich_nrfi_savant.py)
    # xwoba_against removed — statcast_pitcher_exitvelo_barrels does not return xwOBA;
    # barrel_pct and hard_hit_pct cover the same contact-quality signal.
    "home_barrel_pct",     "away_barrel_pct",        # barrel rate allowed
    "home_hard_hit_pct",   "away_hard_hit_pct",      # hard-hit % allowed
    "home_whiff_pct",      "away_whiff_pct",          # swing-and-miss rate
    "home_avg_velo",       "away_avg_velo",           # fastball velocity
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

# Fallback league-average values when a feature is missing at predict time
FEATURE_DEFAULTS = {
    "home_starter_fi_rate": 0.29,    # per-pitcher avg fi_rate (NOT the 0.477 YRFI rate)
    "away_starter_fi_rate": 0.29,
    "home_barrel_pct":      0.085,   # MLB average barrel rate against
    "away_barrel_pct":      0.085,
    "home_hard_hit_pct":    0.370,   # MLB average hard-hit % against
    "away_hard_hit_pct":    0.370,
    "home_whiff_pct":       0.245,   # MLB average whiff %
    "away_whiff_pct":       0.245,
    "home_avg_velo":        93.5,    # MLB average fastball velocity
    "away_avg_velo":        93.5,
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
    "home_top3_wrc":     100.0,
    "away_top3_wrc":     100.0,
    "park_factor":       1.0,
    "is_dome":           0,
}

# Dome lookup for prediction-time use. Park factors come from fetchers.baseball.PARK_FACTORS
# (imported above) — do not re-hardcode a second copy here, it will drift out of sync
# with the corrected values (see CLAUDE.md Bug 2 / README Known Discrepancies).
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
    home_fi_rate: Optional[float] = None,
    away_fi_rate: Optional[float] = None,
) -> Optional[float]:
    """
    Return calibrated NRFI probability (0–1) using the trained XGBoost model.
    Returns None if the model is not trained yet (falls back to Poisson in caller).

    home_starter / away_starter dicts accept keys from the live analysis pipeline:
      Savant: barrel_pct_against, hard_hit_pct_against, whiff_pct, avg_fb_velo
      Season: siera, xfip, fip, csw_pct, o_swing_pct, k_pct, bb_pct, gb_pct, hr_fb_pct
    home_fi_rate / away_fi_rate: rolling first-inning run rate (defaults to league avg 0.29)
    home_top3_wrc / away_top3_wrc: avg wRC+ for lineup spots 1-3 (default: 100)
    Missing values default to league-average constants.
    """
    if not _load_model() or _xgb_model is None:
        return None

    pf      = park_factor or _PARK_FACTORS.get(home_team.upper(), 1.0)
    is_dome = 1 if home_team.upper() in _DOME_TEAMS else 0

    def _get(d: dict, *keys: str, default: float = 0.0) -> float:
        """Return first non-None/non-NaN value from dict using any of the given keys."""
        for k in keys:
            val = d.get(k)
            if val is not None and not (isinstance(val, float) and val != val):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return default

    def _scalar(val: Optional[float], default: float) -> float:
        if val is None or (isinstance(val, float) and val != val):
            return default
        return float(val)

    D = FEATURE_DEFAULTS
    # Build feature vector in FEATURES list order (must stay in sync)
    fv = [
        # Rolling first-inning run rate per starter (enrich_nrfi_fi_rates.py)
        _scalar(home_fi_rate, D["home_starter_fi_rate"]),
        _scalar(away_fi_rate, D["away_starter_fi_rate"]),
        # Savant Statcast — starter dict uses _against / _pct_against suffixes
        _get(home_starter, "barrel_pct_against", "barrel_pct",      default=D["home_barrel_pct"]),
        _get(away_starter, "barrel_pct_against", "barrel_pct",      default=D["away_barrel_pct"]),
        _get(home_starter, "hard_hit_pct_against", "hard_hit_pct",  default=D["home_hard_hit_pct"]),
        _get(away_starter, "hard_hit_pct_against", "hard_hit_pct",  default=D["away_hard_hit_pct"]),
        _get(home_starter, "whiff_pct",                             default=D["home_whiff_pct"]),
        _get(away_starter, "whiff_pct",                             default=D["away_whiff_pct"]),
        _get(home_starter, "avg_fb_velo", "avg_velo",               default=D["home_avg_velo"]),
        _get(away_starter, "avg_fb_velo", "avg_velo",               default=D["away_avg_velo"]),
        # Pitcher season stats
        _get(home_starter, "siera",       default=D["home_siera"]),
        _get(home_starter, "xfip",        default=D["home_xfip"]),
        _get(home_starter, "fip",         default=D["home_fip"]),
        _get(home_starter, "csw_pct",     default=D["home_csw_pct"]),
        _get(home_starter, "o_swing_pct", default=D["home_o_swing_pct"]),
        _get(home_starter, "k_pct",       default=D["home_k_pct"]),
        _get(home_starter, "bb_pct",      default=D["home_bb_pct"]),
        _get(home_starter, "gb_pct",      default=D["home_gb_pct"]),
        _get(home_starter, "hr_fb_pct",   default=D["home_hr_fb_pct"]),
        _get(away_starter, "siera",       default=D["away_siera"]),
        _get(away_starter, "xfip",        default=D["away_xfip"]),
        _get(away_starter, "fip",         default=D["away_fip"]),
        _get(away_starter, "csw_pct",     default=D["away_csw_pct"]),
        _get(away_starter, "o_swing_pct", default=D["away_o_swing_pct"]),
        _get(away_starter, "k_pct",       default=D["away_k_pct"]),
        _get(away_starter, "bb_pct",      default=D["away_bb_pct"]),
        _get(away_starter, "gb_pct",      default=D["away_gb_pct"]),
        _get(away_starter, "hr_fb_pct",   default=D["away_hr_fb_pct"]),
        # Lineup and park
        _scalar(home_top3_wrc, D["home_top3_wrc"]),
        _scalar(away_top3_wrc, D["away_top3_wrc"]),
        pf,
        float(is_dome),
    ]

    assert len(fv) == len(FEATURES), f"fv length {len(fv)} != FEATURES length {len(FEATURES)}"
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

    Train/val split (dynamic — adapts as more seasons are added):
      Train: all seasons except the two most recent
      Val:   second-most-recent season  (genuine holdout — disjoint from train)
      Test:  most-recent season         (held out — never seen during training)

      e.g. with 2022-2024: train=2022, val=2023, test=2024
           with 2022-2026: train=2022-2024, val=2025, test=2026

    Calibration:
      Platt scaling on the val fold (skipped when val AUC ≤ 0.52 to
      avoid amplifying noise into an inverted calibration curve).
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
        ("home_barrel_pct",      70, "run: python scripts/enrich_nrfi_savant.py"),
        ("home_whiff_pct",       70, "run: python scripts/enrich_nrfi_savant.py"),
        ("home_k_pct",           70, "BRef fallback (or rebuild dataset)"),
        ("home_fip",             70, "BRef fallback (or rebuild dataset)"),
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

    # Dynamic season split — adapts as more seasons are added to the dataset.
    # Always keeps the two most recent seasons out of training to prevent leakage.
    all_seasons  = sorted(df_clean["season"].unique())
    if len(all_seasons) < 3:
        raise ValueError(f"Need ≥ 3 seasons to split train/val/test; found: {all_seasons}")
    test_season  = int(all_seasons[-1])
    val_season   = int(all_seasons[-2])
    train_seasons = [int(s) for s in all_seasons[:-2]]

    train_df = df_clean[df_clean["season"].isin(train_seasons)].copy()
    val_df   = df_clean[df_clean["season"] == val_season].copy()
    test_df  = df_clean[df_clean["season"] == test_season].copy()

    X_train = train_df[FEATURES].values.astype(np.float32)
    y_train = train_df["nrfi"].values.astype(int)
    X_val   = val_df[FEATURES].values.astype(np.float32)
    y_val   = val_df["nrfi"].values.astype(int)
    X_test  = test_df[FEATURES].values.astype(np.float32)
    y_test  = test_df["nrfi"].values.astype(int)

    train_label = f"{train_seasons[0]}–{train_seasons[-1]}" if len(train_seasons) > 1 else str(train_seasons[0])
    print(f"\nTrain: {len(X_train)} games ({train_label})")
    print(f"Val:   {len(X_val)} games ({val_season})")
    print(f"Test:  {len(X_test)} games ({test_season} held-out)")

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

    # Platt scaling — only apply when val AUC shows real discrimination.
    # When val AUC ≈ 0.50 (random), Platt fits a noise relationship on 2023
    # and applies it in the wrong direction on 2024, producing the inverted
    # calibration curve (predicted 14% → actual 55%).  Skip it in that case.
    raw_val_probs = model.predict_proba(X_val)[:, 1]
    val_auc       = roc_auc_score(y_val, raw_val_probs)
    if val_auc > 0.52:
        calibrator = LogisticRegression(C=1.0, solver="lbfgs")
        calibrator.fit(raw_val_probs.reshape(-1, 1), y_val)
        print(f"\nPlatt scaling fitted on {val_season} val fold  (val AUC: {val_auc:.4f})")
    else:
        calibrator = None
        print(f"\nSkipping Platt scaling — val AUC {val_auc:.4f} ≈ random; "
              f"Platt would amplify noise rather than correct it. Using raw XGBoost probs.")

    # Test-set evaluation (2024 — never seen)
    raw_test_probs = model.predict_proba(X_test)[:, 1]
    if calibrator is not None:
        cal_test_probs = calibrator.predict_proba(raw_test_probs.reshape(-1, 1))[:, 1]
    else:
        cal_test_probs = raw_test_probs

    brier_raw = brier_score_loss(y_test, raw_test_probs)
    brier_cal = brier_score_loss(y_test, cal_test_probs)
    auc       = roc_auc_score(y_test, cal_test_probs)
    ll        = log_loss(y_test, cal_test_probs)

    # Accuracy at 0.5 threshold and at calibrated "BET" threshold (55%)
    # 65% was too high; 57% produced only ~20 tagged games (too thin to validate).
    # 55% captures top ~10% of predictions, giving ~100-150 games/season volume.
    BET_THRESH = 0.55
    preds_50  = (cal_test_probs >= 0.5).astype(int)
    preds_bet = cal_test_probs >= BET_THRESH
    acc_50    = (preds_50 == y_test).mean()
    n_65      = preds_bet.sum()
    acc_65    = (y_test[preds_bet] == 1).mean() if n_65 > 0 else float("nan")

    print(f"\n{'='*50}")
    print(f"{test_season} HOLD-OUT TEST RESULTS")
    print(f"{'='*50}")
    print(f"  Brier score (raw):        {brier_raw:.4f}  (lower=better; 0.25=random)")
    print(f"  Brier score (calibrated): {brier_cal:.4f}")
    print(f"  AUC-ROC:                  {auc:.4f}  (0.5=random, 1.0=perfect)")
    print(f"  Log-loss:                 {ll:.4f}")
    print(f"  Accuracy @ 50% threshold: {acc_50*100:.1f}%  (on {len(y_test)} games)")
    print(f"  Accuracy @ {BET_THRESH*100:.0f}% threshold: {acc_65*100:.1f}%  (on {n_65} games tagged BET)")
    print(f"  Base NRFI rate {test_season}:      {y_test.mean()*100:.1f}%")
    print(f"{'='*50}")

    if auc < 0.52:
        print("\nWARNING: AUC near random — model may not have learned real signal.")
        print("Consider: more seasons, additional features (weather, lineup), or")
        print("          check whether FanGraphs data is matching correctly.")
    elif acc_65 >= 0.57:
        print(f"\n✓ Model clears {BET_THRESH*100:.0f}% accuracy at BET threshold — edge is real.")
    elif acc_65 >= 0.54:
        print(f"\n~ 54-57% at BET threshold — modest edge over base rate, keep collecting data.")
    else:
        print(f"\n✗ <54% at BET threshold — base rate beats the filter. Do not use.")

    # Calibration curve — checks if predicted probabilities match actual frequencies
    print(f"\n{'='*50}")
    print(f"CALIBRATION CURVE ({test_season} hold-out)")
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


def validate(dataset_path: Path = DATASET_PATH) -> None:
    """
    Walk-forward (expanding-window) validation across all available seasons.

    For each season s (except the first), trains on all prior seasons and
    predicts season s — no future data ever leaks into the prediction.

    With 2022-2026 this produces 4 folds:
      Train: 2022          → Predict: 2023
      Train: 2022-2023     → Predict: 2024
      Train: 2022-2024     → Predict: 2025
      Train: 2022-2025     → Predict: 2026

    Combines all out-of-sample predictions (~8,000+ games) to give a
    statistically meaningful accuracy estimate at each betting threshold.
    """
    try:
        import xgboost as xgb
        from sklearn.metrics import roc_auc_score
        import pandas as pd
        import scipy.stats as stats
    except ImportError as e:
        print(f"ERROR: {e}\nInstall: pip install xgboost scikit-learn pandas scipy")
        return

    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}")
        print("Run: python scripts/build_nrfi_dataset.py")
        return

    df = pd.read_csv(dataset_path, encoding="latin-1")
    print(f"Loaded {len(df)} rows")

    key_cols = ["home_fip", "home_k_pct", "away_fip", "away_k_pct"]
    df_clean = df.dropna(subset=key_cols, how="all").copy()
    for feat in FEATURES:
        if feat in df_clean.columns:
            df_clean[feat] = df_clean[feat].fillna(FEATURE_DEFAULTS.get(feat, 0.0))
        else:
            df_clean[feat] = FEATURE_DEFAULTS.get(feat, 0.0)

    all_seasons = sorted(df_clean["season"].unique())
    if len(all_seasons) < 2:
        print("Need ≥ 2 seasons for walk-forward validation.")
        return

    print(f"\n{'='*60}")
    print(f"WALK-FORWARD VALIDATION  ({int(all_seasons[0])}–{int(all_seasons[-1])})")
    print(f"{'='*60}")
    print(f"  {'Fold':<4}  {'Train':>14}  {'Test':>6}  {'Games':>6}  "
          f"{'Base%':>6}  {'@55%':>5}  {'Acc%':>5}")
    print(f"  {'-'*60}")

    all_probs:   list[float] = []
    all_labels:  list[int]   = []

    for i, test_season in enumerate(all_seasons[1:], start=1):
        train_seasons = [s for s in all_seasons if s < test_season]
        train_df = df_clean[df_clean["season"].isin(train_seasons)]
        test_df  = df_clean[df_clean["season"] == test_season]

        X_tr = train_df[FEATURES].values.astype(np.float32)
        y_tr = train_df["nrfi"].values.astype(int)
        X_te = test_df[FEATURES].values.astype(np.float32)
        y_te = test_df["nrfi"].values.astype(int)

        # Use last training season as val for early stopping
        last_train = train_seasons[-1]
        val_mask = train_df["season"] == last_train
        X_val_es = train_df[val_mask][FEATURES].values.astype(np.float32)
        y_val_es = train_df[val_mask]["nrfi"].values.astype(int)

        model = xgb.XGBClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
            gamma=1.0,
            scale_pos_weight=(y_tr == 0).sum() / max(1, (y_tr == 1).sum()),
            eval_metric="logloss", early_stopping_rounds=30,
            random_state=42,
        )
        model.fit(X_tr, y_tr, eval_set=[(X_val_es, y_val_es)], verbose=False)

        probs = model.predict_proba(X_te)[:, 1]
        all_probs.extend(probs.tolist())
        all_labels.extend(y_te.tolist())

        n_bet = (probs >= 0.55).sum()
        acc_bet = y_te[probs >= 0.55].mean() * 100 if n_bet > 0 else float("nan")
        train_label = (f"{int(train_seasons[0])}–{int(train_seasons[-1])}"
                       if len(train_seasons) > 1 else str(int(train_seasons[0])))
        print(f"  {i:<4}  {train_label:>14}  {int(test_season):>6}  "
              f"{len(y_te):>6}  {y_te.mean()*100:>5.1f}%  "
              f"{n_bet:>5}  {acc_bet:>4.1f}%")

    all_probs  = np.array(all_probs)
    all_labels = np.array(all_labels)

    print(f"\n  Overall AUC (all folds): {roc_auc_score(all_labels, all_probs):.4f}")

    print(f"\n{'='*60}")
    print(f"THRESHOLD ANALYSIS  (combined out-of-sample)")
    print(f"{'='*60}")
    print(f"  {'Thresh':>7}  {'Games':>6}  {'%Total':>7}  {'WinRate':>8}  "
          f"{'Edge':>6}  {'95% CI':>16}  {'p-val':>7}")
    print(f"  {'-'*60}")

    BREAKEVEN = 0.524  # -110 juice
    for thresh in [0.52, 0.53, 0.54, 0.55, 0.56, 0.57, 0.58]:
        mask = all_probs >= thresh
        n = mask.sum()
        if n < 10:
            continue
        wr = all_labels[mask].mean()
        edge = wr - BREAKEVEN
        se = np.sqrt(wr * (1 - wr) / n)
        ci_lo = wr - 1.96 * se
        ci_hi = wr + 1.96 * se
        # one-tailed z-test: H0 = win rate ≤ breakeven
        z = (wr - BREAKEVEN) / np.sqrt(BREAKEVEN * (1 - BREAKEVEN) / n)
        p = 1 - stats.norm.cdf(z)
        sig = "✓ sig" if p < 0.05 else ("~ marginal" if p < 0.15 else "")
        pct_total = n / len(all_labels) * 100
        print(f"  {thresh*100:>6.0f}%  {n:>6}  {pct_total:>6.1f}%  "
              f"{wr*100:>7.1f}%  {edge*100:>+5.1f}pp  "
              f"[{ci_lo*100:.1f}%–{ci_hi*100:.1f}%]  {p:>6.4f}  {sig}")

    print(f"\n  Total out-of-sample games: {len(all_labels)}")
    print(f"  Base NRFI rate: {all_labels.mean()*100:.1f}%")
    print(f"  Breakeven at -110: {BREAKEVEN*100:.1f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train",    action="store_true", help="Train the production model")
    parser.add_argument("--validate", action="store_true",
                        help="Walk-forward validation across all seasons — shows real edge at each threshold")
    parser.add_argument("--dataset",  type=Path, default=DATASET_PATH,
                        help="Path to the CSV from build_nrfi_dataset.py")
    args = parser.parse_args()

    if args.train:
        train(args.dataset)
    elif args.validate:
        validate(args.dataset)
    else:
        parser.print_help()
