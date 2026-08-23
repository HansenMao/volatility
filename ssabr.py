from pysabr import Hagan2002LognormalSABR as sabr
from pysabr import black
from scipy.stats import norm
from scipy.optimize import fsolve
import numpy as np
import math
from common_functions import *


class SSabr:
    # this parameter should now be redundant
    strike_scale = 1
    in_sabr=None

    def __init__(self,f=None,shift=None,t=None,atmv=None,rho=None,volvol=None,beta=None):
        if f is None:
            self.f = 1
        else:
            self.f = f
        if shift is None:
            self.shift = 0
        else:
            self.shift = shift
        if t is None:
            self.t = 1
        else:
            self.t = t
        if atmv is None:
            self.atmv = 0.1
        else:
            self.atmv = atmv
        if rho is None:
            self.rho = 0
        else:
            self.rho = rho
        if volvol is None:
            self.volvol = 1
        else:
            self.volvol = volvol
        if beta is None:
            self.beta = 1
        else:
            self.beta = beta
        self.in_sabr=sabr(f=self.f,beta=self.beta,shift=self.shift,t=self.t,v_atm_n=self.atmv,rho=self.rho,volvol=self.volvol)
        #sabr(f=1/self.strike_scale,shift=0,t=t,beta=1)


    def getStrDiffFromRR (self,rr,st,hi_vol,at_vol,dlta,t,if_adjust,to_calibrate=0):
        # hi_vol is high strike vol. hi_vol + rr is the low strike vol
        # dlta is either 25 delta or 10 delta etc. in absolute value
        # use that to get 3 points, fit sabr, and value market strangle based on sabr
        # ? compare that with market strangle and return the difference? ( for solve 0 )
        low_vol=hi_vol-rr
        #hi_strike=getStrikeFromDelta(dlta,hi_vol,t)
        #low_strike=getStrikeFromDelta(-1*dlta,low_vol,t)
        hi_strike=getAdjustedStrikeFromDelta(dlta,hi_vol,t,1,if_adjust)
        low_strike=getAdjustedStrikeFromDelta(-1*dlta,low_vol,t,0,if_adjust)

        #dn_strike=getAdjustedStrikeFromDelta(0.5,at_vol,t,1,if_adjust)
        dn_strike=getDNStrike(at_vol,t,if_adjust)

        k = np.array([low_strike,dn_strike,hi_strike])/self.strike_scale
        v_sln = np.array([low_vol,at_vol,hi_vol])*100
        my_sabr=sabr(f=1/self.strike_scale,shift=0,t=t,beta=1)
        my_sabr.fit(k,v_sln)
        if to_calibrate:
            self.in_sabr=sabr(f=1/self.strike_scale,shift=0,t=t,beta=1)
            self.in_sabr.fit(k,v_sln)
        #str_hi_strike=getStrikeFromDelta(dlta,at_vol+st,t)
        #str_low_strike=getStrikeFromDelta(-1*dlta,at_vol+st,t)
        str_hi_strike=getAdjustedStrikeFromDelta(dlta,at_vol+st,t,1,if_adjust)
        str_low_strike=getAdjustedStrikeFromDelta(-1*dlta,at_vol+st,t,0,if_adjust)
        # sabr log normal vol convert before it can be compared to atm vol??
        str_hi_sabr_vol=my_sabr.lognormal_vol(str_hi_strike/self.strike_scale)
        str_low_sabr_vol=my_sabr.lognormal_vol(str_low_strike/self.strike_scale)
        #print (str_hi_sabr_vol,str_low_sabr_vol,at_vol+st)
        sabr_call_prem=black.lognormal_call(str_hi_strike,1,t,str_hi_sabr_vol,0,'call')
        sabr_put_prem=black.lognormal_call(str_low_strike,1,t,str_low_sabr_vol,0,'put')
        sabr_prem=sabr_call_prem+sabr_put_prem
        market_call_prem=black.lognormal_call(str_hi_strike,1,t,at_vol+st,0,'call')
        market_put_prem=black.lognormal_call(str_low_strike,1,t,at_vol+st,0,'put')
        market_prem=market_call_prem+market_put_prem
        prem_diff = sabr_prem - market_prem
        return prem_diff

    def solveSabrFromMarket(self,rr,st,at_vol,dlta,t,if_adjust):
        # return caliberated rho and SDLogVol
        func = lambda x: self.getStrDiffFromRR (rr,st,x,at_vol,dlta,t,if_adjust)
        #if rr > 0:
        #    initial_guess = at_vol * 0.99
        #else:
        #    initial_guess = at_vol * 1.01
        initial_guess = at_vol + rr/2 + st
        #initial_guess = at_vol + rr / 2
        hi_vol = fsolve(func,initial_guess)[0]
        self.getStrDiffFromRR(rr,st,hi_vol,at_vol,dlta,t,if_adjust,1)
        #return [self.rho,self.volvol*math.sqrt(t)]
        return [self.in_sabr.rho,self.in_sabr.volvol*math.sqrt(t)]

    def getStrikeVol(self,K):
        res=self.in_sabr.lognormal_vol(K)
        return res

    def getDNVol(self,t,if_adjust):
        v_e = self.in_sabr.lognormal_vol(1)
        dn_s = getDNStrike(v_e, t, if_adjust)
        v = self.in_sabr.lognormal_vol(dn_s)
        return v

    def getStrikeFromDelta(self,d,t,if_call,if_adjust):
        v=self.getDNVol(t,if_adjust)
        i=0
        while i<10:
            s=getAdjustedStrikeFromDelta(d,v,t,if_call,if_adjust)
            v=self.in_sabr.lognormal_vol(s)
            i=i+1
        return (s,v)

    def getRR(self,d,t,if_adjust):
        (cs,cv)=self.getStrikeFromDelta(d,t,1,if_adjust)
        (ps,pv)=self.getStrikeFromDelta(-1*d,t,0,if_adjust)
        return cv-pv

    def get_ST_PV_diff(self,d,t,s,if_adjust):
        atv=self.getDNVol(t,if_adjust)
        v=atv+s
        cs=getAdjustedStrikeFromDelta(d,v,t,1,if_adjust)
        ps=getAdjustedStrikeFromDelta(-1*d,v,t,0,if_adjust)
        cv=self.getStrikeVol(cs)
        pv=self.getStrikeVol(ps)
        sabr_pv=getBSPV(1,cs,cv,t,1)+getBSPV(1,ps,pv,t,0)
        market_pv=getBSPV(1,cs,v,t,1)+getBSPV(1,ps,v,t,0)
        pv_diff=sabr_pv-market_pv
        return pv_diff

    def getST(self,d,t,if_adjust):
        func = lambda s: self.get_ST_PV_diff(d,t,s,if_adjust)
        res =fsolve(func,0)[0]
        return res



