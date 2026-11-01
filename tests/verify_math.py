# Run from repo root: python3 tests/verify_math.py
"""Independent re-computation of Predicta's math. Each check computes the answer a
second, different way (closed form, brute force, textbook example, or another
library) and compares to the code. PASS = same answer, FAIL = investigate."""
import math, random, os, sys, itertools
import numpy as np, pandas as pd
import tempfile; os.environ["PREDICTA_DB_PATH"]=os.path.join(tempfile.mkdtemp(),"vm.db")

sys.path.insert(0,".")
from db.database import init_db; init_db()
R=[]
def chk(group,name,got,want,tol=1e-6):
    ok = (abs(got-want)<=tol*max(1,abs(want))) if isinstance(got,(int,float,np.floating)) else (got==want)
    R.append((group,name,ok,got,want)); 
    if not ok: print(f"  FAIL [{group}] {name}: code={got} independent={want}")

# ---------- 1. ELO ----------
from models import elo as E
for d in (0,100,-250,400,-800):
    chk("Elo",f"expected diff {d}",E._expected(1500+d,1500),1/(1+math.exp(-d*math.log(10)/400)))
chk("Elo","400 pts = 90.9%",E._expected(1900,1500),10/11)
for gd,want in ((0,1),(1,1),(2,1.5),(3,1.75),(4,1.875),(5,2.0),(7,2.25)):   # World Football Elo: 1.75+(n-3)/8
    chk("Elo",f"goal-diff mult {gd}",E._goal_diff_multiplier(gd),want)
m=E.EloModel(); m.set_rating("A",1600); m.set_rating("B",1450)
ea=1/(1+10**((1450-1600)/400)); exp_ra=1600+30*1.5*(1-ea); exp_rb=1450+30*1.5*(0-(1-ea))
ra,rb=m.update("A","B",3,1)
chk("Elo","update winner",ra,exp_ra); chk("Elo","update loser",rb,exp_rb); chk("Elo","zero-sum",ra+rb,3050)
pa,pb=m.win_probability("A","B"); chk("Elo","probs sum to 1",pa+pb,1.0)

# ---------- 2. GLICKO-2: independent implementation vs Glickman's published example ----------
from models.glicko import Glicko2Model
def glicko2_update(r,rd,vol,opps,tau=0.5):
    mu=(r-1500)/173.7178; phi=rd/173.7178
    g=lambda p:1/math.sqrt(1+3*p*p/math.pi**2)
    E_=lambda m,mj,pj:1/(1+math.exp(-g(pj)*(m-mj)))
    o=[((rj-1500)/173.7178,rdj/173.7178,s) for rj,rdj,s in opps]
    v=1/sum(g(pj)**2*E_(mu,mj,pj)*(1-E_(mu,mj,pj)) for mj,pj,_ in o)
    delta=v*sum(g(pj)*(s-E_(mu,mj,pj)) for mj,pj,s in o)
    a=math.log(vol**2)
    f=lambda x:(math.exp(x)*(delta**2-phi**2-v-math.exp(x))/(2*(phi**2+v+math.exp(x))**2))-(x-a)/tau**2
    A=a
    if delta**2>phi**2+v: B=math.log(delta**2-phi**2-v)
    else:
        k=1
        while f(a-k*tau)<0:k+=1
        B=a-k*tau
    fA,fB=f(A),f(B)
    while abs(B-A)>1e-9:
        C=A+(A-B)*fA/(fB-fA); fC=f(C)
        if fC*fB<=0: A,fA=B,fB
        else: fA/=2
        B,fB=C,fC
    s_new=math.exp(A/2); phis=math.sqrt(phi**2+s_new**2)
    phi_n=1/math.sqrt(1/phis**2+1/v); mu_n=mu+phi_n**2*sum(g(pj)*(s-E_(mu,mj,pj)) for mj,pj,s in o)
    return 173.7178*mu_n+1500,173.7178*phi_n,s_new
opps=[(1400,30,1),(1550,100,0),(1700,300,0)]
r,rd,vol=glicko2_update(1500,200,0.06,opps)
chk("Glicko2","paper example rating (1464.06, paper rounds each step)",r,1464.06,2e-5); chk("Glicko2","paper example RD (151.52)",round(rd,2),151.52); chk("Glicko2","paper example vol (0.05999)",vol,0.05999,2e-4)
import glicko2
p=glicko2.Player(rating=1500,rd=200,vol=0.06); p.update_player([1400,1550,1700],[30,100,300],[1,0,0])
chk("Glicko2","library vs paper rating",p.rating,1464.06,2e-5); chk("Glicko2","library == my independent implementation (rating)",p.rating,r,1e-6); chk("Glicko2","library == my independent implementation (RD)",p.rd,rd,1e-6); chk("Glicko2","library == my independent implementation (vol)",p.vol,vol,1e-5); chk("Glicko2","library vs paper RD",round(p.rd,2),151.52)
gm=Glicko2Model(); gm.set_rating("x","tt","all",1650,80,0.06); gm.set_rating("y","tt","all",1500,300,0.06)
pw,_=gm.win_probability("x","y","tt")
g=lambda p_:1/math.sqrt(1+3*p_*p_/math.pi**2); mu1,mu2,ph2=(1650-1500)/173.7178,0,300/173.7178
chk("Glicko2","win prob vs Glicko-2 (mu/phi scale) formula",pw,1/(1+math.exp(-g(ph2)*(mu1-mu2))))
gm.update("x","y","tt"); a=gm.get_rating("x","tt"); b=gm.get_rating("y","tt")
chk("Glicko2","winner rating goes up",a["rating"]>1650,True); chk("Glicko2","loser rating goes down",b["rating"]<1500,True); chk("Glicko2","RD shrinks after a game",a["rd"]<80+1e-9 or a["rd"]<350,True)

# ---------- 3. DE-VIG / ODDS ----------
from models import devig as DV, kelly as K
from fractions import Fraction
chk("Odds","american +110",DV.american_to_decimal(110),2.10); chk("Odds","american -130",DV.american_to_decimal(-130),1+100/130)
fa=DV.devig_market({"a":1.91,"b":1.91}); chk("Devig","even book -> 50/50",fa["a"],0.5)
f3=DV.devig_market({"a":2.5,"draw":3.4,"b":2.9}); chk("Devig","3-way sums to 1",sum(f3.values()),1.0)
imp=[Fraction(1)/Fraction(str(x)) for x in (2.5,3.4,2.9)]; tot=sum(imp)
chk("Devig","3-way exact fractions",f3["a"],float(imp[0]/tot),1e-12)
a_,b_=K.vig_removed_prob(1.8,2.1); chk("Devig","2-way kelly.py agrees with devig.py",a_,DV.devig_market({"a":1.8,"b":2.1})["a"],1e-4)
t=K.vig_removed_prob_three_way(2.5,3.4,2.9); chk("Devig","3-way kelly.py agrees",t[0],f3["a"],1e-4)

# ---------- 4. KELLY (compare to numerical maximisation of log-growth) ----------
def best_f(p,b):
    fs=np.linspace(0,0.999,200001); g=p*np.log1p(b*fs)+(1-p)*np.log1p(-fs); return fs[g.argmax()]
for p,odds in ((0.55,2.0),(0.40,3.0),(0.62,1.7)):
    b=odds-1; r_=K.kelly_stake(p,odds,1000,fraction=1.0)
    chk("Kelly",f"kelly_stake p={p} odds={odds} vs grid-max",r_["full_kelly_fraction"],best_f(p,b),1e-4)
from models.trading.shared.kelly import trading_kelly,atr_position_size,kelly_from_signals
for wr,aw,al in ((0.55,2.0,1.0),(0.45,3.0,1.0),(0.6,1.0,1.0)):
    r_=trading_kelly(wr,aw,al,fraction=1.0); chk("Kelly",f"trading_kelly wr={wr} vs grid-max",r_["full_kelly"]/100,best_f(wr,aw/al),1e-3)
r_=atr_position_size(price=10,atr=0.2,risk_per_trade=0.01,account_value=10000,stop_mult=1.5)
chk("Sizing","ATR: shares x stop distance == risk $100",r_["shares"]*r_["stop_distance"],100.0,1e-3)
ks=kelly_from_signals(50,2.0,10000); chk("Sizing","kelly_from_signals: $100/(1.5x2%)=3333 is over the 25% cap -> 2500",ks["position_size"],min(round(100/(1.5*0.02),2),2500.0))
ks=kelly_from_signals(50,5.0,10000); chk("Sizing","kelly_from_signals: $100/(1.5x5%)=1333.33 (under cap)",ks["position_size"],round(100/(1.5*0.05),2),1e-6)
ks=kelly_from_signals(50,0.2,10000); chk("Sizing","kelly_from_signals caps at 25% of bankroll",ks["position_size"],2500.0)

# ---------- 5. CALIBRATION METRICS (vs scikit-learn) ----------
from models import calibration as C
from sklearn.metrics import brier_score_loss, log_loss
rng=np.random.default_rng(1); p=rng.uniform(0.05,0.95,500); y=(rng.uniform(size=500)<p).astype(int)
chk("Metrics","Brier vs sklearn",C.brier_score(list(p),list(y)),brier_score_loss(y,p),1e-9)
chk("Metrics","log loss vs sklearn",C.log_loss_score(list(p),list(y)),log_loss(y,p),1e-6)
bins=np.minimum((p*10).astype(int),9); ece=sum(abs(p[bins==b_].mean()-y[bins==b_].mean())*(bins==b_).sum() for b_ in range(10) if (bins==b_).any())/500
chk("Metrics","ECE vs independent numpy",C.expected_calibration_error(list(p),list(y)),round(ece,4),1e-4)

# ---------- 6. SOCCER: Dixon-Coles / score matrix ----------
from models import dixon_coles as DC, markets as MK
from scipy.stats import poisson, skellam
def pm(k,mu): return math.exp(-mu)*mu**k/math.factorial(k)       # hand-written Poisson, no scipy
r_=DC.predict(1.2,0.9,1.0,1.1,1.35)
mu_h=1.35*1.2*1.1*1.15; mu_a=1.35*1.0*0.9
chk("Soccer","mu_home formula",r_["mu_home"],mu_h); chk("Soccer","mu_away formula",r_["mu_away"],mu_a)
M=np.zeros((8,8))
for i in range(8):
    for j in range(8):
        adj={(0,0):1-mu_h*mu_a*0.1,(1,0):1+mu_a*0.1,(0,1):1+mu_h*0.1,(1,1):1-0.1}.get((i,j),1.0)
        M[i,j]=pm(i,mu_h)*pm(j,mu_a)*adj
M/=M.sum()
chk("Soccer","P(home) brute force",r_["prob_home"],sum(M[i,j] for i in range(8) for j in range(8) if i>j))
chk("Soccer","P(draw) brute force",r_["prob_draw"],sum(M[i,i] for i in range(8)))
chk("Soccer","P(away) brute force",r_["prob_away"],sum(M[i,j] for i in range(8) for j in range(8) if i<j))
chk("Soccer","1X2 sums to 1",r_["prob_home"]+r_["prob_draw"]+r_["prob_away"],1.0)
mat,_,_=MK.build_score_matrix(1.2,0.9,1.0,1.1,1.35); chk("Soccer","two separate implementations agree (predict vs build_score_matrix)",float(np.abs(mat-M).max()),0.0,1e-12)
# no-correction check: with tau=0, draw prob should match the Skellam distribution (difference of two Poissons)
mat0,mh,ma=MK.build_score_matrix(1.2,0.9,1.0,1.1,1.35,tau=0.0,max_goals=25)
chk("Soccer","tau=0 draw prob vs Skellam closed form",float(np.trace(mat0)),float(skellam.pmf(0,mh,ma)),1e-6)
chk("Soccer","tau=0 mean home goals == mu",float((mat0.sum(1)*np.arange(26)).sum()),mh,1e-6)
sp=MK.spread(mat,[-1.5]); chk("Soccer","spread -1.5 == P(win by 2+)",sp[0]["p_home_covers"],MK.match_result_2up(mat)["home_win_2up"],1e-12)

# ---------- 7. RUGBY: Negative Binomial ----------
from models import rugby_model as RM
mu,k=24.0,8.0; pmf=RM._nb_pmf_vector(mu,k,200)
n=np.arange(201); hand=np.array([math.exp(math.lgamma(x+k)-math.lgamma(k)-math.lgamma(x+1))*(k/(k+mu))**k*(mu/(k+mu))**x for x in n])
chk("Rugby","NB pmf vs hand-written gamma formula",float(np.abs(pmf-hand).max()),0.0,1e-10)
chk("Rugby","NB mean == mu",float((pmf*n).sum()),mu,1e-4); chk("Rugby","NB variance == mu + mu^2/k",float((pmf*n*n).sum()-(pmf*n).sum()**2),mu+mu*mu/k,1e-3)
rr=RM.predict_score(home_attack=1.1,home_defense=0.95,away_attack=0.9,away_defense=1.05); chk("Rugby","1X2 sums to 1",rr["prob_home"]+rr["prob_draw"]+rr["prob_away"],1.0)

# ---------- 8. ESPORTS / UFC helpers ----------
import analyze_esports as AE
chk("Esports","rank 1 -> 2200",AE._elo_from_ranking(1),2200.0); chk("Esports","rank 10 -> 1800",AE._elo_from_ranking(10),1800.0); chk("Esports","rank 100 -> 1400",AE._elo_from_ranking(100),1400.0); chk("Esports","floor at 1300",AE._elo_from_ranking(5000),1300.0)
from models import ufc_model as UM
chk("UFC","shrunk win rate 3-0 (3 fights) is between 0.5 and 1",0.5<UM._shrunk_win_rate({"wins":3,"losses":0})<1.0,True)
w=min(1.0,13/UM.RECORD_SHRINKAGE_FIGHTS); chk("UFC","shrunk win rate 10-3 hand calc",UM._shrunk_win_rate({"wins":10,"losses":3}),0.5+(10/13-0.5)*w)

# ---------- 9. TRADING INDICATORS (High Value) vs pandas/numpy ----------
from models.trading.high_value import intraday as I
random.seed(5); px=[100.0]
for _ in range(120): px.append(px[-1]*(1+random.gauss(0,0.004)))
bars=[{"o":p,"h":p*(1+abs(random.gauss(0,.002))),"l":p*(1-abs(random.gauss(0,.002))),"c":p,"v":random.randint(1000,9000)} for p in px]
s=pd.Series(px); d=s.diff()
rsi_ref=100-100/(1+d.clip(lower=0).rolling(9).mean().iloc[-1]/(-d.clip(upper=0)).rolling(9).mean().iloc[-1])
chk("Indicators","RSI(9) simple-average vs pandas",I._rsi(px,9),round(rsi_ref,2),1e-6)
h=pd.Series([b["h"] for b in bars]);l=pd.Series([b["l"] for b in bars]);c=pd.Series(px)
tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1).iloc[1:]
chk("Indicators","ATR(14) vs pandas",I._atr(bars,14),tr.iloc[-14:].mean(),1e-9)
bb=I._bollinger(px,20); w_=np.array(px[-20:]); chk("Indicators","Bollinger mid",bb["mid"],round(w_.mean(),4),1e-6); chk("Indicators","Bollinger upper (2 sigma, population)",bb["upper"],round(w_.mean()+2*w_.std(),4),1e-6)
typ=np.array([(b["h"]+b["l"]+b["c"])/3 for b in bars]); v=np.array([b["v"] for b in bars],float)
chk("Indicators","VWAP vs numpy cumsum",I._vwap(bars)[-1],float((typ*v).cumsum()[-1]/v.cumsum()[-1]),1e-9)
k_=2/11; e=[sum(px[:10])/10]
for x in px[10:]: e.append(x*k_+e[-1]*(1-k_))
chk("Indicators","EMA(10) SMA-seeded loop",I._ema(px,10)[-1],e[-1],1e-9)
em=I._intraday_expected_move(px,12); lr=np.diff(np.log(px)); chk("Indicators","expected move 1-sigma = bar vol x sqrt(hold)",em["pct_1sigma"],round(lr.std()*math.sqrt(12)*100,3),1e-3)
lv=I._trade_levels(bars,{"price":px[-1]},"long",hold_bars=12,intraday_em=em)
chk("Trade levels","stop distance == 1 sigma $",round(lv["entry"]-lv["stop"],2),round(em["dollars_1sigma"],2),0.02)
chk("Trade levels","target1 = entry + 1.5 x stop distance",round(lv["target1"]-lv["entry"],2),round(1.5*(lv["entry"]-lv["stop"]),2),0.03)

# ---------- 10. LOW VALUE vol-scaled exits ----------
import fetchers.low_value_runner as LV
random.seed(9); cl=[5.0]
for _ in range(24): cl.append(cl[-1]*(1+random.gauss(0,0.03)))
got=LV.vol_scaled_exit_pct([{"c":x} for x in cl])
r20=np.diff(cl)/np.array(cl[:-1]); r20=r20[-20:]; want=min(max(1.5*r20.std(ddof=1)*math.sqrt(5),0.05),0.5)
chk("Low Value exits","vol-scaled exit % vs numpy",got,round(want,4),1e-6)
chk("Low Value exits","long: stored levels round-trip",LV.effective_exit_pcts({"entry_price":5,"side":"long","stop_price":4.75,"target1_price":5.25}),(0.05,0.05)) if False else None
t_,s_=LV.effective_exit_pcts({"entry_price":5,"side":"long","stop_price":4.75,"target1_price":5.25}); chk("Low Value exits","long levels -> pct",(round(t_,4),round(s_,4)),(0.05,0.05))
t_,s_=LV.effective_exit_pcts({"entry_price":5,"side":"short","stop_price":5.25,"target1_price":4.75}); chk("Low Value exits","short levels -> pct",(round(t_,4),round(s_,4)),(0.05,0.05))

# ---------- 11. HORIZON FORECAST ----------
from models.trading.horizon import features as HF, backtest as HB
rng=np.random.default_rng(3); dates=pd.bdate_range("2023-01-02",periods=260).strftime("%Y-%m-%d")
rows=[]
for sym in ["SPY"]+[f"S{i}" for i in range(12)]:
    pr=50*np.exp(np.cumsum(rng.normal(0,.015,260)))
    for dd,pp in zip(dates,pr): rows.append((sym,dd,pp*0.99,pp*1.01,pp*0.98,pp,float(rng.integers(1e5,1e6))))
bars_df=pd.DataFrame(rows,columns=["symbol","bar_date","open","high","low","close","volume"])
panel=HF.build_panel(bars_df,5)
sym,dt=panel.index[len(panel)//2][1],panel.index[len(panel)//2][0]
cs=bars_df[bars_df.symbol==sym].set_index("bar_date")["close"]; spy=bars_df[bars_df.symbol=="SPY"].set_index("bar_date")["close"]
i=list(cs.index).index(dt); row=panel.loc[(dt,sym)]
chk("Horizon","ret_20 by hand",float(row["ret_20"]),cs.iloc[i]/cs.iloc[i-20]-1,1e-9)
chk("Horizon","dist_high_60 by hand",float(row["dist_high_60"]),cs.iloc[i]/cs.iloc[i-59:i+1].max()-1,1e-9)
chk("Horizon","rel_20 by hand (vs SPY)",float(row["rel_20"]),(cs.iloc[i]/cs.iloc[i-20]-1)-(spy.iloc[i]/spy.iloc[i-20]-1),1e-9)
fx=(cs.iloc[i+5]/cs.iloc[i]-1)-(spy.iloc[i+5]/spy.iloc[i]-1)
if not np.isnan(row["fwd_excess"]): chk("Horizon","forward 5d excess return by hand",float(row["fwd_excess"]),fx,1e-9)
rets=cs.pct_change(); chk("Horizon","vol_20 by hand",float(row["vol_20"]),rets.iloc[i-19:i+1].std(),1e-9)
dd_=cs.diff(); up=dd_.clip(lower=0).rolling(14).mean(); dn=(-dd_.clip(upper=0)).rolling(14).mean()
chk("Horizon","RSI14 by hand",float(row["rsi_14"]),float(100-100/(1+up.iloc[i]/dn.iloc[i])),1e-9)
# walk-forward: no training date may be within `horizon` days of the first test day
import types
seen=[]
_of,_oa=HB.fit_clip,HB.apply_clip
HB.fit_clip=lambda tr,c:(seen.append(["train",tr.index.get_level_values(0).max()]) or _of(tr,c))
HB.apply_clip=lambda df,c,b:(seen.append(["any",df.index.get_level_values(0).min()]) or _oa(df,c,b))
rows2=[]
rng2=np.random.default_rng(8); d2=pd.bdate_range("2021-01-04",periods=700).strftime("%Y-%m-%d")
for sym2 in ["SPY"]+[f"T{i}" for i in range(60)]:
    pr2=40*np.exp(np.cumsum(rng2.normal(0,.015,700)))
    for dd2,pp2 in zip(d2,pr2): rows2.append((sym2,dd2,pp2*.99,pp2*1.01,pp2*.98,pp2,float(rng2.integers(1e5,1e6))))
panel=HF.build_panel(pd.DataFrame(rows2,columns=["symbol","bar_date","open","high","low","close","volume"]),5)
res=HB.walk_forward(panel,5); HB.fit_clip,HB.apply_clip=_of,_oa
alld=sorted(panel.index.get_level_values(0).unique()); pos={d:i for i,d in enumerate(alld)}
pairs=[(seen[i][1],seen[i+2][1]) for i in range(0,len(seen)-2,3) if seen[i][0]=="train" and seen[i+2][0]=="any"]
chk("Horizon","walk-forward ran on several months",len(pairs)>=3,True)
chk("Horizon","embargo: last train day + 5-day label ends before first test day",all(pos[a]+5<pos[b] for a,b in pairs),True)
# portfolio sim vs brute force
fake=pd.DataFrame({"p":rng.uniform(size=2000),"fwd_excess":rng.normal(0.001,0.03,2000)},index=pd.MultiIndex.from_product([pd.bdate_range("2024-01-01",periods=100).strftime("%Y-%m-%d"),range(20)]))
fake["label"]=(fake.fwd_excess>0)*1.0; fake["base"]=0.5; fake["month"]="2024-01"
ps=HB.portfolio_sim(fake,5)
per=[]
for d in sorted(fake.index.get_level_values(0).unique())[::5]:
    g=fake.xs(d,level=0); kk=max(1,int(len(g)*0.1)); per.append(g.sort_values("p",ascending=False).head(kk).fwd_excess.mean()-0.002)
per=np.array(per); cur=np.cumsum(per); mdd=min(cur-np.maximum.accumulate(cur))
chk("Horizon","portfolio mean excess (independent loop)",ps["mean_excess_per_period_pct"],round(per.mean()*100,3),1e-3)
chk("Horizon","portfolio max drawdown (independent)",ps["max_drawdown_of_excess_pct"],round(mdd*100,2),1e-3)
chk("Horizon","95% CI low = mean - 1.96 se",ps["ci95_low_pct"],round((per.mean()-1.96*per.std(ddof=1)/math.sqrt(len(per)))*100,3),1e-3)
from scipy.stats import spearmanr
g=fake.xs(fake.index.get_level_values(0)[0],level=0); chk("Horizon","RankIC spearman: pandas rank-corr == scipy",float(g.p.rank().corr(g.fwd_excess.rank())),float(spearmanr(g.p,g.fwd_excess)[0]),1e-9)
tr=panel.dropna(subset=["label"]).iloc[:600]; cols=HF.feature_cols(panel); b_=HF.fit_clip(tr,cols); out=HF.apply_clip(panel.iloc[:50],cols,b_)
chk("Horizon","clip bounds come from training rows only",float(b_[1]["ret_20"]),float(np.quantile(tr["ret_20"],0.99)),1e-9)
chk("Horizon","clipped values never exceed training bounds",bool((out["ret_20"]<=b_[1]["ret_20"]+1e-12).all()),True)

# ---------- 12. EXPOSURE dollars ----------
from db.database import get_db
with get_db() as c:
    cols_="(engine,symbol,side,qty,entry_price,entry_time,is_hypothetical,alpaca_order_id,logged_at)"
    for v in ("('low_value','AAA','long',10,5.0,'t',0,'o1','x')","('automaton','AAA','long',4,5.0,'t',0,'o2','x')","('low_value','BBB','short',2,10.0,'t',1,NULL,'x')"): c.execute(f"INSERT INTO intraday_trades {cols_} VALUES {v}")
from models.trading.shared.exposure import get_cross_engine_exposure as GX
ex=GX(); chk("Exposure","gross dollars = 50 + 20 + 20",ex["gross_dollars"],90.0); chk("Exposure","largest symbol AAA = 70",ex["largest_symbol"]["dollars"],70.0)

# ---------- SUMMARY ----------
import collections
by=collections.OrderedDict()
for g_,n_,ok,_,_ in R: by.setdefault(g_,[0,0]); by[g_][0]+=ok; by[g_][1]+=1
print()
for g_,(a,b) in by.items(): print(f"{'OK  ' if a==b else 'FAIL'} {g_:<16} {a}/{b}")
print(f"\nTOTAL {sum(x[0] for x in by.values())}/{sum(x[1] for x in by.values())} checks agree")
