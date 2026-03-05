#!/usr/bin/env bash
set -euo pipefail

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [[ -n "${DJANGO_SUPERUSER_USERNAME:-}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
  python manage.py shell -c """
from django.contrib.auth import get_user_model
User = get_user_model()
username = '${DJANGO_SUPERUSER_USERNAME}'
password = '${DJANGO_SUPERUSER_PASSWORD}'
email = '${DJANGO_SUPERUSER_EMAIL:-}'
user, created = User.objects.get_or_create(username=username, defaults={'email': email})
if not created:
    if email and user.email != email:
        user.email = email
        user.save(update_fields=['email'])
user.set_password(password)
user.is_staff = True
user.is_superuser = True
user.save()
"""
fi

exec "$@"
