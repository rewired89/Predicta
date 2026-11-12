"""Plain-English wording shared by the pages and the emails (no jargon, no raw numbers without meaning)."""

PAIRS = ("EUR_USD", "GBP_USD", "USD_JPY")


def is_fx(symbol: str) -> bool:
    return symbol.upper().replace("/", "_") in PAIRS


def norm_fx(symbol: str) -> str:
    return symbol.upper().replace("/", "_")


def risk_label(daily_vol: float, kind: str = "stock") -> tuple[str, str]:
    """daily_vol as a fraction (0.02 = 2%). Returns (LOW|MEDIUM|HIGH, one plain sentence)."""
    v = daily_vol * 100
    lo, hi = (0.35, 0.6) if kind == "fx" else (1.5, 3.0)
    if v < lo:
        return "LOW", f"Risk is LOW. It usually moves only about {v:.2f}% in a day, so the price should stay fairly calm."
    if v < hi:
        return "MEDIUM", f"Risk is MEDIUM. It usually moves about {v:.2f}% in a day, so expect normal ups and downs."
    return "HIGH", f"Risk is HIGH. It usually moves about {v:.2f}% in a day, so expect big swings. Buy less of it."


def days_word(n: int) -> str:
    return f"{n} day" + ("" if n == 1 else "s")


def finished_check_text(symbol: str, made_on: str, horizon: int, forecast: float, actual: float, guess: float) -> str:
    """Plain result of one finished volatility check (all inputs are fractions)."""
    f, a, g = forecast * 100, actual * 100, guess * 100
    better = abs(f - a) < abs(g - a)
    return (f"{symbol}: on {made_on} Predicta expected it to move about {f:.2f}% a day over the next {days_word(horizon)}. "
            f"It really moved about {a:.2f}% a day. "
            + ("Predicta's guess was closer than just assuming 'same as lately' " if better else "Just assuming 'same as lately' would have been closer this time ")
            + f"(that simple guess said {g:.2f}%).")
