"""Strict scenario input schema with no dependency on Pydantic at runtime."""
from dataclasses import dataclass, field, asdict
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
        close_sites: list[str] = []
        force_open_sites: list[str] = []
        k: int | None = Field(default=None, ge=2)
        capacity_scale: float = Field(default=1.0, ge=0.5, le=2.0)
        demand_scale: float = Field(default=1.0, ge=0.5, le=3.0)
        remove_customers: list[str] = []
        add_customers: list[AddedCustomer] = []
        emission_mode: str = "diesel"
        emission_factor_scale: float = Field(default=1.0, ge=0.1, le=2.0)
        warehouse_co2_scale: float = Field(default=1.0, ge=0.5, le=2.0)
        carbon_price_eur_per_t: float = Field(default=80.0, ge=0, le=500)
        max_backup_km: float = Field(default=500.0, ge=50, le=2000)
        failure_probability: float = Field(default=0.05, ge=0, le=0.5)
        backup_must_cover_full_demand: bool = True

        @field_validator("emission_mode")
        @classmethod
        def valid_mode(cls, value):
            if value not in {"diesel", "electric"}: raise ValueError("must be diesel or electric")
            return value

except ImportError:
    @dataclass
    class AddedCustomer:
        id: str
        name: str = "Added customer"
        lat: float = 0.0
        lon: float = 0.0
        demand: float = 1.0

    @dataclass
    class Scenario:
        close_sites: list[str] = field(default_factory=list)
        force_open_sites: list[str] = field(default_factory=list)
        k: int | None = None
        capacity_scale: float = 1.0
        demand_scale: float = 1.0
        remove_customers: list[str] = field(default_factory=list)
        add_customers: list[AddedCustomer] = field(default_factory=list)
        emission_mode: str = "diesel"
        emission_factor_scale: float = 1.0
        warehouse_co2_scale: float = 1.0
        carbon_price_eur_per_t: float = 80.0
        max_backup_km: float = 500.0
        failure_probability: float = 0.05
        backup_must_cover_full_demand: bool = True

        def model_dump(self): return asdict(self)

def scenario_from_dict(data: dict[str, Any] | None = None) -> Scenario:
    data = data or {}
    if hasattr(Scenario, "model_validate"):
        return Scenario.model_validate(data)
    adds = [AddedCustomer(**x) if isinstance(x, dict) else x for x in data.get("add_customers", [])]
    return Scenario(add_customers=adds, **{k: v for k, v in data.items() if k != "add_customers"})
