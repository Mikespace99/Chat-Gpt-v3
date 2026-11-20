"""Unique, versioned configuration file for agent *operational* limits.

Do not put credentials or security permissions here. Tenant overrides, if later
implemented, must be validated using the explicit allowlist and hard caps.
"""
from __future__ import annotations

import json
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_PATH = Path(__file__).with_name("settings.json")

class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    enabled: bool = True
    model: str | None = None
    max_tool_calls_per_turn: int = Field(4, ge=1, le=6)
    max_tool_calls_hard_cap: int = Field(6, ge=1, le=6)
    max_search_days: int = Field(30, ge=1, le=90)
    max_search_days_hard_cap: int = Field(90, ge=1, le=90)
    max_slots_per_page: int = Field(3, ge=1, le=3)  # WhatsApp button limit
    max_remembered_slots: int = Field(120, ge=3, le=300)
    max_recent_user_messages: int = Field(6, ge=0, le=12)
    request_timeout_seconds: int = Field(18, ge=3, le=45)
    max_output_tokens: int = Field(450, ge=100, le=900)
    allow_tenant_overrides: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_caps(self):
        if self.max_tool_calls_per_turn > self.max_tool_calls_hard_cap:
            raise ValueError("Limite richieste maggiore del limite assoluto")
        if self.max_search_days > self.max_search_days_hard_cap:
            raise ValueError("Giorni ricerca maggiori del limite assoluto")
        allowed = {"max_tool_calls_per_turn", "max_search_days", "max_recent_user_messages"}
        if not set(self.allow_tenant_overrides) <= allowed:
            raise ValueError("Override tenant non autorizzato")
        return self


def load_settings(path: Path | None = None, tenant_overrides: dict | None = None) -> AgentSettings:
    config = json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
    settings = AgentSettings.model_validate(config)
    if not tenant_overrides:
        return settings
    overrides = {k: v for k, v in tenant_overrides.items() if k in settings.allow_tenant_overrides}
    # In particular a tenant cannot enable database writes, alter the hard caps,
    # or bypass confirmation/security by putting unknown keys in settings.
    return AgentSettings.model_validate({**settings.model_dump(), **overrides})
