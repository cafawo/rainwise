from django.db import migrations, models
import django.db.models.deletion


def create_default_schedules(apps, schema_editor):
    Site = apps.get_model("irrigation", "Site")
    Schedule = apps.get_model("irrigation", "Schedule")
    ScheduleRule = apps.get_model("irrigation", "ScheduleRule")

    for site in Site.objects.all():
        schedule = Schedule.objects.filter(site=site).order_by("id").first()
        if schedule is None:
            schedule = Schedule.objects.create(site=site, name="Default")
        if getattr(site, "active_schedule_id", None) is None:
            site.active_schedule = schedule
            site.save(update_fields=["active_schedule"])

        ScheduleRule.objects.filter(
            schedule__isnull=True,
            valve__relay_device__site=site,
        ).update(schedule=schedule)


class Migration(migrations.Migration):
    dependencies = [
        ("irrigation", "0002_remove_schedulerule_fixed_duration_seconds"),
    ]

    operations = [
        migrations.CreateModel(
            name="Schedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("site", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="schedules", to="irrigation.site")),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="site",
            name="active_schedule",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="active_sites", to="irrigation.schedule"),
        ),
        migrations.AddField(
            model_name="schedulerule",
            name="schedule",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="rules", to="irrigation.schedule"),
        ),
        migrations.RunPython(create_default_schedules, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="schedulerule",
            name="schedule",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rules", to="irrigation.schedule"),
        ),
        migrations.AddConstraint(
            model_name="schedule",
            constraint=models.UniqueConstraint(fields=("site", "name"), name="unique_schedule_name"),
        ),
    ]
