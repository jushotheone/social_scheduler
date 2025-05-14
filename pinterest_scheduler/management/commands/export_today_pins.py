from django.core.management.base import BaseCommand
from django.utils.timezone import now
from pinterest_scheduler.services.exporter import export_scheduled_pins_to_csv

class Command(BaseCommand):
    help = "Export all ScheduledPins for today into a Pinterest bulk upload CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            help="Optional output filename (default: scheduled_pins_export.csv)",
            default="scheduled_pins_export.csv"
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='🔍 Preview export without changing status'
        )

    def handle(self, *args, **options):
        today = now().date()
        dry_run = options.get("dry_run", False)

        output_file = export_scheduled_pins_to_csv(
            target_date=today,
            output_path=options["output"],
            dry_run=dry_run
        )

        status = "PREVIEW ONLY" if dry_run else "Export complete"
        self.stdout.write(self.style.SUCCESS(f"✅ {status}! CSV saved to: {output_file}"))