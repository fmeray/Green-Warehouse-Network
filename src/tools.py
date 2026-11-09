"""The six structured tool functions described by the project specification."""
from copy import deepcopy
from hashlib import sha256
import json
from .data import customers as base_customers, sites, emission_factors, validate_data
from .geo import distance_matrix, haversine
from .model import solve
from .schema import Scenario, scenario_from_dict

SAVED_SCENARIOS={}

def _dump(s): return s.model_dump() if hasattr(s,'model_dump') else s.__dict__.copy()

def _materialize(s):
    cs=[dict(x) for x in base_customers() if x['id'] not in s.remove_customers]
    cs.extend(_dump(x) for x in s.add_customers)
    for c in cs: c['demand']*=s.demand_scale
    return cs, sites()

def validate_scenario(params=None):
    errors=[]; warnings=[]
    try: s=scenario_from_dict(params) if isinstance(params,dict) else (params or Scenario())
    except Exception as e: return {"valid":False,"errors":[str(e)],"warnings":[]}
    ss=sites(); ids={x['id'] for x in ss}; cs,_=_materialize(s)
    ranges=[('capacity_scale',s.capacity_scale,.5,2),('demand_scale',s.demand_scale,.5,3),('emission_factor_scale',s.emission_factor_scale,.1,2),('warehouse_co2_scale',s.warehouse_co2_scale,.5,2),('carbon_price_eur_per_t',s.carbon_price_eur_per_t,0,500),('max_backup_km',s.max_backup_km,50,2000),('failure_probability',s.failure_probability,0,.5)]
    for name,value,low,high in ranges:
        if not low <= value <= high: errors.append(f"{name} must be between {low} and {high}")
    if s.emission_mode not in emission_factors(): errors.append("emission_mode must be diesel or electric")
    for x in s.close_sites+s.force_open_sites:
        if x not in ids: errors.append(f"unknown site: {x}")
    if set(s.close_sites)&set(s.force_open_sites): errors.append("close_sites and force_open_sites overlap")
    if s.k is not None and (s.k<2 or s.k>len(ids-set(s.close_sites))): errors.append("k must be between 2 and available site count")
    for cid in s.remove_customers:
        if cid not in {x['id'] for x in base_customers()}: errors.append(f"unknown customer: {cid}")
    if len({x['id'] for x in cs}) != len(cs): errors.append("duplicate customer ID in additions")
    totalcap=sum(x['capacity']*s.capacity_scale for x in ss if x['id'] not in s.close_sites)
    totaldem=sum(x['demand'] for x in cs)
    if totalcap < 2*totaldem: warnings.append("available capacity is below 2x demand; failure cover may be infeasible")
    dist=distance_matrix(cs,ss)
    for c in cs:
        reachable=[x for x in ss if x['id'] not in s.close_sites and dist[c['id']][x['id']]<=s.max_backup_km]
        if len(reachable)<2: warnings.append(f"{c['id']} has fewer than two reachable sites")
    return {"valid":not errors,"errors":errors,"warnings":warnings}

def _baseline():
    cs=base_customers(); ss=sites(); total=sum(x['demand'] for x in cs)
    lat=sum(x['lat']*x['demand'] for x in cs)/total; lon=sum(x['lon']*x['demand'] for x in cs)/total
    dist=sum(haversine(c['lat'],c['lon'],lat,lon)*c['demand'] for c in cs)/total
    factor=emission_factors()['diesel']; co2=sum(haversine(c['lat'],c['lon'],lat,lon)*c['demand']*factor/1_000_000 for c in cs)
    return {"resilient":False,"site":{"name":"Center of gravity","lat":round(lat,5),"lon":round(lon,5)},"total_cost_eur":round(dist*total*0.0018+80*co2,2),"total_co2_t":round(co2,3),"average_distance_km":round(dist,2),"note":"Comparison baseline; it cannot provide a backup."}

def get_baseline():
    default=Scenario(); result,_=solve(*_materialize(default),default)
    if result is None: return {"status":"infeasible","single_warehouse":_baseline()}
    return {"status":"optimal","network":result,"single_warehouse":_baseline()}

def list_sites():
    return [{**x,"status":"candidate"} for x in sites()]

def run_scenario(params=None):
    try: s=scenario_from_dict(params) if isinstance(params,dict) else (params or Scenario())
    except Exception as e: return {"status":"invalid","errors":[str(e)]}
    check=validate_scenario(s)
    if not check['valid']: return {"status":"invalid","validation":check}
    cs,ss=_materialize(s); result,reason=solve(cs,ss,s)
    if result is None: return {"status":"infeasible","reason_code":reason['reason_code'],"reason":reason['details'],"validation":check}
    sweep=[]
    for k in range(2,min(5,len(ss))+1):
        r,_=solve(cs,ss,s,forced_k=k)
        if r: sweep.append({"k":k,"total_cost_eur":r['total_cost_eur'],"total_co2_t":r['total_co2_t']})
    baseline=_baseline()
    result['k_sweep']=sweep; result['resilience_price_eur']=round(result['total_cost_eur']-baseline['total_cost_eur'],2); result['resilience_price_co2_t']=round(result['total_co2_t']-baseline['total_co2_t'],3); result['scenario']=_dump(s); result['validation']=check
    sid=sha256(json.dumps(_dump(s),sort_keys=True,default=str).encode()).hexdigest()[:12]
    result['scenario_id']=sid; SAVED_SCENARIOS[sid]=deepcopy(result)
    return result

def simulate_failure(scenario_id, site_id):
    result=SAVED_SCENARIOS.get(scenario_id)
    if not result: return {"status":"not_found","scenario_id":scenario_id}
    if site_id not in result['open_sites']: return {"status":"invalid","reason":"site is not open in this scenario"}
    cs,_=_materialize(scenario_from_dict(result['scenario'])); dist=distance_matrix(cs,sites()); moved=[]; overloads=[]
    for c in cs:
        a=result['assignments'][c['id']]
        if a['primary']==site_id: moved.append({"customer_id":c['id'],"to_site":a['backup'],"demand":c['demand']})
    loads={x:0 for x in result['open_sites'] if x!=site_id}
    for c in cs:
        a=result['assignments'][c['id']]; target=a['backup'] if a['primary']==site_id else a['primary']; loads[target]+=c['demand']
    byid={x['id']:x for x in sites()}; capscale=result['scenario']['capacity_scale']
    for x,l in loads.items():
        if l>byid[x]['capacity']*capscale: overloads.append({"site":x,"load":round(l,2),"capacity":byid[x]['capacity']*capscale})
    extra=sum((dist[c['id']][result['assignments'][c['id']]['backup']]-dist[c['id']][result['assignments'][c['id']]['primary']])*c['demand'] for c in cs if result['assignments'][c['id']]['primary']==site_id)
    factor=emission_factors()[result['scenario']['emission_mode']]*result['scenario']['emission_factor_scale']
    return {"status":"ok","scenario_id":scenario_id,"failed_site":site_id,"extra_km":round(extra,2),"extra_co2_t":round(extra*factor/1_000_000,3),"customers_moved":moved,"capacity_overloads":overloads,"remaining_loads":loads}

def compare_scenarios(ids):
    rows=[]
    for sid in ids:
        if sid not in SAVED_SCENARIOS: return {"status":"not_found","scenario_id":sid}
        r=SAVED_SCENARIOS[sid]; rows.append({"scenario_id":sid,"total_cost_eur":r['total_cost_eur'],"total_co2_t":r['total_co2_t'],"k":r['k'],"open_sites":r['open_sites']})
    if not rows:return {"status":"ok","scenarios":[]}
    first=rows[0]
    for row in rows: row['delta_cost_eur']=round(row['total_cost_eur']-first['total_cost_eur'],2); row['delta_co2_t']=round(row['total_co2_t']-first['total_co2_t'],3)
    return {"status":"ok","scenarios":rows,"reference":first['scenario_id']}

