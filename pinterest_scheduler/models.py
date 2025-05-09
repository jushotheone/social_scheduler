from django.db import models

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
    headline = models.ForeignKey(Headline, on_delete=models.CASCADE, related_name='variations')
    image = models.ImageField(upload_to='pins/')
    cta = models.CharField(max_length=100)
    background_style = models.CharField(max_length=100)
    mockup_name = models.CharField(max_length=100)
    badge_icon = models.CharField(max_length=100)
    description = models.TextField()
    link = models.URLField(blank=True, null=True)
    keywords = models.CharField(max_length=255, blank=True, help_text="Comma-separated keywords")
    
    def __str__(self):
        return f"Variation of: {self.headline.text[:40]}"

    def image_url(self):
        # Ensure full URL is used in export
        if self.image and hasattr(self.image, 'url'):
            return self.image.url
        return ""

class Board(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name

class ScheduledPin(models.Model):
    pin = models.ForeignKey(PinTemplateVariation, on_delete=models.CASCADE)
    board = models.ForeignKey(Board, on_delete=models.CASCADE)
    publish_date = models.DateField()
    slot_number = models.IntegerField()
    posted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.pin} → {self.board.name} on {self.publish_date}"