#!/usr/bin/python3

from cvol import CVol
import math

class CVol_Cor(CVol):
    def __init__(self,sdadd=0,sdecay=50):
        CVol.__init__(self,0,0,0,sdadd,0,0,sdecay)
        self.vp={}

    def load_cparam(self,ri,rf,rr):
        self.ri=ri
        self.rf=rf
        self.rr=rr

    def set_vparam(self,iv,ltv,rv,mr,rc,v_n):
        self.vp[v_n]=[iv,ltv,rv,mr,rc]

    def bbv(self,iv,ltv,rv,mr,rc,t):
        st=ltv-(ltv-iv)*math.exp(-1*mr*t)
        sb=math.sqrt(st**2+2*rc*st*rv+(rv*t)**2)
        return sb

    def v1(self,t):
        res=self.bbv(*self.vp[1],t)
        return res

    def v2(self,t):
        res=self.bbv(*self.vp[2],t)
        return res

    def getCor(self,t):
        c=self.rf-(self.rf-self.ri)*math.exp(-1*self.rr*t)
        return c

    def getBackboneVol(self,t,return_var=False,use_hol_weights=False,event_f=None):
        b1=self.v1(t)
        b2=self.v2(t)
        cor=self.getCor(t)
        sig_b=math.sqrt(b1**2+b2**2-2*cor*b1*b2)+self.ShortInitialAddon * math.exp(-1 * self.ShortInitialDecay * t)
        if use_hol_weights:
            sig_b=sig_b * self.getTimeWeight(t)
        if event_f is not None:
            sig_b=max(sig_b+event_f(t),0)
        sig_v=sig_b**2
        if return_var:
            res=sig_v
        else:
            res=sig_b
        return res


