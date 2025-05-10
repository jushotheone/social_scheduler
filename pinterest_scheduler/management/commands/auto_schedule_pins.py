from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from pinterest_scheduler.models import PinTemplateVariation, ScheduledPin, Board
from django.db import transaction

class Command(BaseCommand):
    help = "Auto-schedule 120 variations into 600 pins over 30 days starting from next Monday"

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='❗ Deletes unposted ScheduledPins before creating fresh ones'
        )

    def handle(self, *args, **options):
        boards = list(Board.objects.all())[:5]
        pins = list(PinTemplateVariation.objects.all().order_by('id'))

        if len(boards) < 5:
            self.stderr.write("❌ You need at least 5 boards.")
            return

        if len(pins) != 120:
            self.stdout.write(f"⚠️ You currently have {len(pins)} variations. Expected 120.")

        if options['reset']:
            deleted = ScheduledPin.objects.filter(posted=False).delete()
            self.stdout.write(f"♻️ Reset: {deleted[0]} unposted ScheduledPins deleted.")

        # 🔢 Get next Monday
        today = timezone.now().date()
        days_until_monday = (7 - today.weekday()) % 7 or 7
        next_monday = today + timedelta(days=days_until_monday)

        scheduled_count = 0

        with transaction.atomic():
            for i, pin in enumerate(pins):
                campaign_day = (i // 4) + 1  # 4 new pins per day
                publish_date = next_monday + timedelta(days=campaign_day - 1)

                for slot, board in enumerate(boards, start=1):
                    exists = ScheduledPin.objects.filter(pin=pin, board=board).exists()
                    if not exists:
                        ScheduledPin.objects.create(
                            pin=pin,
                            board=board,
                            campaign_day=campaign_day,
                            publish_date=publish_date,
                            slot_number=slot,
                            posted=False
                        )
                        scheduled_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"✅ {scheduled_count} ScheduledPins created across 30 days starting {next_monday}."
        ))