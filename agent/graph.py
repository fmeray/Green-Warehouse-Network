"""Small deterministic scenario translator; numbers still come only from tools."""
import re
from src.schema import scenario_from_dict
from src.tools import run_scenario

def parse_scenario(text):
    t=text.lower(); p={}
    if 'electric' in t: p['emission_mode']='electric'
    m=re.search(r'(?:demand|volume).*?(\d+)\s*%',t)
    if m: p['demand_scale']=1+int(m.group(1))/100
    m=re.search(r'(?:backup|within).*?(\d+)\s*km',t)
    if m: p['max_backup_km']=float(m.group(1))
    m=re.search(r'close(?:s|d)?\s+(w\d+)',t)
    if m: p['close_sites']=[m.group(1).upper()]
    m=re.search(r'warehouse\s+(w\d+)\s+(?:goes )?down',t)
    if m: p['close_sites']=[m.group(1).upper()]
    m=re.search(r'\b([2345])\s+warehouses?',t)
    if m: p['k']=int(m.group(1))
    return scenario_from_dict(p)

def ask(text):
    return run_scenario(parse_scenario(text))

