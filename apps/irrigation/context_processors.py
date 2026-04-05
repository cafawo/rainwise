from __future__ import annotations

from django.db.utils import OperationalError, ProgrammingError

from apps.irrigation.site_context import available_sites


def site_switcher(request):
    try:
        sites = available_sites()
    except (OperationalError, ProgrammingError):
        sites = []
    return {
        "active_site": getattr(request, "active_site", None),
        "available_sites": sites,
    }
