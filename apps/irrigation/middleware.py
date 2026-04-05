from __future__ import annotations

from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone

from apps.irrigation.site_context import active_site_timezone, resolve_active_site


def _resolve_request_site(request):
    try:
        return resolve_active_site(request)
    except (OperationalError, ProgrammingError):
        return None


class ActiveSiteTimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.active_site = _resolve_request_site(request)
        timezone.activate(active_site_timezone(request.active_site))
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
