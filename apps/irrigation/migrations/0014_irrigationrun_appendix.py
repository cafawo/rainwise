from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("irrigation", "0013_in_memory_group_sequences")]

    operations = [
        migrations.AddField(
            model_name="irrigationrun",
            name="appendix",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
