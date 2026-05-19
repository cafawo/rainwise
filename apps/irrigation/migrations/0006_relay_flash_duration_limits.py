from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("irrigation", "0005_curvesettings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="irrigationrun",
            name="max_duration_seconds",
            field=models.PositiveIntegerField(
                validators=[MinValueValidator(1), MaxValueValidator(3276)]
            ),
        ),
        migrations.AlterField(
            model_name="irrigationrun",
            name="optimal_duration_seconds",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[MinValueValidator(1), MaxValueValidator(3276)],
            ),
        ),
        migrations.AlterField(
            model_name="schedulerule",
            name="max_duration_seconds",
            field=models.PositiveIntegerField(
                validators=[MinValueValidator(60), MaxValueValidator(3276)]
            ),
        ),
        migrations.AlterField(
            model_name="valve",
            name="default_max_duration_seconds",
            field=models.PositiveIntegerField(
                default=1800,
                validators=[MinValueValidator(1), MaxValueValidator(3276)],
            ),
        ),
    ]
