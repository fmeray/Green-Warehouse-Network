"""Streamlit dashboard. Run with: streamlit run app.py"""
try:
    import streamlit as st
    import plotly.express as px
except ImportError:
    st=None
from src.geo import ExternalAPIError, geocode_address
from src.tools import get_baseline, list_sites, run_scenario, simulate_failure
from agent.graph import parse_scenario

def main():
    if st is None: raise SystemExit('Install requirements.txt to run the dashboard.')
    st.set_page_config(page_title='Green Warehouse Network',layout='wide')
    st.title('Green Warehouse Network')
    st.caption('Resilient facility-location planning with primary + backup coverage')
    with st.sidebar:
        st.subheader('Scenario')
        text=st.text_area('Describe a what-if', 'Use electric trucks and demand grows 30%')
        if st.button('Solve scenario',type='primary'): st.session_state.result=run_scenario(parse_scenario(text))
        if st.button('Reset to baseline'): st.session_state.result=get_baseline()['network']
        st.divider()
        st.subheader('Add a customer by address')
        with st.form('add_customer_by_address'):
            address=st.text_input('Customer address')
            customer_id=st.text_input('Customer ID', 'NEW01')
            demand=st.number_input('Annual demand (tonnes)', min_value=1.0, value=100.0)
            submitted=st.form_submit_button('Geocode and solve')
        if submitted:
            try:
                location=geocode_address(address)
            except (ExternalAPIError, ValueError) as exc:
                st.error(str(exc))
            else:
                scenario=parse_scenario(text)
                params=scenario.model_dump() if hasattr(scenario,'model_dump') else scenario.__dict__.copy()
                params['add_customers'].append({
                    'id':customer_id.strip(),
                    'name':location['display_name'],
                    'lat':location['lat'],
                    'lon':location['lon'],
                    'demand':demand,
                })
                st.session_state.result=run_scenario(params)
    result=st.session_state.get('result') or get_baseline()['network']
    if result.get('status')!='optimal': st.error(result); return
    cols=st.columns(5)
    cols[0].metric('Annual cost',f"€{result['total_cost_eur']:,.0f}")
    cols[1].metric('CO₂',f"{result['total_co2_t']:,.1f} t")
    cols[2].metric('Open warehouses',result['k'])
    cols[3].metric('Primary distance',f"{result['average_primary_km']:.1f} km")
    cols[4].metric('Backup distance',f"{result['average_backup_km']:.1f} km")
    left,right=st.columns(2)
    with left:
        st.subheader('Cost / CO₂ trade-off')
        sweep=result.get('k_sweep',[])
        if sweep: st.plotly_chart(px.line(sweep,x='k',y=['total_cost_eur','total_co2_t'],markers=True),use_container_width=True)
    with right:
        st.subheader('Open sites')
        st.dataframe([s for s in list_sites() if s['id'] in result['open_sites']],hide_index=True)
    st.subheader('Assignments')
    st.dataframe([{'customer_id':cid,**a} for cid,a in result['assignments'].items()],hide_index=True,use_container_width=True)
    if result.get('scenario_id'):
        failed=st.selectbox('Failure simulation',result['open_sites'])
        if st.button('Simulate failure'): st.json(simulate_failure(result['scenario_id'],failed))

if __name__=='__main__': main()
