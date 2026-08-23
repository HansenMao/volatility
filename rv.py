from common_functions import *

class RV:

    pct_ladder = [-0.2,-0.15,-0.1,-0.05,0,0.05,0.1,0.15,0.2]
    col_name='fwd'

    def __init__(self):
        self.vol=None # vol object
        self.fwds=None
        self.rv_matrix=None

    def set_vol(self,full_vol):
        self.vol=deepcopy(full_vol)

    def get_fwd(self, input_file, input_tab):
        # expiry,ST 10D,ST 25D,RR 25D,RR 10D
        # assume it's an excel file with tabs
        #xls = pd.ExcelFile(input_file,header=None)
        xls = pd.ExcelFile(input_file)
        self.fwds = pd.read_excel(xls, input_tab,index_col=0)

    def calc(self):
        s=self.fwds[self.col_name].iloc[0]
        #nr=self.fwds.shape[0]
        #nc=len(self.pct_ladder)
        cols = [s*(1 + x) for x in self.pct_ladder]
        #self.rv_matrix=pd.DataFrame(index=range(nr-1),columns=range(nc))
        self.rv_matrix=pd.DataFrame(index=self.fwds.index[1:],columns=cols)
        for i in range(len(self.rv_matrix.index)):
            for j in range (len(cols)):
                t2=self.rv_matrix.index[i]
                K=float(cols[j])
                t1=self.fwds.index[i]
                self.rv_matrix[cols[j]].iloc[i]=self.get_roll(K,t1,t2,1,0)

    def get_vol_rolldown(self,K,t1,t2,f1,f2):
         # t1 < t2
         v0=self.vol.get_vol(K/f2,t2)
         v1=self.vol.get_vol(K/f1,t1)
         return (v1-v0)

    def get_pv_roll(self,K,t1,t2,f1,f2,is_vega,use_pv=1):
         # t1 < t2
         v0 = self.vol.get_vol(K / f2, t2)
         v1 = self.vol.get_vol(K / f1, t1)
         if is_vega:
             if use_pv:
                vega0=getpctBSVega(f2/K,v0,t2)
                vega1=getpctBSVega(f1/K,v1,t1)
                vega_avg=(vega0+vega1)/2
                res=vega_avg*(v1-v0)
             else:
                 res=v1-v0
         else: # delta roll, assuming no delta exchange. and only use pv. carry is from actual delta not delta exchange
            is_call = K>=f2
            d0=getAdjustedDeltaFromStrike(f2/K,v0,t2,is_call,self.vol.delta_adjust)
            d1=getAdjustedDeltaFromStrike(f1/K,v1,t1,is_call,self.vol.delta_adjust)
            d_avg=(d0+d1)/2
            fwd0=f2
            fwd1=f1
            res=d_avg*(fwd1-fwd0)
         return res

    def get_roll(self,K,t1_str,t2_str,is_vega,use_pv=1):
        f1=self.fwds[self.col_name][t1_str]
        f2=self.fwds[self.col_name][t2_str]
        t1 = get_years_time(t1_str)
        t2 = get_years_time(t2_str)
        res=self.get_pv_roll(K,t1,t2,f1,f2,is_vega,use_pv)
        # annualize
        res=res/(t2-t1)
        return res










