"""Pydantic schemas for Bambuddy Print Advisor ("Assess & Enhance")."""

from typing import Literal

from pydantic import BaseModel, Field

AdvisorPriority = Literal["balanced", "fast", "strong", "gloss"]
AdvisorSeverity = Literal["info", "warning", "critical"]


class PrintAdvisorBadgeSummary(BaseModel):
    """Compact summary stored in ``LibraryFile.file_metadata['print_advisor']``."""

    health_score: int = Field(ge=0, le=100)
    tqi_mean: float
    estimated_fast_savings_s: int = Field(ge=0)
    top_risk_code: str | None = None
    top_risk_severity: AdvisorSeverity | None = None
    assessed_plate: int = 1


class LayerThermalPoint(BaseModel):
    """Per-layer thermal, flow, and speed telemetry."""

    layer: int
    z: float
    duration_s: float
    tqi: float = Field(ge=-100.0, le=100.0)
    max_flow_mm3s: float
    outer_wall_speed_mms: float | None = None
    fan_pct: int = Field(ge=0, le=100)
    risk_flags: list[str] = Field(default_factory=list)


class AdvisorFinding(BaseModel):
    """Actionable diagnostic risk or optimization opportunity."""

    code: str
    severity: AdvisorSeverity
    title: str
    detail: str
    layer_start: int | None = None
    layer_end: int | None = None
    slicer_fix: str | None = None


class AssessResponse(BaseModel):
    """Full physics & G-code diagnostic output for one plate."""

    plate_id: int
    material: str
    nozzle_diameter_mm: float = 0.4
    health_score: int = Field(ge=0, le=100)
    tqi_mean: float
    tqi_hot_layers: int
    tqi_cold_layers: int
    estimated_time_s: int
    estimated_fast_savings_s: int
    max_volumetric_flow_mm3s: float
    flow_limit_mm3s: float
    findings: list[AdvisorFinding]
    layers: list[LayerThermalPoint]
    slicer_recommendations: dict[str, str | float | int]


class EnhanceRequest(BaseModel):
    """Parameters for generating an optimized .gcode.3mf copy."""

    priority: AdvisorPriority = "balanced"
    plate: int | None = Field(default=None, ge=1)
    folder_id: int | None = None


class EnhanceResponse(BaseModel):
    """Result of rewriting G-code speeds/cooling into a new LibraryFile."""

    new_library_file_id: int
    filename: str
    priority: AdvisorPriority
    plate_id: int
    before_time_s: int
    after_time_s: int
    time_saved_s: int
    before_health_score: int
    after_health_score: int
    changes_summary: list[str]
    assess: AssessResponse
