"""
Logistic regression / gradient boosting calibration layer.
Only trains when 50+ matches with outcomes exist; below that threshold
the Elo/Glicko/Poisson baseline is more reliable than a trained model.
"""
from __future__ import annotations
from typing import Optional
import json

from db.database import get_db

MIN_SAMPLES = 50
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


FEATURE_SIGNALS = [
    "elo_diff",
    "glicko2_diff",
    "form_diff",
    "h2h_decayed",
    "rest_diff",
    "key_player_out_flag",
    "neutral_site_flag",
    "fatigue_flag",
]


def _gather_training_data() -> tuple:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.match_id, p.prob_a, o.result
               FROM predictions p
               JOIN outcomes o ON p.match_id = o.match_id
               WHERE p.method = 'model_v1'"""
        ).fetchall()
        if not rows:
            return [], []

        X, y = [], []
        for row in rows:
            mid = row["match_id"]
            sigs = conn.execute(
                "SELECT signal_name, signal_value FROM signals WHERE match_id=?", (mid,)
            ).fetchall()
            sig_map = {s["signal_name"]: s["signal_value"] for s in sigs}
            features = [sig_map.get(f, 0.0) or 0.0 for f in FEATURE_SIGNALS]
            X.append(features)
            y.append(1 if row["result"] == "a" else 0)
    return X, y


def train(model_path: str = MODEL_PATH_DEFAULT, use_gbm: bool = False) -> dict:
    if not _HAS_SKLEARN:
        return {"error": "scikit-learn not installed. Run: pip install scikit-learn joblib"}

    X, y = _gather_training_data()
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
    return {"status": "trained", "samples": len(X), "model_path": model_path}


def predict(features: dict, model_path: str = MODEL_PATH_DEFAULT) -> Optional[float]:
    if not _HAS_SKLEARN:
        return None
    try:
        import joblib, numpy as np
        model = joblib.load(model_path)
        feat_vec = np.array([[features.get(f, 0.0) or 0.0 for f in FEATURE_SIGNALS]])
        return float(model.predict_proba(feat_vec)[0][1])
    except Exception:
        return None
