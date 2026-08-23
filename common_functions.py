import pandas as pd
import numpy as np
import datetime
from copy import deepcopy
import math
from scipy.integrate import quad
from scipy.optimize import minimize,fsolve
from scipy.stats import norm
from pysabr import black
from datetime import datetime,date,timedelta,time
from pandas.tseries.offsets import DateOffset
import holidays

# overwrite can only happen at tenor points
#tenor_points = ["1w","2w","3w","1m","2m","3m","6m","1y","18m","2y","3y","4y","5y","7y","10y"]
#tenor_points = ["1w","2w","3w","1m","2m","3m","6m","9m","1y","18m","2y"]
tenor_points = ["1w","2w","3w","1m","2m","3m","6m","9m","1y"]

def five_point_interpolate_straight(xs,ys):
        # a_i+b_i*[rho_i*(x-m_i)+|x-m_i|], i=1,2,3. assume three a_i and rho_i are all equal
        # analytical
        [m0,m1,m2,m3,m4]=xs
        [y0,y1,y2,y3,y4]=ys
        l1=(y4-y3)/(m4-m3)
        l2=(y3-y2)/(m3-m2)
        l3=(y2-y1)/(m2-m1)
        l4=(y1-y0)/(m1-m0)
        b3=(l1-l2)/2
        b2=(l2-l3)/2
        b1=(l3-l4)/2
        rho=(l1+l4)/(l1-l4)
        a=(y2-b3*(m2-m3)*(rho-1)-b1*(m2-m1)*(rho+1))/3
        return [b1,b2,b3,rho,a]

def svi_var_in_lnK(lnK,a,b,sigma,rho,m):
        res = a+b*(rho*(lnK-m)+math.sqrt((lnK-m)**2+sigma**2))
        return res

def svi3_var_in_lnK(lnK,a1,a2,a3,b1,b2,b3,s1,s2,s3,r1,r2,r3,m1,m2,m3):
        res=svi_var_in_lnK(lnK,a1,b1,s1,r1,m1)
        res=res+svi_var_in_lnK(lnK,a2,b2,s2,r2,m2)
        res=res+svi_var_in_lnK(lnK,a3,b3,s3,r3,m3)
        return res

def svi3_diff(xs,ys,a1,a2,a3,b1,b2,b3,s1,s2,s3,r1,r2,r3,m1,m2,m3):
        res=0
        for i in range(5):
            y_exp_i=svi3_var_in_lnK(xs[i],a1,a2,a3,b1,b2,b3,s1,s2,s3,r1,r2,r3,m1,m2,m3)
            res=res+10000*(ys[i]-y_exp_i)**2
        return res

def solve_svi3(xs,ys,default_sigma=0.6):
        # xs and ys are 5 points
        s=default_sigma*(xs[3]-xs[1])
        func = lambda z: svi3_diff(xs,ys,z[0],z[1],z[2],z[3],z[4],z[5],s,s,s,z[6],z[7],z[8],z[9],z[10],z[11])
        [b1,b2,b3,rho,a]=five_point_interpolate_straight(xs,ys)
        res=minimize (func,(a,a,a,b1,b2,b3,rho,rho,rho,xs[1],xs[2],xs[3]))
        [a1,a2,a3,b1,b2,b3,r1,r2,r3,m1,m2,m3]=res.x
        #return res.x
        return [a1,a2,a3,b1,b2,b3,s,s,s,r1,r2,r3,m1,m2,m3]


def get_years_time(time_str):
        #1W,1M,12Y type of string
        #simple manual divide, not using date object
        time_unit=time_str[-1]
        time_number=int(time_str[0:-1])
        res=time_number
        if time_unit=='W' or time_unit == 'w':
            #res=res/52
            res=res/52.0345
        elif time_unit=='M' or time_unit == 'm':
            #res=res/12
            res=res/12.0079726
        return res


def get_inst_x(t,init_x,end_x,decay):
        #res=init_x+(end_x-init_x)*math.exp(-1*decay*t)
        res = end_x - (end_x - init_x) * math.exp(-1 * decay * t)
        return res

def get_int_x(t,init_x,end_x,decay):
        int_val=quad(get_inst_x,0,t,args=(init_x,end_x,decay))[0]
        return int_val

def get_abs_diff(init_x,end_x,decay,x,use_integral=True):
        res=0
        for r in x:
            if use_integral:
                x_exp=get_int_x(r[0],init_x,end_x,decay)
            else:
                x_exp=get_inst_x(r[0],init_x,end_x,decay)
            res=res+(x_exp-r[1])**2
        #diff of shorter and longer expiries are treated the same. Any reason for weighting?
        return math.sqrt(res)

# get the forward / spot delta adjustment for the below 2 functions
def getDeltaFromStrike (SK,vol,t):
        # SK is S/K ratio
        # default forward delta
        d1=(np.log(SK)+(vol*vol/2)*t)/(vol*math.sqrt(t))
        nd1=norm.cdf(d1)
        if SK<=1:
            delta= nd1
        else:
            delta= nd1-1
        return delta

def getDeltaFromCallStrike(SK,vol,t):
    d1 = (np.log(SK) + (vol * vol / 2) * t) / (vol * math.sqrt(t))
    nd1=norm.cdf(d1)
    return nd1

def getAdjustedDeltaFromStrike(SK,vol,t,if_call,if_adjust):
    d1 = (np.log(SK) + (vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol*math.sqrt(t)
    if if_adjust:
        N = 1/SK
        nd=norm.cdf(d2)
    else:
        N = 1
        nd = norm.cdf(d1)
    if if_call:
        res = N*nd
    else:
        res = N*(nd-1)
    return res

def getpctBSGamma(SK,vol,t):
    d1=(np.log(SK) + (vol * vol / 2) * t) / (vol * math.sqrt(t))
    res=math.exp(-0.5*d1*d1)/(vol*math.sqrt(2*math.pi*t))
    return res

def getpctBSVega(SK,vol,t):
    d1 = (np.log(SK) + (vol * vol / 2) * t) / (vol * math.sqrt(t))
    res=math.exp(-0.5*d1*d1)*(math.sqrt(t/(2*math.pi)))
    return res

def getAdjustedStrikeFromDelta ( delta, vol, t,if_call,if_adjust):
    initial_guess=1
    func = lambda x: getAdjustedDeltaFromStrike(x,vol,t,if_call,if_adjust)-delta
    res=fsolve(func,initial_guess)[0]
    return 1/res

def getDNStrike ( vol, t, if_adjust):
    initial_guess=1
    func = lambda x: getAdjustedDeltaFromStrike(x,vol,t,1,if_adjust)+getAdjustedDeltaFromStrike(x,vol,t,0,if_adjust)
    res=fsolve(func,initial_guess)[0]
    return 1/res

def getStrikeFromDelta (delta,vol,t):
        # returns ratio of K/S based on black formula
        # deltas is in (-1,1), and is default to forward delta
        if delta>0:
            initial_guess = 0.99
        else:
            initial_guess = 1.01
        func = lambda x: getDeltaFromStrike(x,vol,t)-delta
        res=fsolve(func,initial_guess)[0]
        return 1/res

def getBSPV(S,K,vol,t,if_call,foreign_prem=False):
        d1 = (np.log(S/K) + (vol * vol / 2) * t) / (vol * math.sqrt(t))
        d2 = d1-vol*math.sqrt(t)
        if if_call:
            pv = norm.cdf(d1)*S-norm.cdf(d2)*K
        else:
            pv = norm.cdf(-1*d2)*K-norm.cdf(-1*d1)*S
        if foreign_prem:
            pv = pv / S
        return pv

def getBSPVDigi(S,K,vol,t,if_call,foreign_prem=False):
    d1=(np.log(S/K) + (vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2=d1-vol*math.sqrt(t)
    if if_call:
        pv = norm.cdf(d2)
    else:
        pv = 1 - norm.cdf(d2)
    if foreign_prem:
        if if_call:
            pv = norm.cdf(d1)
        else:
            pv = 1 - norm.cdf(d1)
    return pv


def min_diff(data_x,x_init,x_final,x_lambda,use_integral=False):
        func = lambda y: get_abs_diff(y[0],y[1],y[2],data_x,use_integral)
        res = minimize (func,(x_init,x_final,x_lambda))
        return res.x

def get_neighbor_tenors(t):
    # t as in a number. return neighbor points in strings [ ) -> left inclusive, right exclusive
    tenors=np.array([get_years_time(x) for x in tenor_points])
    tenor_loc=np.argmax(tenors>t)
    return [tenor_points[tenor_loc-1],tenor_points[tenor_loc]]

def get_inter_point(t1,t2,v1,v2,t,r,method=0):
    # method 0 is for vol, method 1 is for instantaneous interpolate
    # ratio is provided as parameter
    # for method 1 input t1 and t2 are not needed
    if method == 0:
        res=math.sqrt((r*(v2**2*t2-v1**2*t1)+v1**2*t1)/t)
    elif method == 1:
        res=r*(v2-v1)+v1
    else:
        # shouldn't happen
        res=r*(v2-v1)+v1
    return res

def getTargetTime (t):
    # return GMT time given t: time in years from now
    time_diff=timedelta(days=1)*t*365.2425
    cur_time=datetime.utcnow()
    target_time=cur_time+time_diff
    return target_time

def get_time_from_string(t,fmt=0):
    #fmt 0: "%m/%d/%Y"
    #fmt 1: "%m/%d/%Y %H:%M"
    if fmt==1:
        res = datetime.strptime(t, "%m/%d/%Y %H:%M")
        # like 05/31/2021, "%m/%d/%Y"
    else:
        res = datetime.strptime(t,"%m/%d/%Y")
    return res

def get_t_from_datetime(dt):
    dt_now=datetime.utcnow()
    #ts=(dt-dt_now).total_seconds()/31536000
    ts=(dt-dt_now).total_seconds()/31556952
    return ts

def get_t_from_datetime_start(dt):
        dt_now=datetime.utcnow()
        #dt_start=dt_now.replace(hour=22,minute=0,second=0)-timedelta(days=1)
        dt_start=dt_now.replace(hour=22,minute=0,second=0)
        #ts=(dt-dt_start).total_seconds()/31536000
        ts=(dt-dt_start).total_seconds()/31556952
        if ts<0.00001:
                ts=0
        return ts

def get_t_from_string(s):
    dt=get_time_from_string(s)
    t=get_t_from_datetime(dt)
    return t

def get_datetime(s):
    if isinstance(s,(int,float)):
        res=getTargetTime(s)
    elif isinstance(s,datetime):
        res=s
    else:
        res=get_time_from_string(s)
    return res

def getVV(K,T,K1,K2,K3,S1,S2,S3):
    # get model independent strike vol of K, based on 3 strikes K1-K3 and their corresponding Vols S1-S3
    ln = lambda x: math.log(x)
    d1 = lambda x: ln(x)/(S2*math.sqrt(T))+S2*math.sqrt(T)/2
    d2 = lambda x: d1(x)-S2*math.sqrt(T)
    eta1=ln(K2/K)*ln(K3/K)/(ln(K2/K1)*ln(K3/K1))*S1+ln(K/K1)*ln(K3/K)/(ln(K2/K1)*ln(K3/K2))*S2+ln(K/K1)*ln(K/K2)/(ln(K3/K1)*ln(K3/K2))*S3
    D1=eta1-S2
    D2=ln(K2/K)*ln(K3/K)/(ln(K2/K1)*ln(K3/K1))*d1(K1)*d2(K1)*(S1-S2)**2+\
            ln(K/K1)*ln(K/K2)/(ln(K3/K1)*ln(K3/K2))*d1(K3)*d2(K3)*(S3-S2)**2
    res=S2+(-S2+math.sqrt(S2**2+d1(K)*d2(K)*(2*S2*D1+D2)))/(d1(K)*d2(K))
    return res

def isholiday(ccy,dt):
    ih = False
    us_hols = holidays.US()
    uk_hols = holidays.UK()
    jp_hols = holidays.JP()
    ca_hols = holidays.CA()
    nz_hols = holidays.NZ()
    if "USD" in ccy:
        ih = ih or dt in us_hols
    if "GBP" in ccy:
        ih = ih or dt in uk_hols
    if "NZD" in ccy:
        ih = ih or dt in nz_hols
    if "CAD" in ccy:
        ih = ih or dt in ca_hols
    if "JPY" in ccy:
        ih = ih or dt in jp_hols
    #manually add Chinese holidays
    return ih

def getbday(ccy,ds):
    # currency pair and date string, like 1w, 2w, 1m etc.
    ds=ds.strip()
    td=date.today()
    #DEBUG# td=date.today()+timedelta(days=1)
    # get today's settlement date
    if ccy == "USDCAD":
        sd_ofs=1
    else:
        sd_ofs=2
    sd=np.busday_offset(td,sd_ofs)
    while isholiday(ccy,sd.astype(datetime)):
        sd=np.busday_offset(sd,1)
    u=ds[-1:].lower()
    n=int(ds[:-1])
    if u == 'w':
        esd=sd.astype(datetime)+DateOffset(days=n*7)
    elif u == 'm':
        esd=sd.astype(datetime)+DateOffset(months=n)
    if esd.isoweekday() in set((6, 7)):
         #esd+= timedelta(days=(3-esd.isoweekday() % 5))
         esd -= timedelta(days=esd.isoweekday() % 5)
    esd=np.busday_offset(esd.date(),-1*sd_ofs)
    while isholiday(ccy,esd.astype(datetime)):
        esd=np.busday_offset(esd,-1)
    res = datetime.combine(esd.astype(datetime),time())
    return res
