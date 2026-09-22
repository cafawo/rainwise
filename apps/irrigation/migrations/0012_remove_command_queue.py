from django.db import migrations
from django.utils import timezone


def retire_abandoned_requests(apps, schema_editor):
    """Upgrade after stopping old processes and letting relay timers finish."""
    runs = apps.get_model("irrigation", "IrrigationRun").objects.using(
        schema_editor.connection.alias,
    )
    now = timezone.now()
    runs.filter(
        dispatch_state__in=("QUEUED", "UNSENT"),
        actual_start_at=None, actual_stop_at=None,
    ).update(
        status="FAILED", stop_reason="MANUAL_STOP", cancellation_requested=True,
        attempt_started_at=None, attempt_finished_at=None, delivery_uncertain=False,
        actual_stop_at=now,
        error_message="Abandoned queued opening cancelled during upgrade.",
    )
    runs.filter(
        dispatch_state__in=("OPENING", "SENDING"), actual_stop_at=None,
    ).update(
        status="FAILED", stop_reason="ERROR", cancellation_requested=True,
        delivery_uncertain=True, actual_stop_at=now,
        error_message="Interrupted opening retired during upgrade; delivery is uncertain.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("irrigation", "0011_controller_commands"),
    ]

    operations = [
        migrations.RunPython(retire_abandoned_requests, migrations.RunPython.noop),
        migrations.RemoveField(model_name="irrigationrun", name="sender_interrupted"),
        migrations.RemoveField(model_name="irrigationrun", name="dispatch_state"),
        migrations.DeleteModel(name="ValveClosure"),
    ]
