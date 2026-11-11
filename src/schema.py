"""Validated scenario inputs defined by the final project specification."""
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    from pydantic import BaseModel, ConfigDict, Field, field_validator

    class AddedCustomer(BaseModel):
        id: str
        name: str = "Added customer"
        lat: float
        lon: float
        demand: float = Field(gt=0)

    class Scenario(BaseModel):
        model_config = ConfigDict(extra="forbid")

        objective: str = Field(default="co2", description="Optimization target: CO2/emissions/carbon or cost/expense/spend minimization.")
        k_sweep: bool = Field(default=False, description="Solve k=2 through 5; synonyms: k curve, warehouse-count sweep.")
        compare_objectives: bool = Field(default=False, description="Solve both CO2 and cost objectives; synonyms: compare objectives, trade-off.")
        close_sites: list[str] = Field(default_factory=list, description="Warehouse IDs or cities to close; synonyms: shut, remove, decommission; unit: sites.")
        force_open_sites: list[str] = Field(default_factory=list, description="Warehouses required to stay open; synonyms: open, require, force open; unit: sites.")
        k: int | None = Field(default=None, ge=2, description="Exact number of open warehouses; synonyms: facilities, warehouse count; unit: warehouses.")
        capacity_scale: float = Field(default=1.0, ge=0.5, le=2.0, description="Warehouse capacity multiplier; synonyms: capacity change; unit: multiplier.")
        demand_scale: float = Field(default=1.0, ge=0.5, le=3.0, description="Customer demand multiplier; synonyms: volume, throughput; unit: multiplier, 1.3 means +30%.")
        remove_customers: list[str] = Field(default_factory=list, description="Customer IDs to exclude; synonyms: remove, drop, exclude; unit: customer IDs.")
        add_customers: list[AddedCustomer] = Field(default_factory=list, description="New customer records; unit: coordinates and tonnes/year.")
        emission_mode: str = Field(default="diesel", description="Vehicle mode: electric for EV, e-trucks, electric fleet; diesel otherwise.")
        emission_factor_scale: float = Field(default=1.0, ge=0.1, le=2.0, description="Vehicle emissions-factor multiplier; synonyms: emissions factor change; unit: multiplier.")
        warehouse_co2_scale: float = Field(default=1.0, ge=0.5, le=2.0, description="Fixed warehouse CO2 multiplier; synonyms: facility emissions change; unit: multiplier.")
        carbon_price_eur_per_t: float = Field(default=80.0, ge=0, le=500, description="Carbon price for reporting; synonyms: carbon tax, CO2 price; unit: EUR per tonne CO2.")
        transport_cost_eur_per_tkm: float = Field(default=0.10, ge=0.01, le=1.0, description="Transport rate; synonyms: freight rate, trucking cost; unit: EUR per tonne-km.")
        max_backup_km: float = Field(default=500.0, ge=50, le=2000, description="Maximum primary-to-backup distance; synonyms: backup radius, backup limit; unit: km.")
        failure_probability: float = Field(default=0.05, ge=0, le=0.5, description="Backup-use probability weight; synonyms: failure risk, outage probability; unit: fraction.")

        @field_validator("emission_mode")
        @classmethod
        def valid_mode(cls, value):
            if value not in {"diesel", "electric"}:
                raise ValueError("must be diesel or electric")
            return value

        @field_validator("objective")
        @classmethod
        def valid_objective(cls, value):
            if value not in {"co2", "cost"}:
                raise ValueError("must be co2 or cost")
            return value

except ImportError:
    @dataclass
    class AddedCustomer:
        id: str
        lat: float
        lon: float
        demand: float
        name: str = "Added customer"

        def __post_init__(self):
            if not self.id:
                raise ValueError("id must not be empty")
            if self.demand <= 0:
                raise ValueError("demand must be greater than 0")

    @dataclass
    class Scenario:
        objective: str = field(default="co2", metadata={"description": "Optimization target: CO2/emissions/carbon or cost/expense/spend minimization."})
        k_sweep: bool = field(default=False, metadata={"description": "Solve k=2 through 5; synonyms: k curve, warehouse-count sweep."})
        compare_objectives: bool = field(default=False, metadata={"description": "Solve both CO2 and cost objectives; synonyms: compare objectives, trade-off."})
        close_sites: list[str] = field(default_factory=list, metadata={"description": "Warehouse IDs or cities to close; synonyms: shut, remove, decommission; unit: sites."})
        force_open_sites: list[str] = field(default_factory=list, metadata={"description": "Warehouses required to stay open; synonyms: open, require, force open; unit: sites."})
        k: int | None = field(default=None, metadata={"description": "Exact number of open warehouses; synonyms: facilities, warehouse count; unit: warehouses."})
        capacity_scale: float = field(default=1.0, metadata={"description": "Warehouse capacity multiplier; synonyms: capacity change; unit: multiplier."})
        demand_scale: float = field(default=1.0, metadata={"description": "Customer demand multiplier; synonyms: volume, throughput; unit: multiplier, 1.3 means +30%."})
        remove_customers: list[str] = field(default_factory=list, metadata={"description": "Customer IDs to exclude; synonyms: remove, drop, exclude; unit: customer IDs."})
        add_customers: list[AddedCustomer] = field(default_factory=list, metadata={"description": "New customer records; unit: coordinates and tonnes/year."})
        emission_mode: str = field(default="diesel", metadata={"description": "Vehicle mode: electric for EV, e-trucks, electric fleet; diesel otherwise."})
        emission_factor_scale: float = field(default=1.0, metadata={"description": "Vehicle emissions-factor multiplier; synonyms: emissions factor change; unit: multiplier."})
        warehouse_co2_scale: float = field(default=1.0, metadata={"description": "Fixed warehouse CO2 multiplier; synonyms: facility emissions change; unit: multiplier."})
        carbon_price_eur_per_t: float = field(default=80.0, metadata={"description": "Carbon price for reporting; synonyms: carbon tax, CO2 price; unit: EUR per tonne CO2."})
        transport_cost_eur_per_tkm: float = field(default=0.10, metadata={"description": "Transport rate; synonyms: freight rate, trucking cost; unit: EUR per tonne-km."})
        max_backup_km: float = field(default=500.0, metadata={"description": "Maximum primary-to-backup distance; synonyms: backup radius, backup limit; unit: km."})
        failure_probability: float = field(default=0.05, metadata={"description": "Backup-use probability weight; synonyms: failure risk, outage probability; unit: fraction."})

        def __post_init__(self):
            ranges = {
                "capacity_scale": (.5, 2), "demand_scale": (.5, 3),
                "emission_factor_scale": (.1, 2), "warehouse_co2_scale": (.5, 2),
                "carbon_price_eur_per_t": (0, 500),
                "transport_cost_eur_per_tkm": (.01, 1),
                "max_backup_km": (50, 2000), "failure_probability": (0, .5),
            }
            if self.objective not in {"co2", "cost"}:
                raise ValueError("objective must be co2 or cost")
            if self.emission_mode not in {"diesel", "electric"}:
                raise ValueError("emission_mode must be diesel or electric")
            for name, (low, high) in ranges.items():
                if not low <= getattr(self, name) <= high:
                    raise ValueError(f"{name} must be between {low} and {high}")
            if self.k is not None and self.k < 2:
                raise ValueError("k must be at least 2")

        def model_dump(self):
            return asdict(self)


def scenario_from_dict(data: dict[str, Any] | None = None) -> Scenario:
    data = data or {}
    if hasattr(Scenario, "model_validate"):
        return Scenario.model_validate(data)
    additions = [
        AddedCustomer(**item) if isinstance(item, dict) else item
        for item in data.get("add_customers", [])
    ]
    return Scenario(
        add_customers=additions,
        **{key: value for key, value in data.items() if key != "add_customers"},
    )
