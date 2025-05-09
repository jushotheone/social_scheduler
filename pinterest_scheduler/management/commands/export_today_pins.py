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

    def handle(self, *args, **options):
        today = now().date()
        output_file = export_scheduled_pins_to_csv(target_date=today, output_path=options["output"])

        self.stdout.write(self.style.SUCCESS(f"✅ Export complete! CSV saved to: {output_file}"))