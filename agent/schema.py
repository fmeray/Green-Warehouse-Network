"""LLM-facing schemas for extracting Green Warehouse Network intent."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.schema import AddedCustomer


class PartialScenario(BaseModel):
    """Only values explicitly requested by the user are populated."""

    model_config = ConfigDict(extra="forbid")

    objective: Literal["co2", "cost"] | None = Field(
        default=None,
        description="Optimization objective: co2 for emissions/carbon, or cost for expense/spend/price minimization.",
    )
    k_sweep: bool | None = Field(
        default=None,
        description="Whether to solve the warehouse-count curve for k=2 through 5; synonyms: k curve, warehouse sweep.",
    )
    compare_objectives: bool | None = Field(
        default=None,
        description="Whether to solve and compare both CO2 and cost objectives; synonyms: compare objectives, trade-off.",
    )
    close_sites: list[str] | None = Field(
        default=None,
        description="Candidate warehouse IDs or city names to close; synonyms: shut, remove, decommission; unit: sites.",
    )
    force_open_sites: list[str] | None = Field(
        default=None,
        description="Candidate warehouse IDs or city names that must remain open; synonyms: open, require, force open; unit: sites.",
    )
    k: int | None = Field(
        default=None,
        description="Exact number of open warehouses; synonyms: warehouse count, facilities; unit: warehouses.",
    )
    capacity_scale: float | None = Field(
        default=None,
        description="Multiplier applied to warehouse capacity; synonyms: capacity increase/decrease; unit: multiplier, default 1.0.",
    )
    demand_scale: float | None = Field(
        default=None,
        description="Multiplier applied to customer demand; synonyms: volume, throughput, demand change; unit: multiplier, 1.3 means +30%.",
    )
    remove_customers: list[str] | None = Field(
        default=None,
        description="Customer IDs to remove from the scenario; synonyms: drop, exclude customers; unit: customer IDs.",
    )
    add_customers: list[AddedCustomer] | None = Field(
        default=None,
        description="New customers with ID, name, latitude, longitude, and demand; unit: coordinates and tonnes/year.",
    )
    emission_mode: Literal["diesel", "electric"] | None = Field(
        default=None,
        description="Vehicle mode: electric for EV, e-trucks, electric trucks, battery fleet; diesel otherwise.",
    )
    emission_factor_scale: float | None = Field(
        default=None,
        description="Multiplier applied to the vehicle emission factor; synonyms: emissions factor change; unit: multiplier.",
    )
    warehouse_co2_scale: float | None = Field(
        default=None,
        description="Multiplier applied to fixed warehouse CO2; synonyms: facility emissions change; unit: multiplier.",
    )
    carbon_price_eur_per_t: float | None = Field(
        default=None,
        description="Carbon price used for reporting; synonyms: carbon tax, CO2 price; unit: EUR per tonne CO2.",
    )
    transport_cost_eur_per_tkm: float | None = Field(
        default=None,
        description="Transport cost rate; synonyms: freight rate, trucking cost; unit: EUR per tonne-km.",
    )
    max_backup_km: float | None = Field(
        default=None,
        description="Maximum distance allowed for a backup warehouse; synonyms: backup radius, backup limit; unit: km.",
    )
    failure_probability: float | None = Field(
        default=None,
        description="Annual probability weight for using the backup route; synonyms: failure risk, outage probability; unit: fraction from 0 to 0.5.",
    )


class IntentEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["scenario", "report", "reset", "question", "out_of_scope"] = Field(
        description="scenario for a requested change or hypothetical, report for a report, reset for defaults, question for information only, out_of_scope otherwise.",
    )
    scenario: PartialScenario = Field(default_factory=PartialScenario)
    question: str | None = Field(
        default=None,
        description="The information question to answer when intent is question; leave null for scenario requests.",
    )
