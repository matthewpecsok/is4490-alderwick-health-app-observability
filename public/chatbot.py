"""Website chat assistant for visitors to the public site.

The assistant answers general questions about Alderwick Family Health (locations,
hours, specialties, insurance, and public policies) using Gemini through
clinic.gemini. It knows only what is on the public website: it has no access to
patient records, schedules, or the portal.

LangSmith: when LANGSMITH_API_KEY is set, each visitor message is traced as a
"website-chat" run, with the Gemini call nested inside it. Runs are tagged with an
anonymous conversation ID (a hash of the session key) so one conversation can be
followed in LangSmith without sending the visitor's session key.
"""

import hashlib
import logging

from django.conf import settings
from google.genai import types
from langsmith import traceable

from clinic import gemini
from clinic.context_processors import PRACTICE
from clinic.models import InsurancePlan, Office, PolicyDocument, Specialty

logger = logging.getLogger("alderwick.chat")

ROLE_USER = "user"
ROLE_MODEL = "model"

GUIDELINES = """You are the website assistant for {name}, a family medical group in the Salt Lake valley.

Help visitors with general questions about the practice: locations and hours, specialties,
insurance accepted, new-patient information, appointments, billing, video visits, and the
patient portal. Use only the practice information below. If the answer is not there, say
you don't know and suggest calling the main line at {main_phone}.

Rules:
- Never give medical advice, diagnoses, or treatment or medication guidance. For health
  questions, suggest requesting an appointment, or calling the after-hours nurse line at
  {nurse_line}.
- If someone describes an emergency (for example chest pain, trouble breathing, stroke
  symptoms, severe bleeding, or thoughts of harming themselves), tell them to call 911 right
  away (or 988 for a mental health crisis) before anything else.
- You cannot see patient records, appointments, or messages, and cannot book, change, or
  cancel appointments. Point visitors to the appointment request page (/request-appointment/)
  or the patient portal (/portal/).
- Ask visitors not to share personal health information, dates of birth, or insurance
  member numbers in this chat.
- Keep answers short and friendly: two to four sentences, plain text, no markdown.

PRACTICE INFORMATION
{context}
"""


def conversation_id(request):
    """An anonymous, stable ID for this visitor's conversation (never the raw session key)."""
    if not request.session.session_key:
        request.session.save()
    return hashlib.sha256(request.session.session_key.encode()).hexdigest()[:16]


def practice_context():
    """Plain-text summary of the public website, used as the assistant's knowledge."""
    lines = [
        f"Main phone: {PRACTICE['main_phone']}",
        f"Billing: {PRACTICE['billing_phone']}",
        f"After-hours nurse line: {PRACTICE['nurse_line']}",
        "",
        "LOCATIONS",
    ]
    for office in Office.objects.prefetch_related("hours"):
        lines.append(f"- {office.name}: {office.full_address}. Phone {office.phone or PRACTICE['main_phone']}.")
        hours = "; ".join(
            f"{h.get_weekday_display()} {h.opens:%I:%M %p}-{h.closes:%I:%M %p}" for h in office.hours.all()
        )
        if hours:
            lines.append(f"  Hours: {hours}")
        if office.service_list:
            lines.append(f"  Services: {', '.join(office.service_list)}")
        if office.parking_notes:
            lines.append(f"  Parking: {office.parking_notes}")
        if office.accessibility_notes:
            lines.append(f"  Accessibility: {office.accessibility_notes}")

    lines += ["", "SPECIALTIES"]
    for specialty in Specialty.objects.all():
        lines.append(f"- {specialty.name}: {specialty.description}".rstrip(": "))

    lines += ["", "INSURANCE PLANS ACCEPTED"]
    for plan in InsurancePlan.objects.filter(is_active=True):
        lines.append(f"- {plan.name} ({plan.carrier}, {plan.get_plan_type_display()})")

    lines += ["", "PATIENT POLICIES AND RESOURCES"]
    for doc in PolicyDocument.objects.filter(is_public=True):
        lines += [f"### {doc.title} (/patient-resources/{doc.slug}/)", doc.body.strip(), ""]
    return "\n".join(lines)


def system_instruction():
    return GUIDELINES.format(context=practice_context(), **PRACTICE)


def _contents(history, message):
    contents = [
        types.Content(role=turn["role"], parts=[types.Part(text=turn["text"])])
        for turn in history
        if turn.get("role") in (ROLE_USER, ROLE_MODEL) and turn.get("text")
    ]
    contents.append(types.Content(role=ROLE_USER, parts=[types.Part(text=message)]))
    return contents


@traceable(name="website-chat", run_type="chain", tags=["alderwick", "public-site"])
def answer(message, history):
    """Return the assistant's reply to `message`, given earlier turns in `history`.

    Raises gemini.GeminiError if Gemini is not configured or the call fails.
    """
    return gemini.generate_text(_contents(history, message), system_instruction=system_instruction()).strip()


def reply_to(request, message):
    """Answer a visitor's message and remember the exchange in their session."""
    history = request.session.get("chat_history", [])
    reply = answer(
        message,
        history,
        langsmith_extra={
            "metadata": {
                "conversation_id": conversation_id(request),
                "turn": len(history) // 2 + 1,
                "signed_in": request.user.is_authenticated,
                "model": gemini.model_name(),
            }
        },
    )
    history = history + [{"role": ROLE_USER, "text": message}, {"role": ROLE_MODEL, "text": reply}]
    request.session["chat_history"] = history[-settings.CHAT_HISTORY_LIMIT :]
    logger.info("chat_reply turn=%s chars=%s", len(history) // 2, len(reply))
    return reply
