from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.irrigation.admin import SiteAdminForm
from apps.irrigation.models import Site


class SiteTimezoneTests(SimpleTestCase):
    def test_site_accepts_valid_timezone(self) -> None:
        site = Site(name="Home", timezone="Europe/Berlin")
        site.full_clean()

    def test_site_rejects_invalid_timezone(self) -> None:
        site = Site(name="Home", timezone="Mars/Phobos")
        with self.assertRaises(ValidationError) as exc_info:
            site.full_clean()

        self.assertIn("timezone", exc_info.exception.message_dict)

    def test_site_admin_form_lists_standard_timezones(self) -> None:
        form = SiteAdminForm()
        choices = dict(form.fields["timezone"].choices)

        self.assertIn("UTC", choices)
        self.assertIn("Europe/Berlin", choices)

    def test_site_admin_form_rejects_invalid_timezone(self) -> None:
        form = SiteAdminForm(
            data={
                "name": "Home",
                "timezone": "Mars/Phobos",
                "latitude": "",
                "longitude": "",
                "active_schedule": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("timezone", form.errors)
