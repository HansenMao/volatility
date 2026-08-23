#!/usr/bin/python3

from vol import Vol
from vol_cor import Vol_Cor
import pandas as pd

##p aram_file="vol_marks.xlsx"

class Vols:
    def __init__(self,param_file):
        self.vs={}
        self.pairs={}
        self.param_file=param_file
        self.xls=pd.ExcelFile(param_file)
        self.params=pd.read_excel(self.xls,"PARAMS",index_col=0)
        self.config=pd.read_excel(self.xls,"CONFIG")
        for i in self.config['BASE'].dropna():
            prem_adj = (i[0:3] == 'USD')
            self.vs[i]=Vol(prem_adj)
        for i in self.config["COR"].dropna():
            self.vs[i]=Vol_Cor()
            self.pairs[i]=self.config[i].dropna().values.tolist()
            self.tenor_points=self.config['TENORS'].dropna().values.tolist()

    def reload_data(self):
        self.xls=pd.ExcelFile(self.param_file)
        self.params=pd.read_excel(self.xls,"PARAMS",index_col=0)

    def load_vol(self,ccy):
        raw_line=self.params[ccy]
        res_line = raw_line.values
        index_line=raw_line.index
        i = res_line[0]
        l = res_line[1]
        r = res_line[2]
        s = res_line[3]
        c = res_line[5]
        d = res_line[6]
        m = res_line[4]
        if ccy in self.pairs.keys():
            #[i,l,r,s,m,c,d,w]=self.params[ccy].values
            self.vs[ccy].set_cvol(s,d,i,l,m)
            for n in [0,1]:
                tmp_line=self.params[self.pairs[ccy][n]].values
                #[i,l,r,s,m,c,d,w0]=self.params[self.pairs[ccy][n]].values
                i=tmp_line[0]
                l=tmp_line[1]
                r=tmp_line[2]
                m=tmp_line[4]
                c=tmp_line[5]
                self.vs[ccy].load_pair(i,l,r,m,c,n+1)
        else:
            #[i,l,r,s,m,c,d,w]=self.params[ccy].values
            self.vs[ccy].set_cvol(i,l,r,s,m,c,d)
        # create dictionary
        w_dict={}
        for zz in list(range(7, len(res_line))):
            w_dict[index_line[zz]]=res_line[zz]

        #self.vs[ccy].atm_vols.load_events(w)
        self.vs[ccy].atm_vols.load_events(w_dict)
        self.vs[ccy].atm_vols.calc_event_heights()

# obslete and to be replaced by two separate steps
    def load_surf(self,ccy):
        self.vs[ccy].surf_matrix=pd.read_excel(self.xls,ccy)
        self.vs[ccy].fit_sabrs()
        self.vs[ccy].interpolate_sabr()

    def fit_sabr(self,ccy,target_expiries=None):
        self.vs[ccy].surf_matrix=pd.read_excel(self.xls,ccy)
        self.vs[ccy].fit_sabrs(target_expiries)

    def interp_sabr(self,ccy):
        self.vs[ccy].interpolate_sabr()

    def load_vol_all(self):
        for k in self.vs:
            self.load_vol(k)

    def load_surf_all(self):
        for k in self.vs:
            self.load_surf(k)

    def load_all(self):
        self.load_vol_all()
        self.load_surf_all()

    def load_ccy(self,ccy):
        self.load_vol(ccy)
        self.load_surf(ccy)

    def print_tenors(self,ccy):
        self.vs[ccy].atm_vols.print_tenor_points()

