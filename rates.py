from scipy.stats import norm
import math
import numpy as np

def PV_normal(F,K,v,t,is_call):
    d1=(F-K)/(v*math.sqrt(t))
    if is_call:
        pv=(F-K)*norm.cdf(d1)+v*math.sqrt(t/(2*math.pi))*math.exp(-1*d1*d1/2)
    else:
        pv=(K-F)*norm.cdf(-1*d1)+v*math.sqrt(t/(2*math.pi))*math.exp(-1*d1*d1/2)
    return pv

def PV_lognormal_shift(F_0,K_0,vol,t,s,if_call):
    S=F_0+s
    K=K_0+s
    d1 = (np.log(S / K) + (vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if if_call:
        pv = norm.cdf(d1) * S - norm.cdf(d2) * K
    else:
        pv = norm.cdf(-1 * d2) * K - norm.cdf(-1 * d1) * S
    return pv

def PV_CF(Fts,K,v,s,use_normal,is_call):
    # use Ft pairs manually as input,as array of pairs
    pv = 0
    for (f,t) in Fts:
        if use_normal:
            pv = pv + PV_normal(f,K,v,t,is_call)
        else:
            pv = pv + PV_lognormal_shift(f,K,v,t,s,is_call)
    return pv

def get_fts():
    # pure helper function
    fs=[0.2912,0.28877,0.28574,0.18928,0.21625,0.23532,0.22853,0.25228,0.26923,0.27255,0.29740,0.35184,0.39101,0.43234,0.47579,0.52844,0.57445,0.61944,0.66490]
    ts=[0.25*n for n in range(1,20)]
    fts=zip(fs,ts)
    return fts