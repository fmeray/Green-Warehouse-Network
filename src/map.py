"""Folium maps for customer demand and solver-generated k solutions."""
from math import sqrt


def _records(value):
    """Accept project lists of dicts and pandas DataFrames."""
    if hasattr(value, "to_dict"):
        return value.to_dict("records")
    return list(value)


def _field(row, *names):
    for name in names:
        if name in row:
            return row[name]
    raise KeyError(f"Missing one of: {', '.join(names)}")


def plot_network_solutions(customers, sites, result):
    """Return a Folium map with the current and every feasible k solution.

    Customers are rendered once as demand-sized circles. Each k solution is a
    separate layer that can be toggled in the layer control. Its primary links
    are solid and backup links are dashed.
    """
    import folium

    customers = _records(customers)
    sites = _records(sites)
    if not customers:
        raise ValueError("At least one customer is required to draw the map")

    normalized = []
    for row in customers:
        normalized.append({
            "id": _field(row, "id", "ID"),
            "name": row.get("name", row.get("Name", "")),
            "lat": float(_field(row, "lat", "Latitude")),
            "lon": float(_field(row, "lon", "Longitude")),
            "demand": float(_field(row, "demand", "Demand")),
        })
    site_by_id = {_field(row, "id", "ID"): row for row in sites}
    total_demand = sum(row["demand"] for row in normalized)
    cog = (
        sum(row["lat"] * row["demand"] for row in normalized) / total_demand,
        sum(row["lon"] * row["demand"] for row in normalized) / total_demand,
    )
    m = folium.Map(location=list(cog), zoom_start=6, tiles="OpenStreetMap")

    demand_layer = folium.FeatureGroup(name="Customers (circle size = yearly demand)", show=True)
    minimum_demand = min(row["demand"] for row in normalized)
    for row in normalized:
        radius = 4 + 8 * sqrt(row["demand"] / minimum_demand)
        folium.CircleMarker(
            [row["lat"], row["lon"]],
            radius=radius,
            color="#277DA1",
            weight=1,
            fill=True,
            fill_color="#277DA1",
            fill_opacity=0.65,
            tooltip=f"{row['id']}: {row['demand']:,.0f} t/year",
        ).add_to(demand_layer)
    demand_layer.add_to(m)

    cog_layer = folium.FeatureGroup(name="Greenfield: center of gravity", show=True)
    for row in normalized:
        folium.PolyLine(
            [[row["lat"], row["lon"]], list(cog)],
            color="#808080",
            weight=1,
            opacity=0.45,
        ).add_to(cog_layer)
    folium.Marker(
        list(cog),
        tooltip=f"Center of gravity ({cog[0]:.2f}, {cog[1]:.2f})",
        icon=folium.Icon(color="red", icon="warehouse", prefix="fa"),
    ).add_to(cog_layer)
    cog_layer.add_to(m)

    sweep = result.get("k_sweep", [])
    current = {
        "k": result.get("k"),
        "total_cost_eur": result.get("total_cost_eur"),
        "total_co2_t": result.get("total_co2_t"),
        "open_sites": result.get("open_sites", []),
        "assignments": result.get("assignments", {}),
    }
    solutions = {
        item.get("k"): item for item in sweep
        if item.get("k") is not None and item.get("status", "optimal") == "optimal"
    }
    solutions[current["k"]] = current

    for k in sorted(solutions):
        solution = solutions[k]
        title = f"{k} warehouses"
        if solution.get("total_cost_eur") is not None:
            title += f" (€{solution['total_cost_eur']:,.0f})"
        layer = folium.FeatureGroup(name=title, show=(k == current["k"]))
        assignments = solution.get("assignments", {})
        for row in normalized:
            assignment = assignments.get(row["id"], {})
            primary_id = assignment.get("primary")
            backup_id = assignment.get("backup")
            for site_id, color, dash in (
                (primary_id, "#2D6A4F", None),
                (backup_id, "#E9C46A", "6, 8"),
            ):
                site = site_by_id.get(site_id)
                if site is None:
                    continue
                endpoint = [float(_field(site, "lat", "Latitude")), float(_field(site, "lon", "Longitude"))]
                folium.PolyLine(
                    [[row["lat"], row["lon"]], endpoint],
                    color=color,
                    weight=2,
                    opacity=0.65,
                    dash_array=dash,
                ).add_to(layer)
        for site_id in solution.get("open_sites", []):
            site = site_by_id.get(site_id)
            if site is None:
                continue
            lat = float(_field(site, "lat", "Latitude"))
            lon = float(_field(site, "lon", "Longitude"))
            assigned_demand = sum(
                row["demand"] for row in normalized
                if assignments.get(row["id"], {}).get("primary") == site_id
            )
            folium.Marker(
                [lat, lon],
                tooltip=f"{site.get('name', site_id)}: {assigned_demand:,.0f} t primary demand",
                icon=folium.Icon(color="orange", icon="warehouse", prefix="fa"),
            ).add_to(layer)
        layer.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    return m


def plot_customers_only(customers):
    """Return the landing map before a scenario has been solved.

    This deliberately does not accept candidate sites or a solver result.  It
    keeps the initial dashboard state useful without implying that an
    objective has already been optimized.
    """
    import folium

    customers = _records(customers)
    if not customers:
        raise ValueError("At least one customer is required to draw the map")

    normalized = [
        {
            "id": _field(row, "id", "ID"),
            "lat": float(_field(row, "lat", "Latitude")),
            "lon": float(_field(row, "lon", "Longitude")),
            "demand": float(_field(row, "demand", "Demand")),
        }
        for row in customers
    ]
    total_demand = sum(row["demand"] for row in normalized)
    center = [
        sum(row["lat"] * row["demand"] for row in normalized) / total_demand,
        sum(row["lon"] * row["demand"] for row in normalized) / total_demand,
    ]
    m = folium.Map(location=center, zoom_start=6, tiles="OpenStreetMap")
    layer = folium.FeatureGroup(name="Customers (circle size = yearly demand)", show=True)
    minimum_demand = min(row["demand"] for row in normalized)
    for row in normalized:
        radius = 4 + 8 * sqrt(row["demand"] / minimum_demand)
        folium.CircleMarker(
            [row["lat"], row["lon"]],
            radius=radius,
            color="#277DA1",
            weight=1,
            fill=True,
            fill_color="#277DA1",
            fill_opacity=0.65,
            tooltip=f"{row['id']}: {row['demand']:,.0f} t/year",
        ).add_to(layer)
    layer.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    return m
