from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("irrigation", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="schedulerule",
            name="fixed_duration_seconds",
        ),
    ]
