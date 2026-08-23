#!/usr/bin/python3

from cvol_cor import CVol_Cor
from vol import Vol


class Vol_Cor(Vol):

    def set_cvol(self, short_addon, short_decay, corr_initial, corr_final, corr_decay):
        self.atm_vols = CVol_Cor(short_addon, short_decay)
        self.atm_vols.load_cparam(corr_initial, corr_final, corr_decay)

    def load_pair(self, initial_vol, long_term_vol, rate_vol, mean_reversion, rate_corr, n):
        self.atm_vols.set_vparam(initial_vol, long_term_vol, rate_vol, mean_reversion, rate_corr, n)


