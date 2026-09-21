from django.core.management.base import BaseCommand, CommandError

from apps.irrigation import group_services
from apps.irrigation.models import IrrigationRun


class Command(BaseCommand):
    help = (
        "Reconcile unacknowledged openings after stopping all old web and "
        "controller processes. This command closes/reads valves; it never opens them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--senders-stopped", action="store_true",
            help="Confirm every old web and controller sender has been stopped.",
        )

    def handle(self, *args, **options):
        if not options["senders_stopped"]:
            raise CommandError(
                "Stop all old web and controller processes first, then pass "
                "--senders-stopped. Elapsed time does not prove a sender stopped."
            )
        group_services.reconcile_attempts(senders_stopped=True)
        unresolved = IrrigationRun.objects.filter(
            attempt_started_at__isnull=False, closure_confirmed_at=None,
        ).count()
        self.stdout.write(
            f"Opening reconciliation completed; {unresolved} run(s) still "
            "awaiting safe completion or confirmed closure."
        )
