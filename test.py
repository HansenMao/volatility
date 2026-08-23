from vol import Vol

## CVOL TEST ###
#cnh_vol=CVol(4.8,5.8,0,-0.5,2.9,0,0)
#cnh_vol.load_events()
#cnh_vol.calc_event_heights()
#print (cnh_vol.getBackboneVol(1/12))
#print (cnh_vol.getIntVol(1/12))

## SABR TEST ####
#s = SSabr()
#res = s.solveSabrFromMarket(0.025,0.01,0.07,0.10,1)
#print (res)
#[-0.38316299166191947, 0.6989834015681667]
#print (s.getStrikeVol(1.1))

#s2 = SSabr(t=1,atmv=0.07,rho=-0.38316299, volvol=0.69898340)
#print (s2.getStrikeVol(1.1))

## VOL TEST ####
jpy_vol=Vol(1)
jpy_vol.set_cvol(4.4,6.9,1.65,0,2.5,0.1,50)
jpy_vol.get_surf_matrix("vol_marks.xlsx","USDJPY")
jpy_vol.fit_sabrs()
jpy_vol.interpolate_sabr()

# cnh_vol.set_cvol(5.3,6.3,1.6,0,2.5,0.1,50)
# cnh_vol.set_cvol(5.3,6.3,1.5,0,2.5,0.1,50)

##RV
 from rv import RV
 cnhrv=RV()
cnhrv.set_vol(cnh)
cnhrv.get_fwd("vol_marks.xlsx","CNH_FWD")
cnhrv.calc()

