from django.db import models
from django.db import IntegrityError
from django.utils import timezone
from django.core.exceptions import ValidationError

class Pillar(models.Model):
    name = models.CharField(max_length=100)
    tagline = models.CharField(max_length=255)

    def __str__(self):
        return self.name

class Headline(models.Model):
    pillar = models.ForeignKey(Pillar, on_delete=models.CASCADE, related_name='headlines')
    text = models.TextField()

    def __str__(self):
        return f"{self.pillar.name} – {self.text[:40]}"

class PinTemplateVariation(models.Model):
    headline = models.ForeignKey("Headline", on_delete=models.CASCADE, related_name='variations')
    variation_number = models.PositiveSmallIntegerField(null=True, blank=True)
    image_url = models.URLField(default='')
    cta = models.CharField(max_length=100)
    background_style = models.CharField(max_length=100)
    mockup_name = models.CharField(max_length=100)
    badge_icon = models.CharField(max_length=100)
    description = models.TextField()
    link = models.URLField(blank=True, null=True)
    keywords = models.CharField(max_length=255, blank=True, help_text="Comma-separated keywords")

    class Meta:
        unique_together = ('headline', 'variation_number')  # prevent dupes

    def save(self, *args, **kwargs):
        if self.variation_number is None:
            existing = self.headline.variations.values_list('variation_number', flat=True)
            for i in range(1, 5):
                if i not in existing:
                    self.variation_number = i
                    break
            else:
                raise ValidationError("This headline already has 4 variations.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Variation {self.variation_number} of: {self.headline.text[:40]}"


class Board(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name

class ScheduledPin(models.Model):
    pin = models.ForeignKey(PinTemplateVariation, on_delete=models.CASCADE)
    board = models.ForeignKey(Board, on_delete=models.CASCADE)
    publish_date = models.DateField()
    campaign_day = models.PositiveSmallIntegerField(help_text="Campaign day from 1 to 30")
    slot_number = models.PositiveSmallIntegerField(help_text="Slot position for the day")
    posted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.pin} → {self.board.name} on {self.publish_date}"