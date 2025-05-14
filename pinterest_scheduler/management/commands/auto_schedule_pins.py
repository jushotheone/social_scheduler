from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from pinterest_scheduler.models import PinTemplateVariation, ScheduledPin, Board
from django.db import transaction

class Command(BaseCommand):
    help = "SmartLoop schedule: evenly distribute 600 pins over 30 days (20 per day, 5 boards)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='❗ Delete scheduled/exported pins before recreating schedule'
        )

    def handle(self, *args, **options):
        boards = list(Board.objects.all())[:5]
        pins = list(PinTemplateVariation.objects.all().order_by('id'))

        if len(boards) < 5:
            self.stderr.write("❌ You need at least 5 boards.")
            return

        if len(pins) != 120:
            self.stdout.write(f"⚠️ You currently have {len(pins)} pins. Expected 120.")

        if options['reset']:
            deleted = ScheduledPin.objects.filter(status__in=['scheduled', 'exported']).delete()
            self.stdout.write(f"♻️ Reset: {deleted[0]} scheduled/exported pins deleted.")

        # 👇 Build the full set: 120 variations × 5 boards = 600 pins
        full_pinset = [(pin, board) for pin in pins for board in boards]

        if len(full_pinset) != 600:
            self.stderr.write("❌ Expected 600 scheduled pins. Check data.")
            return

        # 🔢 Get next Monday
        today = timezone.now().date()
        days_until_monday = (7 - today.weekday()) % 7 or 7
        next_monday = today + timedelta(days=days_until_monday)

        scheduled_count = 0

        with transaction.atomic():
            for day_offset in range(30):
                chunk = full_pinset[day_offset * 20 : (day_offset + 1) * 20]
                publish_date = next_monday + timedelta(days=day_offset)
                campaign_day = day_offset + 1

                for slot_number, (pin, board) in enumerate(chunk, start=1):
                    exists = ScheduledPin.objects.filter(
                        pin=pin,
                        board=board,
                        publish_date=publish_date
                    ).exists()
                    if not exists:
                        ScheduledPin.objects.create(
                            pin=pin,
                            board=board,
                            publish_date=publish_date,
                            campaign_day=campaign_day,
                            slot_number=slot_number,
                            status='scheduled'
                        )
                        scheduled_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"✅ {scheduled_count} SmartLoop pins scheduled — 20/day for 30 days starting {next_monday}"
        ))