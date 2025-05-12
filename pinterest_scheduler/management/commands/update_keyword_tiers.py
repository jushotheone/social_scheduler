from django.core.management.base import BaseCommand
from pinterest_scheduler.models import Keyword

class Command(BaseCommand):
    help = "Assigns tier to each keyword based on avg_monthly_searches"

    def handle(self, *args, **options):
        updated = 0

        for kw in Keyword.objects.all():
            volume = kw.avg_monthly_searches or 0

            if volume >= 1000:
                kw.tier = 'high'
            elif 300 <= volume < 1000:
                kw.tier = 'mid'
            elif 50 <= volume < 300:
                kw.tier = 'niche'
            else:
                kw.tier = 'low'  # optional catch-all
            kw.save(update_fields=['tier'])
            updated += 1

        self.stdout.write(self.style.SUCCESS(f"✅ {updated} keywords updated with correct tier."))