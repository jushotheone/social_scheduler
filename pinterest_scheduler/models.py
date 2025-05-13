from django.db import models
from django.db import IntegrityError
from django.utils import timezone
from django.core.exceptions import ValidationError

class Campaign(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ['start_date', 'name']

    def __str__(self):
        return self.name


class Pillar(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='pillars', null=True, blank=True)
    name = models.CharField(max_length=100)
    tagline = models.CharField(max_length=255)

    class Meta:
        ordering = ['campaign__start_date', 'name']

    def __str__(self):
        return f"{self.campaign.name if self.campaign else 'Unassigned'} – {self.name}"

class Headline(models.Model):
    pillar = models.ForeignKey(Pillar, on_delete=models.CASCADE, related_name='headlines')
    text = models.TextField()

    class Meta:
        ordering = ['pillar__campaign__start_date', 'pillar__name', 'id']

    def __str__(self):
        return f"{self.pillar.name} – {self.text[:40]}"

class PinTemplateVariation(models.Model):
    headline = models.ForeignKey("Headline", on_delete=models.CASCADE, related_name='variations')
    variation_number = models.PositiveSmallIntegerField(null=True, blank=True)
    title = models.CharField(max_length=255, blank=True)  # ✅ Added title field
    image_url = models.URLField(max_length=500, default='')
    cta = models.CharField(max_length=100)
    background_style = models.CharField(max_length=100)
    mockup_name = models.CharField(max_length=100)
    badge_icon = models.CharField(max_length=100)
    description = models.CharField(max_length=500)
    link = models.URLField(blank=True, null=True)
    keywords = models.ManyToManyField('Keyword', blank=True, related_name='pin_variations')

    class Meta:
        unique_together = ('headline', 'variation_number')  # prevent dupes
        ordering = ['headline__pillar__name', 'variation_number']

    def __str__(self):
        number = self.variation_number

        # Dynamically suggest a variation number even before save
        if not number and self.pk is None and self.headline_id:
            existing = self.headline.variations.values_list('variation_number', flat=True)
            for i in range(1, 5):
                if i not in existing:
                    number = i
                    break
            else:
                number = "⚠️ Max reached"

        return f"Variation {number or '—'} of: {self.headline.text[:40]}"


class Board(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class ScheduledPin(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, null=True, blank=True, related_name='scheduled_pins')
    pin = models.ForeignKey(PinTemplateVariation, on_delete=models.CASCADE)
    board = models.ForeignKey(Board, on_delete=models.CASCADE)
    publish_date = models.DateField()
    campaign_day = models.PositiveSmallIntegerField(help_text="Campaign day from 1 to 30")
    slot_number = models.PositiveSmallIntegerField(help_text="Slot position for the day")
    posted = models.BooleanField(default=False)

    class Meta:
        ordering = ['publish_date', 'campaign_day', 'slot_number']

    def __str__(self):
        return f"{self.pin} → {self.board.name} on {self.publish_date}"
    
    def save(self, *args, **kwargs):
        if not self.campaign:
            self.campaign = self.pin.headline.pillar.campaign
        super().save(*args, **kwargs)

class Keyword(models.Model):
    phrase = models.CharField(max_length=255, unique=True)
    currency = models.CharField(max_length=10)
    avg_monthly_searches = models.PositiveIntegerField()
    tier = models.CharField(max_length=10, choices=[("high", "High"), ("mid", "Mid"), ("niche", "Niche")])
    three_month_change = models.CharField(max_length=20)
    yoy_change = models.CharField(max_length=20)
    competition = models.CharField(max_length=20)
    competition_index = models.FloatField()
    bid_low = models.FloatField()
    bid_high = models.FloatField()

    # Optional monthly breakdown fields (denormalised)
    searches_jan = models.PositiveIntegerField(null=True, blank=True)
    searches_feb = models.PositiveIntegerField(null=True, blank=True)
    searches_mar = models.PositiveIntegerField(null=True, blank=True)
    searches_apr = models.PositiveIntegerField(null=True, blank=True)
    searches_may = models.PositiveIntegerField(null=True, blank=True)
    searches_jun = models.PositiveIntegerField(null=True, blank=True)
    searches_jul = models.PositiveIntegerField(null=True, blank=True)
    searches_aug = models.PositiveIntegerField(null=True, blank=True)
    searches_sep = models.PositiveIntegerField(null=True, blank=True)
    searches_oct = models.PositiveIntegerField(null=True, blank=True)
    searches_nov = models.PositiveIntegerField(null=True, blank=True)
    searches_dec = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-avg_monthly_searches']

    def __str__(self):
        return self.phrase