"""
Logistic regression / gradient boosting calibration layer.
Only trains when 100+ matches with outcomes exist; below that threshold
the Elo/Glicko/Poisson baseline is more reliable than a trained model.
"""
from __future__ import annotations
from typing import Optional

from db.database import get_db

MIN_SAMPLES = 100
MODEL_PATH_DEFAULT = "ml_model.json"

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV
    import numpy as np
    import joblib
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False


# Sport-specific feature schemas.
# Signal names must match what log_signal() records in each sport's pipeline.
# Missing signals default to 0.0 at training/prediction time.
BASEBALL_FEATURES = [
    "wrc_plus", "starter_fip", "bullpen_fip", "park_factor",
    "platoon_adj", "starter_avg_ip", "home_boost", "elo_rating",
    "wind_factor", "temp_factor", "is_dome",
]
TENNIS_FEATURES = [
    "sqi", "rqi", "surface_win_rate", "form_score",
    "rest_days", "glicko2_rating", "surface_amp",
]
SOCCER_FEATURES = [
    "elo_diff", "glicko2_diff", "form_diff",
    "rest_diff", "key_player_out_flag", "neutral_site_flag", "fatigue_flag",
    "home_adv",
]
TABLE_TENNIS_FEATURES = [
    "elo_diff", "glicko2_diff", "form_diff", "fatigue_flag",
]

SPORT_FEATURES: dict[str, list[str]] = {
    "baseball":     BASEBALL_FEATURES,
    "tennis":       TENNIS_FEATURES,
    "soccer":       SOCCER_FEATURES,
    "table_tennis": TABLE_TENNIS_FEATURES,
}

FEATURE_SCHEMA_VERSION = "2025-06-v2"


def _feature_list(sport: Optional[str]) -> list[str]:
    """Return feature list for a sport, or union of all sports if None."""
    if sport and sport in SPORT_FEATURES:
        return SPORT_FEATURES[sport]
    seen: set[str] = set()
    union: list[str] = []
    for feats in SPORT_FEATURES.values():
        for f in feats:
            if f not in seen:
                seen.add(f)
                union.append(f)
    return union


def _gather_training_data(sport: Optional[str] = None) -> tuple:
    feat_names = _feature_list(sport)
    with get_db() as conn:
        query = """
            SELECT p.match_id, p.prob_a, o.result, m.sport
            FROM predictions p
            JOIN outcomes o ON p.match_id = o.match_id
            JOIN matches m  ON m.id = p.match_id
            WHERE p.method = 'model_v1'
        """
        params: list = []
        if sport:
            query += " AND m.sport = ?"
            params.append(sport)
        rows = conn.execute(query, params).fetchall()
        if not rows:
            return [], []

        X, y = [], []
        for row in rows:
            mid = row["match_id"]
            sigs = conn.execute(
                "SELECT signal_name, signal_value FROM signals WHERE match_id=?", (mid,)
            ).fetchall()
            sig_map = {s["signal_name"]: s["signal_value"] for s in sigs}
            features = [sig_map.get(f, 0.0) or 0.0 for f in feat_names]
            X.append(features)
            y.append(1 if row["result"] == "a" else 0)
    return X, y


def train(model_path: str = MODEL_PATH_DEFAULT, use_gbm: bool = False,
          sport: Optional[str] = None) -> dict:
    if not _HAS_SKLEARN:
        return {"error": "scikit-learn not installed. Run: pip install scikit-learn joblib"}

    X, y = _gather_training_data(sport)
    if len(X) < MIN_SAMPLES:
        return {
            "error": f"Insufficient data: {len(X)} samples (minimum {MIN_SAMPLES}). "
                     "Using Elo/Glicko/Poisson baseline until more outcomes are recorded."
        }

    import numpy as np
    X_arr = np.array(X)
    y_arr = np.array(y)

    base = GradientBoostingClassifier(n_estimators=100) if use_gbm else LogisticRegression(max_iter=500)
    model = CalibratedClassifierCV(base, cv=5)
    model.fit(X_arr, y_arr)
    joblib.dump(model, model_path)
    return {
        "status": "trained",
        "samples": len(X),
        "model_path": model_path,
        "sport": sport or "all",
        "features": _feature_list(sport),
        "schema_version": FEATURE_SCHEMA_VERSION,
    }


def predict(features: dict, model_path: str = MODEL_PATH_DEFAULT,
            sport: Optional[str] = None) -> Optional[float]:
    if not _HAS_SKLEARN:
        return None
    try:
        import joblib, numpy as np
        model = joblib.load(model_path)
        feat_names = _feature_list(sport)
        feat_vec = np.array([[features.get(f, 0.0) or 0.0 for f in feat_names]])
        return float(model.predict_proba(feat_vec)[0][1])
    except Exception:
        return None
