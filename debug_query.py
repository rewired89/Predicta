"""
Temporary diagnostic script — safe to delete once the "A's vs CHW" query
parsing issue is confirmed fixed. Run with: python debug_query.py
Requires ANTHROPIC_API_KEY set in the environment (or a .env file).
"""
from ai_agent_baseball import parse_baseball_query

query = "A's vs CHW today"

for i in range(10):
    try:
        result = parse_baseball_query(query)
        print(f"[{i}] team_a={result['team_a']!r}  team_b={result['team_b']!r}")
    except Exception as e:
        print(f"[{i}] REJECTED (safety net caught it): {e}")
