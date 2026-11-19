"""Read-only tools the website chat assistant can call.

Gemini decides when to call these (function calling). Each tool is decorated with
@traceable(run_type="tool"), so in LangSmith every call shows up as its own step,
with its arguments and result, between the Gemini calls that requested it and that
used its answer.

Tools only read public directory information (the same data shown on the public
website). They never touch patient records and never book or change anything.
To add a tool: write the function, add a FunctionDeclaration to DECLARATIONS, and
add it to TOOLS.
"""

from django.db.models import Q
from django.utils import timezone
from google.genai import types
from langsmith import traceable

from clinic.models import InsurancePlan, Office, Provider
from clinic.services import next_available_slot

MAX_RESULTS = 8


def _provider_summary(provider):
    return {
        "name": provider.display_name,
        "title": provider.title,
        "specialty": provider.specialty.name,
        "locations": [office.name for office in provider.offices.all()],
        "languages": provider.languages,
        "accepting_new_patients": provider.accepting_new_patients,
        "offers_video_visits": provider.offers_telehealth,
        "profile_page": provider.get_absolute_url(),
    }


def _offices_matching(text):
    return Office.objects.filter(Q(name__icontains=text) | Q(city__icontains=text) | Q(slug__icontains=text))


def _plans_matching(text):
    return InsurancePlan.objects.filter(
        Q(name__icontains=text) | Q(carrier__icontains=text) | Q(code__icontains=text), is_active=True
    )


def _find_providers(
    specialty=None, location=None, insurance=None, language=None, name=None,
    accepting_new_patients=None, video_visits=None,
):
    providers = Provider.objects.filter(is_active=True).select_related("specialty").prefetch_related("offices")
    if specialty:
        providers = providers.filter(specialty__name__icontains=specialty)
    if location:
        providers = providers.filter(offices__in=_offices_matching(location))
    if insurance:
        providers = providers.filter(insurance_plans__in=_plans_matching(insurance))
    if language:
        providers = providers.filter(languages__icontains=language)
    if name:
        for word in name.replace(",", " ").split():
            if word.lower().rstrip(".") in ("dr", "md", "do", "np", "pa"):
                continue
            providers = providers.filter(Q(first_name__icontains=word) | Q(last_name__icontains=word))
    if accepting_new_patients:
        providers = providers.filter(accepting_new_patients=True)
    if video_visits:
        providers = providers.filter(offers_telehealth=True)
    return providers.distinct().order_by("last_name", "first_name")


@traceable(run_type="tool", name="search_providers")
def search_providers(
    specialty=None, location=None, insurance=None, language=None, name=None,
    accepting_new_patients=None, video_visits=None,
):
    providers = _find_providers(
        specialty=specialty, location=location, insurance=insurance, language=language, name=name,
        accepting_new_patients=accepting_new_patients, video_visits=video_visits,
    )
    total = providers.count()
    return {
        "total_matches": total,
        "providers": [_provider_summary(p) for p in providers[:MAX_RESULTS]],
        "note": f"Showing the first {MAX_RESULTS}." if total > MAX_RESULTS else "",
    }


@traceable(run_type="tool", name="next_available_appointment")
def next_available_appointment(provider_name):
    matches = list(_find_providers(name=provider_name)[:MAX_RESULTS])
    if not matches:
        return {"found": False, "message": f"No provider matches '{provider_name}'."}
    if len(matches) > 1:
        return {
            "found": False,
            "message": "More than one provider matches. Ask which one they mean.",
            "matches": [p.display_name for p in matches],
        }
    provider = matches[0]
    slot = next_available_slot(provider)
    if slot is None:
        return {"found": True, "provider": provider.display_name, "next_opening": None,
                "message": "No openings in the next 30 days. Suggest requesting an appointment or calling."}
    start = timezone.localtime(slot.start)
    return {
        "found": True,
        "provider": provider.display_name,
        "next_opening": start.strftime("%A, %B %-d at %-I:%M %p"),
        "location": slot.office.name,
        "accepting_new_patients": provider.accepting_new_patients,
        "how_to_book": "Request it at /request-appointment/ or call the main line. The assistant cannot book.",
    }


@traceable(run_type="tool", name="check_insurance")
def check_insurance(plan, specialty=None):
    plans = list(_plans_matching(plan))
    if not plans:
        return {
            "accepted": False,
            "message": f"'{plan}' is not on our list of accepted plans.",
            "accepted_plans": list(InsurancePlan.objects.filter(is_active=True).values_list("name", flat=True)),
        }
    results = []
    for match in plans:
        providers = match.providers.filter(is_active=True)
        if specialty:
            providers = providers.filter(specialty__name__icontains=specialty)
        by_specialty = {}
        for specialty_name in providers.values_list("specialty__name", flat=True):
            by_specialty[specialty_name] = by_specialty.get(specialty_name, 0) + 1
        results.append({
            "plan": match.name,
            "carrier": match.carrier,
            "type": match.get_plan_type_display(),
            "providers_in_network": providers.count(),
            "providers_by_specialty": by_specialty,
        })
    return {"accepted": True, "plans": results}


TOOLS = {
    "search_providers": search_providers,
    "next_available_appointment": next_available_appointment,
    "check_insurance": check_insurance,
}

_S = types.Schema
_STR, _BOOL, _OBJ = types.Type.STRING, types.Type.BOOLEAN, types.Type.OBJECT

DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_providers",
        description=(
            "Search Alderwick's provider directory. All filters are optional and combine with AND. "
            "Use it to find doctors by specialty, location, insurance, language, or name."
        ),
        parameters=_S(type=_OBJ, properties={
            "specialty": _S(type=_STR, description="Specialty, e.g. 'Pediatrics' or 'Dermatology'."),
            "location": _S(type=_STR, description="Clinic name or city, e.g. 'Sandy', or 'virtual' for video visits."),
            "insurance": _S(type=_STR, description="Insurance plan or carrier name."),
            "language": _S(type=_STR, description="Language the provider speaks, e.g. 'Spanish'."),
            "name": _S(type=_STR, description="Part of the provider's first or last name."),
            "accepting_new_patients": _S(type=_BOOL, description="Only providers accepting new patients."),
            "video_visits": _S(type=_BOOL, description="Only providers who offer video visits."),
        }),
    ),
    types.FunctionDeclaration(
        name="next_available_appointment",
        description="Find the next open appointment time for one provider. Does not book anything.",
        parameters=_S(type=_OBJ, required=["provider_name"], properties={
            "provider_name": _S(type=_STR, description="The provider's name, e.g. 'Grace Liu' or 'Dr. Liu'."),
        }),
    ),
    types.FunctionDeclaration(
        name="check_insurance",
        description="Check whether Alderwick accepts an insurance plan, optionally for one specialty.",
        parameters=_S(type=_OBJ, required=["plan"], properties={
            "plan": _S(type=_STR, description="Plan or carrier name the visitor mentioned."),
            "specialty": _S(type=_STR, description="Optional specialty to check, e.g. 'Cardiology'."),
        }),
    ),
]

GEMINI_TOOLS = [types.Tool(function_declarations=DECLARATIONS)]


def call_tool(name, args):
    """Run a tool requested by Gemini. Errors are returned to the model, not raised."""
    function = TOOLS.get(name)
    if function is None:
        return {"error": f"Unknown tool '{name}'."}
    try:
        return function(**(args or {}))
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}
