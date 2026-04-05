from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone

from apps.irrigation.models import Site


def _active_site_timezone() -> ZoneInfo:
    try:
        tz_name = (
            Site.objects.order_by("id").values_list("timezone", flat=True).first()
            or settings.TIME_ZONE
        )
    except (OperationalError, ProgrammingError):
        tz_name = settings.TIME_ZONE
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.TIME_ZONE)


class ActiveSiteTimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        timezone.activate(_active_site_timezone())
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
