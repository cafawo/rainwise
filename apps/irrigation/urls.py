from django.urls import path

from apps.irrigation import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("valve/<int:valve_id>/open/", views.open_valve_view, name="valve_open"),
    path("valve/<int:valve_id>/close/", views.close_valve_view, name="valve_close"),
    path("schedule/", views.schedule_view, name="schedule"),
    path("schedule/new/", views.schedule_create, name="schedule_create"),
    path("schedule/<int:rule_id>/edit/", views.schedule_edit, name="schedule_edit"),
    path("schedule/<int:rule_id>/delete/", views.schedule_delete, name="schedule_delete"),
    path("schedule/<int:rule_id>/run/", views.trigger_run_now, name="schedule_run"),
    path("logs/", views.logs_view, name="logs"),
    path("api/calendar-events/", views.calendar_events, name="calendar_events"),
    path("api/chart-data/", views.chart_data, name="chart_data"),
    path("api/valve-status/", views.valve_status, name="valve_status"),
]
