# Green Warehouse Network

This project implements the specification in `Green Warehouse Network Project Spec.md`: a deterministic, resilient warehouse network planner with primary and backup assignments, capacity-aware single-site failure checks, emissions/cost accounting, scenario tools, and a Streamlit dashboard.

## Germany sample data

The built-in dataset is a synthetic Germany-wide example: 24 customer demand points and 12 candidate logistics hubs across major German cities. Customer volumes, warehouse capacities, operating costs, and facility emissions are made-up planning inputs, not company data. The default backup distance is 500 km to accommodate the nationwide sample; replace the demo values with sourced data for a real study.

## External services

The app loads `.env` at startup. Keep API keys in that ignored file (never commit it).

- `OpenRoute_API_KEY` enables driving-distance matrices from OpenRouteService. Results are cached under `data/cache/`; without this key, distances use the local haversine calculation.
- `Nominatim_API` optionally overrides the search endpoint (default: `https://nominatim.openstreetmap.org/search`). The dashboard's **Add a customer by address** form geocodes one address per submission. Set `NOMINATIM_USER_AGENT` to identify your application, and use Nominatim in accordance with its usage policy.
- `Climate_API_KEY` is not used yet. Climatiq integration is deferred until diesel/electric activity-factor mappings are specified; the planner currently uses the illustrative factors in `data/emission_factors.csv`, which are not official Germany-specific factors.

All built-in sample records are stored in CSV files under `data/`: `customers.csv`, `candidate_sites.csv`, and `emission_factors.csv`.

## Run

The core requires Python 3.10+ and `python-dotenv` to load `.env`:

```bash
python3 -c "from src.tools import get_baseline; print(get_baseline())"
python3 -c "from agent.graph import ask; print(ask('Use electric trucks and demand grows 30%'))"
```

For the dashboard, install the optional UI dependencies and run:

```bash
python3 -m pip install -r requirements.txt
streamlit run app.py
```

The six structured functions are in `src/tools.py`: `get_baseline`, `list_sites`, `validate_scenario`, `run_scenario`, `simulate_failure`, and `compare_scenarios`. Scenario parsing is intentionally narrow and validated; the solver remains the only source of numeric outputs.
