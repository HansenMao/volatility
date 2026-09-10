# The desk's own published files, as they were on 2026-09-10

Fixtures for `tests/test_publish.py`, and the source of `volkit/exportseed.py`
(each channel's pair list in the file's order, the Bloomberg ATM and wing
widths).  They are what the bulk export reproduces the *shape* of: the Murex
header rows, sheet name, pair order and tenor spellings; the COS header and
labels; the Bloomberg block layout, tickers and `PLContribFull` formula
strings.  The numbers in them are one morning's marks and are not compared.

| file | channel |
|---|---|
| `BCFO_Vols_bbg_output.xlsx` | Bloomberg DCAP contribution workbook |
| `DRV_MktData_FX_Vol_20260910.xls` | Murex ATM upload |
| `DRV_MktData_FX_Broker_20260910.xls` | Murex wings upload |
| `COS_86830_Bid.csv` | COS grid |
