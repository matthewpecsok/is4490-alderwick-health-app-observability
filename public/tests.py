import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from clinic.models import AppointmentRequest, PolicyDocument
from clinic.testing import make_office, make_patient, make_plan, make_provider, make_specialty, make_user


def request_data(**overrides):
    data = {
        "first_name": "Rae",
        "last_name": "Visitor",
        "date_of_birth": "1991-02-03",
        "email": "rae@example.com",
        "phone": "",
        "is_new_patient": "on",
        "insurance_plan": "",
        "insurance_other": "Out-of-state plan",
        "preferred_specialty": "",
        "preferred_provider": "",
        "preferred_office": "",
        "preferred_times": "Mornings",
        "reason": "Annual checkup",
        "acknowledge": "on",
    }
    data.update(overrides)
    return data


class DirectoryPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plan = make_plan(name="SilverPeak PPO", code="silverpeak-ppo")
        cls.peds = make_specialty(name="Pediatrics", slug="pediatrics")
        cls.derm = make_specialty(name="Dermatology", slug="dermatology")
        cls.sandy = make_office(name="Alderwick Sandy Clinic", slug="sandy")
        cls.virtual = make_office(name="Alderwick Virtual Care", slug="virtual-care", is_virtual=True)
        cls.liu = make_provider(
            first_name="Grace", last_name="Liu", slug="grace-liu", specialty=cls.peds, office=cls.sandy,
            languages="English, Mandarin", plans=[cls.plan], accepting_new_patients=True, offers_telehealth=False,
        )
        cls.brooks = make_provider(
            first_name="Natalie", last_name="Brooks", slug="natalie-brooks", specialty=cls.derm, office=cls.sandy,
            languages="English", accepting_new_patients=False, offers_telehealth=True, gender="M",
        )
        cls.hidden = make_provider(first_name="Gone", last_name="Away", slug="gone-away", is_active=False)
        PolicyDocument.objects.create(
            title="New Patient Policy", slug="new-patients", category="patient_policy", summary="s",
            body="## Bring\n- Insurance card", effective_date=datetime.date(2026, 1, 1),
        )
        PolicyDocument.objects.create(
            title="Internal memo", slug="internal", category="privacy", summary="s", body="secret",
            effective_date=datetime.date(2026, 1, 1), is_public=False,
        )

    def names(self, response):
        return [p.last_name for p, _ in response.context["results"]]

    def test_home(self):
        response = self.client.get(reverse("public:home"))
        self.assertContains(response, "Pediatrics")
        self.assertContains(response, "Alderwick Sandy Clinic")
        self.assertEqual(response.context["provider_count"], 2)

    def test_provider_list_shows_active_providers_with_next_slot(self):
        response = self.client.get(reverse("public:provider_list"))
        self.assertEqual(sorted(self.names(response)), ["Brooks", "Liu"])
        self.assertIsNotNone(response.context["results"][0][1])

    def test_provider_filters(self):
        url = reverse("public:provider_list")
        cases = {
            "specialty=pediatrics": ["Liu"],
            "office=sandy": ["Brooks", "Liu"],
            "insurance=silverpeak-ppo": ["Liu"],
            "language=Mandarin": ["Liu"],
            "accepting=1": ["Liu"],
            "telehealth=1": ["Brooks"],
            "gender=M": ["Brooks"],
            "q=grace": ["Liu"],
            "q=dermatology": ["Brooks"],
            "specialty=pediatrics&telehealth=1": [],
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(sorted(self.names(self.client.get(f"{url}?{query}"))), expected)

    def test_empty_results_message(self):
        response = self.client.get(reverse("public:provider_list") + "?q=zzz")
        self.assertContains(response, "No providers match")

    def test_provider_detail(self):
        response = self.client.get(reverse("public:provider_detail", args=["grace-liu"]))
        self.assertContains(response, "Dr. Grace Liu, MD")
        self.assertContains(response, "SilverPeak PPO")
        self.assertTrue(response.context["slots"])
        self.assertEqual(self.client.get(reverse("public:provider_detail", args=["gone-away"])).status_code, 404)

    def test_locations(self):
        self.assertContains(self.client.get(reverse("public:location_list")), "Alderwick Sandy Clinic")
        detail = self.client.get(reverse("public:location_detail", args=["sandy"]))
        self.assertContains(detail, "Grace Liu")
        self.assertEqual(self.client.get(reverse("public:location_detail", args=["nowhere"])).status_code, 404)

    def test_insurance_matrix(self):
        response = self.client.get(reverse("public:insurance"))
        plan, row = response.context["matrix"][0]
        accepted = {specialty.name: ok for specialty, ok in row}
        self.assertTrue(accepted["Pediatrics"])
        self.assertFalse(accepted["Dermatology"])

    def test_resources(self):
        listing = self.client.get(reverse("public:resource_list"))
        self.assertContains(listing, "New Patient Policy")
        self.assertNotContains(listing, "Internal memo")
        detail = self.client.get(reverse("public:resource_detail", args=["new-patients"]))
        self.assertContains(detail, "<h3>Bring</h3>", html=True)
        self.assertEqual(self.client.get(reverse("public:resource_detail", args=["internal"])).status_code, 404)

    def test_custom_404_page(self):
        response = self.client.get("/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "couldn't find that page", status_code=404)


class AppointmentRequestFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = make_provider(slug="grace-liu")

    def test_prefill_from_provider(self):
        response = self.client.get(reverse("public:request_appointment") + "?provider=grace-liu")
        form = response.context["form"]
        self.assertEqual(form.initial["preferred_provider"], self.provider)
        self.assertEqual(form.initial["preferred_specialty"], self.provider.specialty)

    def test_valid_request_is_saved_audited_and_confirmed(self):
        response = self.client.post(reverse("public:request_appointment"), request_data())
        self.assertRedirects(response, reverse("public:request_thanks"))
        request = AppointmentRequest.objects.get()
        self.assertEqual(request.source, AppointmentRequest.Source.WEBSITE)
        self.assertEqual(request.status, AppointmentRequest.Status.SUBMITTED)
        self.assertTrue(request.is_new_patient)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CREATE_APPOINTMENT_REQUEST).exists())
        thanks = self.client.get(reverse("public:request_thanks"))
        self.assertContains(thanks, request.reference)

    def test_validation_errors(self):
        tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        cases = {
            "no contact": request_data(email="", phone=""),
            "future birth date": request_data(date_of_birth=tomorrow),
            "no insurance": request_data(insurance_other=""),
            "no acknowledgement": request_data(acknowledge=""),
            "no reason": request_data(reason=""),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                response = self.client.post(reverse("public:request_appointment"), data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(AppointmentRequest.objects.exists())

    def test_thanks_page_requires_a_submission(self):
        self.assertRedirects(self.client.get(reverse("public:request_thanks")), reverse("public:request_appointment"))

    def test_signed_in_patients_use_the_portal_form(self):
        patient = make_patient()
        self.client.force_login(patient.user)
        response = self.client.get(reverse("public:request_appointment"))
        self.assertRedirects(response, reverse("portal:request_appointment"))

    def test_staff_can_use_public_form(self):
        self.client.force_login(make_user("front_office"))
        self.assertEqual(self.client.get(reverse("public:request_appointment")).status_code, 200)


class ChatAssistantTests(TestCase):
    """The website chat assistant. Gemini is mocked: tests never call the API or LangSmith."""

    @classmethod
    def setUpTestData(cls):
        cls.office = make_office(name="Alderwick Sandy Clinic", slug="sandy")
        cls.plan = make_plan(name="SilverPeak PPO", code="silverpeak-ppo")
        PolicyDocument.objects.create(
            title="New Patient Policy", slug="new-patients", category="patient_policy", summary="s",
            body="## Bring\n- Insurance card", effective_date=datetime.date(2026, 1, 1),
        )
        PolicyDocument.objects.create(
            title="Internal memo", slug="internal", category="privacy", summary="s", body="secret-internal-text",
            effective_date=datetime.date(2026, 1, 1), is_public=False,
        )

    def post(self, message):
        return self.client.post(reverse("public:chat"), {"message": message}, content_type="application/json")

    def test_widget_is_on_public_pages(self):
        response = self.client.get(reverse("public:home"))
        self.assertContains(response, 'id="chat-widget"')
        self.assertContains(response, "call 911")

    def test_reply_is_returned_and_history_kept(self):
        from unittest import mock

        with mock.patch("clinic.gemini.generate_text", return_value="We're open weekdays.") as generate:
            first = self.post("When are you open?")
            self.assertEqual(first.json(), {"reply": "We're open weekdays."})
            self.post("And Saturdays?")
        history = self.client.session["chat_history"]
        self.assertEqual([turn["role"] for turn in history], ["user", "model", "user", "model"])
        contents, kwargs = generate.call_args.args[0], generate.call_args.kwargs
        self.assertEqual(len(contents), 3)  # two earlier turns plus the new question
        self.assertEqual(contents[-1].parts[0].text, "And Saturdays?")
        system = kwargs["system_instruction"]
        self.assertIn("Alderwick Sandy Clinic", system)
        self.assertIn("SilverPeak PPO", system)
        self.assertIn("Insurance card", system)
        self.assertNotIn("secret-internal-text", system)

    def test_history_is_capped(self):
        from unittest import mock

        with self.settings(CHAT_HISTORY_LIMIT=4), mock.patch("clinic.gemini.generate_text", return_value="ok"):
            for i in range(5):
                self.post(f"question {i}")
        history = self.client.session["chat_history"]
        self.assertEqual(len(history), 4)
        self.assertEqual(history[-2]["text"], "question 4")

    def test_reset_clears_history(self):
        from unittest import mock

        with mock.patch("clinic.gemini.generate_text", return_value="ok"):
            self.post("hello")
        self.client.post(reverse("public:chat_reset"))
        self.assertNotIn("chat_history", self.client.session)

    def test_validation(self):
        self.assertEqual(self.post("   ").status_code, 400)
        self.assertEqual(self.post("x" * 1001).status_code, 400)
        self.assertEqual(self.client.get(reverse("public:chat")).status_code, 405)

    def test_gemini_failure_returns_friendly_error(self):
        from unittest import mock

        from clinic import gemini

        with mock.patch("clinic.gemini.generate_text", side_effect=gemini.GeminiError("no key")):
            response = self.post("hello")
        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.json()["error"])
        self.assertNotIn("chat_history", self.client.session)

    def test_langsmith_is_off_in_tests(self):
        from clinic import gemini

        self.assertFalse(gemini.langsmith_enabled())
