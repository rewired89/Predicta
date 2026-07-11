"""
Temporary diagnostic script — safe to delete once the "A's vs CHW" query
parsing issue is confirmed fixed. Run with: python debug_full.py
Requires ANTHROPIC_API_KEY set in the environment (or a .env file).
"""
from env_loader import load_env
load_env()

from analyze_baseball import run_baseball_analysis

result = run_baseball_analysis("A's vs CHW today")
if result.get("error"):
    print("Blocked by safety net:", result["error"])
else:
    print("team_a:", result["team_a"], "| team_b:", result["team_b"])
