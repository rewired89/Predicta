# Run from repo root: python3 tests/verify_tennis.py  (exact tennis model + Monte Carlo cross-check)
import sys, math, random
from functools import lru_cache
sys.path.insert(0,".")
import analyze_tennis as T

# 1. GAME: closed form vs code vs Monte Carlo
def game_closed(p):  # P(server wins a game) textbook formula
    q=1-p
    return p**4*(1+4*q+10*q*q)+20*p**3*q**3*p*p/(p*p+q*q)
worst=0
for p in (0.5,0.55,0.6,0.65,0.7,0.8):
    worst=max(worst,abs(T._markov_game_prob(p,p,True)-game_closed(p)))
print(f"game: code vs textbook formula, worst gap = {worst:.2e}")
random.seed(1); p=0.62; n=400000; w=0
for _ in range(n):
    a=b=0
    while True:
        if random.random()<p:a+=1
        else:b+=1
        if (a>=4 or b>=4) and abs(a-b)>=2:break
    w+=a>b
print(f"game: Monte Carlo {w/n:.4f} vs code {T._markov_game_prob(p,p,True):.4f}")

# 2. EXACT full match (continuous serve rotation + true tiebreak) vs the code's simplified one
def exact_match(ps,pr,best_of=3):
    need=best_of//2+1
    def tb_prob(a_first):
        @lru_cache(None)
        def dp(a,b):
            if a+b>=60: return 0.5
            if a>=7 and a-b>=2: return 1.0
            if b>=7 and b-a>=2: return 0.0
            k=a+b; srv_first = k==0 or ((k+1)//2)%2==0   # True if first-server serves point k
            a_serves = (srv_first==a_first)
            pw=ps if a_serves else pr
            return pw*dp(a+1,b)+(1-pw)*dp(a,b+1)
        return dp(0,0)
    @lru_cache(None)
    def game(a_srv): return T._markov_game_prob(ps,pr,a_srv)
    @lru_cache(None)
    def match(sa,sb,ga,gb,a_srv):
        if sa==need:return 1.0
        if sb==need:return 0.0
        if ga==6 and gb==6:
            pt=tb_prob(a_srv)
            return pt*match(sa+1,sb,0,0,not a_srv)+(1-pt)*match(sa,sb+1,0,0,not a_srv)
        if ga>=6 and ga-gb>=2: return match(sa+1,sb,0,0,a_srv)
        if gb>=6 and gb-ga>=2: return match(sa,sb+1,0,0,a_srv)
        pg=game(a_srv)
        return pg*match(sa,sb,ga+1,gb,not a_srv)+(1-pg)*match(sa,sb,ga,gb+1,not a_srv)
    return 0.5*match(0,0,0,0,True)+0.5*match(0,0,0,0,False)
print("\nmatch win prob for player A  (code = nested Markov chain; exact = true serve rotation + real tiebreak)")
print(f"{'p_serve':>8} {'p_return':>9} {'code':>8} {'exact':>8} {'gap(pp)':>8}")
mx=0
for ps,pr in ((0.65,0.35),(0.68,0.38),(0.70,0.36),(0.62,0.42),(0.66,0.30),(0.60,0.40),(0.72,0.34)):
    c=T.markov_tennis_match(ps,pr,3)["prob_a"]; e=exact_match(ps,pr,3); mx=max(mx,abs(c-e))
    print(f"{ps:8.2f} {pr:9.2f} {c:8.4f} {e:8.4f} {100*(c-e):8.2f}")
print(f"largest gap: {100*mx:.2f} percentage points")
# 3. Monte Carlo of the exact structure to validate my exact DP
def sim(ps,pr,n=60000,best_of=3):
    need=best_of//2+1; wins=0
    for i in range(n):
        a_srv=(i%2==0); sa=sb=0
        while sa<need and sb<need:
            ga=gb=0
            while True:
                if ga==6 and gb==6:
                    a=b=0; first=a_srv
                    while True:
                        k=a+b; sf = k==0 or ((k+1)//2)%2==0; asv=(sf==first)
                        if random.random()<(ps if asv else pr):a+=1
                        else:b+=1
                        if (a>=7 or b>=7) and abs(a-b)>=2:break
                    if a>b:sa+=1
                    else:sb+=1
                    a_srv=not a_srv;break
                if (ga>=6 or gb>=6) and abs(ga-gb)>=2:
                    if ga>gb:sa+=1
                    else:sb+=1
                    break
                a=b=0
                pw=ps if a_srv else pr
                while True:
                    if random.random()<pw:a+=1
                    else:b+=1
                    if (a>=4 or b>=4) and abs(a-b)>=2:break
                if a>b:ga+=1
                else:gb+=1
                a_srv=not a_srv
        wins+=sa>sb
    return wins/n
print(f"\nvalidation of my exact version at 0.65/0.35: exact DP {exact_match(0.65,0.35):.4f} vs Monte Carlo {sim(0.65,0.35):.4f}")

print("\n--- validate at a lopsided matchup (0.70 / 0.36) ---")
random.seed(11)
print(f"exact DP {exact_match(0.70,0.36):.4f}  vs Monte Carlo (80k matches) {sim(0.70,0.36,80000):.4f}  vs code {T.markov_tennis_match(0.70,0.36,3)['prob_a']:.4f}")
# which simplification causes the gap? exact model, but with the code's tiebreak shortcut (avg point prob)
def exact_with_code_tb(ps,pr):
    pt_avg=(ps+pr)/2
    need=2
    @lru_cache(None)
    def match(sa,sb,ga,gb,a_srv):
        if sa==need:return 1.0
        if sb==need:return 0.0
        if ga==6 and gb==6:
            return pt_avg*match(sa+1,sb,0,0,not a_srv)+(1-pt_avg)*match(sa,sb+1,0,0,not a_srv)
        if ga>=6 and ga-gb>=2: return match(sa+1,sb,0,0,a_srv)
        if gb>=6 and gb-ga>=2: return match(sa,sb+1,0,0,a_srv)
        pg=T._markov_game_prob(ps,pr,a_srv)
        return pg*match(sa,sb,ga+1,gb,not a_srv)+(1-pg)*match(sa,sb,ga,gb+1,not a_srv)
    return 0.5*match(0,0,0,0,True)+0.5*match(0,0,0,0,False)
print(f"exact rotation but code's tiebreak shortcut: {exact_with_code_tb(0.70,0.36):.4f}  -> gap to code {100*(exact_with_code_tb(0.70,0.36)-T.markov_tennis_match(0.70,0.36,3)['prob_a']):+.2f}pp (serve-rotation effect), gap to exact {100*(exact_match(0.70,0.36)-exact_with_code_tb(0.70,0.36)):+.2f}pp (tiebreak effect)")
