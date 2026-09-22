from django.db import migrations, models
from django.db.models import Q


def preserve_attempted_watering(apps, schema_editor):
    runs = apps.get_model("irrigation", "IrrigationRun").objects.using(
        schema_editor.connection.alias
    )
    grouped = runs.filter(occurrence_id__isnull=False)
    attempted = Q(attempt_started_at__isnull=False) | Q(actual_start_at__isnull=False)
    grouped.filter(attempted).update(trigger="GROUP")
    grouped.exclude(attempted).delete()


class Migration(migrations.Migration):
    dependencies = [("irrigation", "0012_remove_command_queue")]

    operations = [
        migrations.RunPython(preserve_attempted_watering, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="irrigationrun", name="unique_occurrence_valve_pass",
        ),
        migrations.RemoveField(model_name="irrigationrun", name="occurrence"),
        migrations.RemoveField(model_name="irrigationrun", name="pass_number"),
        migrations.RemoveField(model_name="irrigationrun", name="member_order"),
        migrations.RemoveField(model_name="irrigationrun", name="cancellation_requested"),
        migrations.RemoveField(model_name="site", name="admission_version"),
        migrations.DeleteModel(name="RuleOccurrence"),
        migrations.AlterField(
            model_name="irrigationrun", name="trigger",
            field=models.CharField(max_length=10, choices=[
                ("SCHEDULED", "Scheduled"), ("GROUP", "Scheduled group"),
                ("MANUAL", "Manual"), ("FAILSAFE", "Failsafe"),
                ("RECOVERY", "Recovery"),
            ]),
        ),
    ]
