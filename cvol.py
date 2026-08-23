import math
from scipy.integrate import quad
from datetime import datetime, date, timedelta
import holidays
from common_functions import *


class CVol:
    WeekendWeight = 0.35
    HolidayWeight = 0.5
    HKCut = 3
    TKCut = 6
    NYCut = 14
    TKOpen = 22
    HKGMTDIFF = 8
    DAYSINYEAR = 365.2425
    VolNewsEventDecay = 5000

    # make this part excel based and read from excel file?
    HourlyWeight = [0.83, 0.95, 1.01, 0.79, 0.67, 0.78, 0.85, 1.35, 1.2, 1.13, 0.99, 1.05, 1.17, 1.24, 1.36, 1.55, 1.37,
                    1.2, 0.92, 0.88, 0.75, 0.68, 0.65, 0.63]
    # use python array, and rows are LDN,NY,TOK,LDN+NY,NY+TOK,LDN+TOK
    # ALL row can be omitted as it's all 1
    HourlyMatrix = [[0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0, 0, 0, 0, 0, 0, 0], \
                    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 1, 1, 1, 1, 0, 0, 0], \
                    [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.5, 0.5, 0.5], \
                    [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0], \
                    [1, 1, 1, 0.5, 0.5, 0.5, 0, 0, 0, 0, 0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 1, 1, 1, 1, 0.5, 0.5, 0.5], \
                    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0, 0, 0, 0, 1, 1, 1]]

    us_holidays = holidays.UnitedStates()
    uk_holidays = holidays.UnitedKingdom()
    jp_holidays = holidays.Japan()


    subdiv_limit = 500

    def __init__(self, InitialVol=None, LongTermVol=None, RateVol=None, ShortInitialAddon=None, MeanReversionSpeed=None,
                 RateCorr=None, ShortInitialDecay=None):
        # tentatively set some default values
        if InitialVol is None:
            self.InitialVol = 5
        else:
            self.InitialVol = InitialVol
        if LongTermVol is None:
            self.LongTermVol = 10
        else:
            self.LongTermVol = LongTermVol
        if RateVol is None:
            self.RateVol = 0
        else:
            self.RateVol = RateVol
        if ShortInitialAddon is None:
            self.ShortInitialAddon = 0
        else:
            self.ShortInitialAddon = ShortInitialAddon
        if MeanReversionSpeed is None:
            self.MeanReversionSpeed = 5
        else:
            self.MeanReversionSpeed = MeanReversionSpeed
        if RateCorr is None:
            self.RateCorr = 10
        else:
            self.RateCorr = RateCorr
        if ShortInitialDecay is None:
            self.ShortInitialDecay = 75
        else:
            self.ShortInitialDecay = ShortInitialDecay

        self.events = {}  # to be loaded as t/vol pairs from file
        self.events_2 = {}  # same format as events, to hold christmas discounts etc. number of extended days is processed in the loading function
        self.event_heights = {}  # to be filled by calc_event_heights function as t/height pairs
        self.tenor_overwrite = {}
        self.events_original = {} # to hold event pairs datetime/vol
        self.event_heights_original = {} # to hold event pairs datetime/event height
        self.daily_cumulative_vols = {} # to hold daily cumulative vols to pass to higher levelsa
        self.daily_vols = {}

    # set and get methods here

    # other functions
    def getBackboneVol(self, t, return_var=False, use_hol_weights=False, event_f=None):
        sig_t = self.LongTermVol - (self.LongTermVol - self.InitialVol) * math.exp(-1 * self.MeanReversionSpeed * t)
        sig_b = math.sqrt(
            sig_t * sig_t + 2 * self.RateCorr * sig_t * self.RateVol + self.RateVol * self.RateVol * t * t) + self.ShortInitialAddon * math.exp(
            -1 * self.ShortInitialDecay * t)
        if use_hol_weights:
            sig_b = sig_b * self.getTimeWeight(t)
        if event_f is not None:
            sig_b = max(sig_b + event_f(t), 0)
        sig_v = sig_b * sig_b
        if return_var:
            res = sig_v
        else:
            res = sig_b
        return res

    def getIntVol(self, t, return_var=False, t0=0, use_hol_weights=False, event_f=None):
        #int_var = quad(self.getBackboneVol, t0, t, args=(True, use_hol_weights, event_f), limit=self.subdiv_limit)[0]
        int_var=0
        tn=t0
        ##ts=self.get_sub_intervals_2(t0,t)
        ts=self.get_sub_intervals(t)
        for i in ts:
            if i>tn:
                #int_var = int_var + quad(self.getBackboneVol, tn, i, args=(True, use_hol_weights, event_f),limit=self.subdiv_limit)[0]
                incre = quad(self.getBackboneVol, tn, i, args=(True, use_hol_weights, event_f),limit=self.subdiv_limit)[0]
                int_var += incre
                tn=i
        if return_var:
            res = int_var / (t - t0)
        else:
            res = math.sqrt(int_var / (t - t0))
        return res

    def getDailyTotalVol(self,t):
        ## assume its all event add on (no christmas trick yet)
        target_time = get_datetime(t)
        cvol_t = get_t_from_datetime(target_time)
        start_t = self.convert_to_vol_day_start(target_time)
        ## to match event day convention, can only be NY cut start with one day delay
        ## add one minute to make sure it captures the event height and not missing due to miss of far digits
        ##start_t=start_t+timedelta(minutes=1)
        start_t=start_t+timedelta(days=1)
        t2=get_t_from_datetime(start_t)
        ##print (self.event_heights)
        ##print (t2)
        ##print (self.getEventAddOn(t2))
        #getDayEventHeight
        #res = self.getDailyCurveVol(t,self.getEventAddOn(t2))
        ##OLD ##res = self.getDailyCurveVol(t,self.getDayEventHeight(t2))
        res = self.getDailyCurveVol(t,self.getDayEventHeight(start_t))
        return res

    def refreshDailyCumulativeVols(self,t,cut="NY"):
        # refresh daily cumulative vols up to numeric time t (in years) from now and store in your class property
        self.daily_cumulative_vols={}
        self.daily_vols = {}
        dt_0=datetime.utcnow()
        dt_n=dt_0+timedelta(days=t*365)
        #dt_i=get_cut_dt_next(dt_0,cut)
        dt_preclose=self.get_cut_dt_next(dt_0-timedelta(days=1),cut)
        dt_i=dt_0
        cum_variance=0
        while dt_i<=dt_n:
            #first one will include events which was in the past today but fix this later
            #get the date offset right
            dt_start=dt_i
            dt_end=self.get_cut_dt_next(dt_start,cut)
            #dt_label=dt_start.strftime("%Y/%m/%d")
            dt_label=dt_end.strftime("%Y/%m/%d")
            ##event_total=self.getReleventEventOriginalHeights(dt_end)
            t_start=get_t_from_datetime(dt_start)
            t_end=get_t_from_datetime(dt_end)
            ## can't really add separately
            #current_daily_variance=self.getIntVol(t_end,True,t_start,True)*(t_end-t_start)
            ## can allow negative add-on
            #current_daily_variance+=np.sign(event_total)*event_total**2*(1/self.DAYSINYEAR)
            current_daily_variance=self.getIntVol(t_end,True,t_start,True,self.getEventAddOn)*(t_end-t_start)
            self.daily_vols[dt_label]=math.sqrt(current_daily_variance / (t_end-t_start))
            cum_variance+=current_daily_variance
            # by definition one day vol is 24 hours from close to close. So your starting point is the previous close
            # use same normalization as get_cut_vol
            dt_end_close=dt_end.replace(hour=self.TKOpen,minute=0,second=0)
            tte=get_t_from_datetime_start(dt_end_close)
            #self.daily_cumulative_vols[dt_label]=math.sqrt(cum_variance / ((dt_end-dt_preclose).total_seconds()/(self.DAYSINYEAR*24*60*60)))
            if tte == 0:
                v = 0
            else:
                v = math.sqrt(cum_variance / tte)
            self.daily_cumulative_vols[dt_label]=v
            #print (dt_label)
            #print (cum_variance)
            dt_i=dt_end



    def getDailyCurveVol(self, t, e_h=0, event_type=1, solving_mode=True):
        # NYC to NYC. t in number of years of GTM time from now, wihchever cvol day that falls into
        # Or t is a specified date, on whose 10 am nyc is used as start of the vol day and count 24 hours
        # tokyo cut won't be meaningful in this
        # if e_h ( event_height ) is passed, it treats it as an event at time t and returns correspondent daily vol
        target_time = get_datetime(t)
        cvol_t = get_t_from_datetime(target_time)
        # gmt_h=target_time.hour
        # t0=target_time.replace(microsecond=0,second=0,minute=0,hour=self.NYCut)
        # if gmt_h>=self.NYCut:
        #    start_t=t0
        #    end_t=t0+timedelta(days=1)
        # else:
        #    end_t=t0
        #    start_t=t0-timedelta(days=1)
        start_t = self.convert_to_vol_day_start(target_time)
        ### UTC day instead
        ## this will destroy event weight calc. investigate why...
        ###start_t = start_t.replace(microsecond=0, second=0, minute=0, hour=self.TKOpen)
        #### UTC day instead
        end_t = start_t + timedelta(days=1)
        # cur_t=datetime.utcnow()
        # t1=(start_t-cur_t).total_seconds()/31536000
        # t2=(end_t-cur_t).total_seconds()/31536000
        t1 = get_t_from_datetime(start_t)
        t2 = get_t_from_datetime(end_t)

        if not solving_mode:
            func2pass = self.getEventAddOn
        else:
            if e_h == 0:
                func2pass = None
            else:
                if event_type == 1:
                    func2pass = lambda x: (e_h * math.exp(-1 * self.VolNewsEventDecay * (x - cvol_t))) * (x >= cvol_t)
                elif event_type == 2:
                    func2pass = lambda x: e_h
                else:
                    func2pass = None
        res = self.getIntVol(t2, False, t1, True, func2pass)
        ## due to integration inconsistency disable holiday weighting function
        #res = self.getIntVol(t2, False, t1, False, func2pass)
        return res

    def getTimeWeight(self, t):
        # input a time t in years from current time
        # return a weighting for that specific t depending on date ( weekend or holiday ), hour as time adjustment
        # need a separate function for event weight adjustment
        # if is_offset flag is False, t is absolute date
        res = 1
        target_time = get_datetime(t)
        weekno = target_time.weekday()
        if weekno == 5 or weekno == 6:
            res = self.WeekendWeight
        else:
            h_ind = target_time.hour
            h_weight = self.HourlyWeight[h_ind]
            is_us_hol = target_time in self.us_holidays
            is_uk_hol = target_time in self.uk_holidays
            is_jp_hol = target_time in self.jp_holidays
            if is_us_hol and is_uk_hol and is_jp_hol:
                res = self.HolidayWeight
            elif not (is_us_hol or is_uk_hol or is_jp_hol):
                res = 1
            else:
                if is_uk_hol and is_jp_hol:
                    r_hour = 5
                elif is_us_hol and is_jp_hol:
                    r_hour = 4
                elif is_uk_hol and is_us_hol:
                    r_hour = 3
                elif is_jp_hol:
                    r_hour = 2
                elif is_us_hol:
                    r_hour = 1
                elif is_uk_hol:
                    r_hour = 0
                # weight2use=self.HourlyWeight[r_hour]
                weight2use = self.HourlyMatrix[r_hour]
                holiday_mask = weight2use[h_ind]
                res = h_weight * (1 - holiday_mask * (1 - self.HolidayWeight))
        return res

    def getRelaventEvents(self, t, event_type):
        # return event keys ( times ) that's preceding t within 24 hours
        if event_type == 1:
            # a=np.array(list(self.event_heights.keys()))
            a = np.array(list(self.events.keys()))
        elif event_type == 2:
            a = np.array(list(self.events_2.keys()))

        else:
            # not defined
            a = np.array([])
        
        ##print (a)
        ##print (t)
        c1 = a <= t
        c2 = a > t - 1 / 365
        return a[c1 * c2]

    def getReleventEventOriginalHeights(self,dt,return_total_heights=True):
        # if true return total of event heighs. otherwise return relevent event keys
        dt_0=dt-timedelta(days=1)
        if return_total_heights:
            rel_e_h=[v for k,v in self.events_original.items() if (k <= dt and k > dt_0)]
            res= sum(rel_e_h)
        else:
            #return events
            rel_e=[k for k in self.events_original.keys() if (k <= dt and k > dt_0)]
            res = rel_e
        return res


    def getEventAddOn_old(self, t):
        # directly use event_heights dictionary only
        # by looking at 24 hours up to and include t to identify necessary add-ons, in terms of Vol
        # key is to get the event height, based on increase of daily vol
        # t is exact GMT time in years from now
        res = 0
        rel_events = self.getRelaventEvents(t, 1)
        for e in rel_events:
            res = res + self.event_heights[e] * math.exp(-1 * self.VolNewsEventDecay * (t - e)) * (t >= e)
        rel_events_2 = self.getRelaventEvents(t, 2)
        for e in rel_events_2:
            res = res + self.event_heights[e]
        return res
    
    def getEventAddOn(self,t):
        res = 0
        dt=getTargetTime(t)
        rel_events = self.getReleventEventOriginalHeights(dt,False)
        for e in rel_events:
            te=get_t_from_datetime(e)
            res += self.event_heights_original[e] * math.exp(-1 * self.VolNewsEventDecay * (t - te)) * (t >= te)
        ## 2nd type not accounted for
        return res


    def getDayEventHeight_old(self,t):
        # get the highest height of the event that day, so add the full event to that day on daily vol calculation
        res = 0
        rel_events = self.getRelaventEvents(t, 1)
        for e in rel_events:
            res = res + self.event_heights[e] * (t >= e)
        return res

    def getDayEventHeight(self,dt):
        # get the highest height of the event that day, so add the full event to that day on daily vol calculation
        res = 0
        rel_e=self.getReleventEventOriginalHeights(dt,False)
        for e in rel_e:
            res += self.event_heights_original[e] * (dt >= e)
        return res



    def getEventHeight_old(self, t, vol_delta, event_type=1):
        # return the event height for that event
        # purly based on base vol, so multiple event during the same day will be problema
        # by definition, t of an event height for daily add on is at the beginning of that effective vol day ( NY cut )
        func = lambda h: self.getDailyCurveVol(t, h, event_type) - self.getDailyCurveVol(t) - vol_delta
        res = fsolve(func, vol_delta)[0]
        return res

    def getEventHeight(self,dt,vol_delta, event_type=1):
        t=get_t_from_datetime(dt)
        func = lambda h: self.getDailyCurveVol(t, h, event_type) - self.getDailyCurveVol(t) - vol_delta
        res = fsolve(func, vol_delta)[0]
        return res

    def getTotalVol(self, t, return_var=False):
        # include all event and time weightings, but no overwrite. so just work on the curve
        # before implementing just return curve vol without weightings
        # return self.getIntVol(t,return_var)
        res=self.getIntVol(t, return_var, 0, True, self.getEventAddOn)
        #print (res*res*t)
        #return self.getIntVol(t, return_var, 0, True, self.getEventAddOn)
        return res
        #return self.getIntVol(t, return_var, 0, False, self.getEventAddOn)


    def getVol(self, t):
        # all in integrated vol taking into account all event and time adjustment
        # currently not implemented yet, here just return int vol
        # return self.getIntVol(t)

        # t as floating
        # identify 2 neighboring tenor points. if either one is overwritten, use interpolation method
        # otherwise use curve vol
        # curve vol and overwrite are parallel
        [l_tenor, r_tenor] = get_neighbor_tenors(t)
        if not (l_tenor in self.tenor_overwrite or r_tenor in self.tenor_overwrite):
            res = self.getTotalVol(t)
        else:
            #print ("OVERWRITE WARNING")
            t1 = get_years_time(l_tenor)
            if l_tenor in self.tenor_overwrite:
                v1 = self.tenor_overwrite[l_tenor]
            else:
                v1 = self.getTotalVol(t1)
            t2 = get_years_time(r_tenor)
            if r_tenor in self.tenor_overwrite:
                v2 = self.tenor_overwrite[r_tenor]
            else:
                v2 = self.getTotalVol(t2)
            # interpolate between t1 and t2 using curve, while fixing both ends
            # calculate variance ratio based on curve
            var1 = self.getTotalVol(t1, True) * t1
            var2 = self.getTotalVol(t2, True) * t2
            vart = self.getTotalVol(t, True) * t
            var_ratio = (vart - var1) / (var2 - var1)
            # apply the same ratio to get interpolated vol
            # res=math.sqrt((var_ratio*(v2**2*t2-v1**2*t1)+v1**2*t1)/t)
            res = get_inter_point(t1, t2, v1, v2, t, var_ratio)
        return res

    def getCutVol(self,dt,c):
        # input is datetime, another layer on top of if input is an integer.
        if c == "TK":
            ##dt = dt.replace(hour=6,minute=0,second=0)
            dt = dt.replace(hour=self.TKCut,minute=0,second=0)
        elif c == "HK":
            dt = dt.replace(hour=self.HKCut,minute=0,second=0)
        else:
            ##dt = dt.replace(hour=14,minute=0,second=0)
            dt = dt.replace(hour=self.NYCut,minute=0,second=0)
        ##dt_end=dt.replace(hour=22,minute=0,second=0)
        dt_end=dt.replace(hour=self.TKOpen,minute=0,second=0)
        t = get_t_from_datetime(dt)
        t0 = get_t_from_datetime_start(dt_end)
        if t0 == 0:
            res=0
        else:
            res=self.getVol(t)*math.sqrt(t/t0)
        return res

    def overwrite_tenor(self, t, v):
        # t as a string
        self.tenor_overwrite[t] = v

    def remove_tenor_overwrite(self, t):
        self.tenor_overwrite.pop(t)

    def load_events(self,d):
        # to be loaded from files. do time translations etc.
        ##self.events[0.5] = 0.75
        ##self.events[0.75] = 1
        # extended number of days is processed and translated here and feed into events_2 dictionary
        # !! also make sure the time falls at the exact start of the NY day, by adjusting time stamp
        # self.events_2[0.6]=-2
        # self.events_2[0.5983236795148719]=-2
        # self.events_2[0.601]=-2
        # say input is "05/30/2021": -2
        # need to convert it from GMT to start of day NY cuts
        ####s = "11/03/2020"
        #####et = self.convert_to_vol_day_start(get_time_from_string(s))
        ###self.events_2[get_t_from_datetime(et)] = -2
        ####self.events[get_t_from_datetime(et)] = w
        ### now input is a dictionary...
        for k in d.keys():
            ##30Jan2024## et = self.convert_to_vol_day_start(get_time_from_string(k,1))
            et = get_time_from_string(k,1)
            et -= timedelta(hours=self.HKGMTDIFF)
            self.events[get_t_from_datetime(et)] = d[k]
            ## add this for datetime identification of events
            ## for new method treat all time stamp as HK time. So convert it to GMT here. 
            ##et_new=et-timedelta(hours=self.HKGMTDIFF)
            self.events_original[et]=d[k]

    def calc_event_heights_old(self):
        for k, v in self.events.items():
            # be sure to remove redundant / old ones?
            h = self.getEventHeight(k, v)
            self.event_heights[k] = h
        for k, v in self.events_2.items():
            h = self.getEventHeight(k, v, 2)
            self.event_heights[k] = h

    def calc_event_heights(self):
        self.event_heights_original = {}
        for k,v in self.events_original.items():
            h = self.getEventHeight(k,v)
            self.event_heights_original[k]=h


    def convert_to_vol_day_start(self, dt):
        # input any datetime dt in GMT format
        # convert to GMT format of the NY cut start
        gmt_h = dt.hour
        t0 = dt.replace(microsecond=0, second=0, minute=0, hour=self.NYCut)
        if gmt_h >= self.NYCut:
            start_t = t0
            # end_t=t0+timedelta(days=1)
        else:
            # end_t=t0
            start_t = t0 - timedelta(days=1)
        return start_t

    def get_cut_dt_next(self,dt,cut="NY"):
        # get next day's tky/nyc datetime,in gmt
        gmt_h=dt.hour
        if cut == "NY":
            h2set=self.NYCut
        elif cut == "TK":
            h2set=self.TKCut
        elif cut == "HK":
            h2set=self.HKCut
        else:
            #if no set or wrong set default to NY
            h2set=self.NYCut
        t0 = dt.replace(microsecond=0,second=0,minute=0,hour=h2set)
        if gmt_h >= h2set:
            res=t0+timedelta(days=1)
        else:
            res=t0
        return res

    def set_subdiv_limit(self, l):
        self.subdiv_limit = l

    def print_tenor_points(self):
        for tp in tenor_points:
            print (tp,self.getVol(get_years_time(tp)))

    def get_sub_intervals(self,t):
        # from t, return sub intervals so integral can be performed daily. t is numeric
        # return a list of date time in numeric
        tn=self.convert_to_vol_day_start(datetime.utcnow()+timedelta(days=1))
        ##tn=get_datetime(t0)
        tf=get_datetime(t)
        res = []
        while tn <tf:
            res.append(tn)
            tn=tn+timedelta(hours=24)
            #tn=tn+timedelta(hours=1)
        res.append(tf)
        res2 = [get_t_from_datetime(x) for x in res]
        return res2

    def get_sub_intervals_2(self,t1,t2):
        dt1=get_datetime(t1)
        dt2=get_datetime(t2)
        res = []
        dt=dt1
        while dt < dt2:
            res.append(dt)
            dt+=timedelta(hours=1)
        res.append(dt2)
        res2 = [get_t_from_datetime(x) for x in res]
        return res2



