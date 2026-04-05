from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings

from apps.irrigation.models import Site


ACTIVE_SITE_SESSION_KEY = "active_site_id"


def available_sites():
    return Site.objects.order_by("name", "id")


def resolve_active_site(request) -> Site | None:
    selected_site_id = request.session.get(ACTIVE_SITE_SESSION_KEY)
    if selected_site_id is not None:
        site = Site.objects.filter(id=selected_site_id).first()
        if site is not None:
            return site

    return Site.objects.order_by("id").first()


def store_active_site(request, site: Site) -> None:
    request.session[ACTIVE_SITE_SESSION_KEY] = site.id


def active_site_timezone(site: Site | None) -> ZoneInfo:
    tz_name = site.timezone if site and site.timezone else settings.TIME_ZONE
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.TIME_ZONE)
