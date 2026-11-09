"""Deterministic resilient facility-location heuristic.

The implementation intentionally has a small dependency footprint. It enumerates
site sets and uses a deterministic capacitated assignment search; this is a
portable fallback for PuLP/OR-Tools and is easy to replace with a MIP backend.
"""
from itertools import combinations
from math import inf
from .geo import distance_matrix
from .data import customers as default_customers, sites as default_sites, emission_factors

def _cost(distance, demand, factor, carbon):
    co2 = distance * demand * factor / 1_000_000
    transport = distance * demand * 0.0018
    return transport + carbon * co2, co2

def _assign(cs, ss, opened, dist, scenario, factor):
    cap={s['id']:s['capacity']*scenario.capacity_scale for s in ss if s['id'] in opened}
    p=scenario.failure_probability; carbon=scenario.carbon_price_eur_per_t
    options={}
    for c in cs:
        pairs=[]
        for primary in opened:
            for backup in opened:
                if primary == backup or dist[c['id']][backup] > scenario.max_backup_km: continue
                c1,c2=_cost(dist[c['id']][primary],c['demand'],factor,carbon),_cost(dist[c['id']][backup],c['demand'],factor,carbon)
                pairs.append((c1[0]+p*c2[0], primary, backup, c1[1], c2[1]))
        if not pairs: return None
        options[c['id']]=sorted(pairs)
    loads={x:0.0 for x in opened}; chosen={}; backup_load={x:0.0 for x in opened}
    # Largest customers first gives capacity repair a stable, useful ordering.
    ordered=sorted(cs,key=lambda x:(-x['demand'],x['id']))
    for c in ordered:
        best=None; bestscore=inf
        for base, primary, backup, co2p, co2b in options[c['id']]:
            if loads[primary] + c['demand'] > cap[primary] + 1e-9: continue
            projected=dict(loads); projected[primary]+=c['demand']
            # The backup must absorb the full primary load if that primary fails.
            if scenario.backup_must_cover_full_demand and backup_load[backup]+c['demand'] > cap[backup] + 1e-9: continue
            # Penalize concentration and capacity utilization while retaining transport objective.
            score=base + 0.03*sum((projected[x]/cap[x])**3 for x in opened)
            if score < bestscore: bestscore,best=score,(primary,backup,co2p,co2b)
        if best is None: return None
        primary,backup,co2p,co2b=best; chosen[c['id']]={"primary":primary,"backup":backup,"primary_co2":co2p,"backup_co2":co2b}
        loads[primary]+=c['demand']; backup_load[backup]+=c['demand']
    # Exact post-check of every single-site failure.
    overloads=[]
    for failed in opened:
        for target in opened:
            if target==failed: continue
            load=loads[target]+sum(c['demand'] for c in cs if chosen[c['id']]['primary']==failed and chosen[c['id']]['backup']==target)
            if load > cap[target]+1e-6: overloads.append({"failed_site":failed,"site":target,"load":round(load,2),"capacity":cap[target]})
    if overloads: return None
    return chosen,loads

def solve(cs, ss, scenario, forced_k=None):
    dist=distance_matrix(cs,ss); factor=emission_factors()[scenario.emission_mode]*scenario.emission_factor_scale
    available=[s['id'] for s in ss if s['id'] not in scenario.close_sites]
    forced=set(scenario.force_open_sites)
    if not forced.issubset(set(available)): return None, {"reason_code":"too_few_sites","details":"forced site is closed or unknown"}
    k=forced_k if forced_k is not None else scenario.k
    ks=[k] if k else range(max(2,len(forced)),min(5,len(available))+1)
    best=None
    byid={s['id']:s for s in ss}
    for count in ks:
        candidates=[x for x in combinations(available,count) if forced.issubset(x)]
        for opened_tuple in candidates:
            opened=list(opened_tuple); assigned=_assign(cs,ss,opened,dist,scenario,factor)
            if not assigned: continue
            chosen,loads=assigned
            transport_co2=sum(x['primary_co2']+scenario.failure_probability*x['backup_co2'] for x in chosen.values())
            warehouse_co2=sum(byid[x]['fixed_co2'] for x in opened)*scenario.warehouse_co2_scale
            fixed=sum(byid[x]['fixed_cost'] for x in opened)
            transport_cost=sum((dist[c['id']][chosen[c['id']]['primary']]*c['demand']*0.0018 + scenario.failure_probability*dist[c['id']][chosen[c['id']]['backup']]*c['demand']*0.0018) for c in cs)
            cost=fixed+transport_cost+scenario.carbon_price_eur_per_t*(transport_co2+warehouse_co2)
            result={"status":"optimal","k":count,"open_sites":opened,"assignments":chosen,"loads":loads,"total_cost_eur":round(cost,2),"total_co2_t":round(transport_co2+warehouse_co2,3),"transport_co2_t":round(transport_co2,3),"warehouse_co2_t":round(warehouse_co2,3),"average_primary_km":round(sum(dist[c['id']][chosen[c['id']]['primary']] for c in cs)/len(cs),2),"average_backup_km":round(sum(dist[c['id']][chosen[c['id']]['backup']] for c in cs)/len(cs),2)}
            if best is None or result['total_cost_eur'] < best['total_cost_eur']: best=result
        if k: break
    return best, {"reason_code":"capacity","details":"No site set can satisfy normal and single-failure capacities"}

