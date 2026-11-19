"""
Route web: registrazione, login, onboarding, API di salvataggio.
"""

from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.web.sse import agenda_event_stream, notify_agenda
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from app.web.auth import (
    register_user,
    login_user,
    get_current_user,
    require_user,
    logout,
    _set_auth_cookies,
    _clear_auth_cookies,
)
from app.repositories.onboarding import (
    get_tenant_by_owner,
    get_full_config,
    update_tenant,
    replace_locations,
    replace_working_hours,
    replace_services,
    replace_exceptions,
    set_tenant_holidays,
    get_holidays,
)
from app.repositories import appointment as appointment_repo
from app.repositories.customer import (
    search_customers,
    get_or_create_customer,
    update_customer_name,
    normalize_phone,
    format_phone_display,
    list_customers,
    get_customer,
    create_customer_manual,
    update_customer,
    count_new_customers,
)
from app.repositories.tenant import get_tenant_knowledge
from app.booking.engine import search_availability

router = APIRouter(tags=["web"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Workaround Python 3.14 / Jinja2 cache key bug
templates.env.cache = None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AuthBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class TenantData(BaseModel):
    business_name: str
    specialty: Optional[str] = None
    assistant_name: Optional[str] = "Assistente"
    timezone: Optional[str] = "Europe/Rome"
    language: Optional[str] = "it"
    min_lead_hours: Optional[int] = 2
    max_appointments_per_day: Optional[int] = 12
    slot_search_days: Optional[int] = 30
    phone: Optional[str] = None          # senza +39
    whatsapp_number: Optional[str] = None  # senza +39
    email: Optional[str] = None
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None
    google_calendar_id: Optional[str] = None
    info: Optional[dict] = None


class LocationIn(BaseModel):
    id: Optional[str] = None
    name: str
    city: Optional[str] = ""
    address: Optional[str] = ""
    active: bool = True
    sort_order: int = 0
