from django.core.management.base import BaseCommand
from pinterest_scheduler.models import Pillar

class Command(BaseCommand):
    help = 'Show a summary of headlines and template variations per pillar'

    def handle(self, *args, **kwargs):
        for pillar in Pillar.objects.all():
            headlines = pillar.headlines.count()
            variations = sum(h.variations.count() for h in pillar.headlines.all())
            pct = int((variations / (headlines * 4)) * 100) if headlines else 0
            self.stdout.write(
                f"{pillar.name}: {headlines} headlines — {variations}/"
                f"{headlines * 4} variations — {pct}% complete"
            )