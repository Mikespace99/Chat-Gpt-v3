"""Contratto esterno AI: schema severo, senza accesso a DB o strumenti."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.context.models import Intent


class Entities(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    period: Literal["today", "tomorrow", "this_week", "next_week", "any"] | None = None
    week_part: Literal["start", "mid", "weekend"] | None = None
    weekday: Literal["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"] | None = None
    month: Literal["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"] | None = None
    month_part: Literal["start", "mid", "end", "whole"] | None = None
    week_of_month: Literal["1", "2", "3", "4", "5", "last"] | None = None
    date_from: str | None = None
    date_to: str | None = None
    time_preference: Literal["morning", "afternoon", "evening", "exact"] | None = None
    exact_time: str | None = None
    slot_number: int | None = Field(default=None, ge=1, le=100)
    service: str | None = Field(default=None, max_length=160)
    full_name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=35)
    email: str | None = Field(default=None, max_length=254)

    @field_validator("date_from", "date_to")
    @classmethod
    def valid_date(cls, value):
        if value is not None:
            from datetime import date
            if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                raise ValueError("Data non ISO YYYY-MM-DD")
        return value

    @field_validator("exact_time")
    @classmethod
    def valid_time(cls, value):
        if value is not None:
            from datetime import time
            if len(value) != 5 or time.fromisoformat(value).strftime("%H:%M") != value:
                raise ValueError("Orario non HH:MM")
        return value


class InterpretedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    entities: Entities = Field(default_factory=Entities)
    topic: str | None = Field(default=None, max_length=180)


class AIEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[InterpretedRequest] = Field(min_length=1, max_length=4)
    needs_clarification: bool = False
    clarification_reason: str | None = Field(default=None, max_length=300)

    @field_validator("clarification_reason")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if value else None
