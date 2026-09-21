from django.core.management.base import BaseCommand, CommandError

from apps.irrigation import group_services
from apps.irrigation.models import ValveClosure


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
        unresolved = group_services._unresolved_runs().count()
        pending_closures = ValveClosure.objects.filter(confirmed_at=None).count()
        if unresolved or pending_closures:
            raise CommandError(
                f"Reconciliation incomplete: {unresolved} run(s) and "
                f"{pending_closures} close request(s) remain unresolved. "
                "Restore relay/database access and repeat before starting watering."
            )
        self.stdout.write(
            f"Opening reconciliation completed; {unresolved} run(s) still "
            "awaiting safe completion or confirmed closure."
        )
