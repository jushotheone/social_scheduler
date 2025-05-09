from django.contrib import admin
from django.http import HttpResponse
from django.utils.html import format_html
from django.utils import timezone
import csv

from .models import Pillar, Headline, PinTemplateVariation, Board, ScheduledPin

@admin.register(Pillar)
class PillarAdmin(admin.ModelAdmin):
    list_display = ['name', 'tagline']

@admin.register(Headline)
class HeadlineAdmin(admin.ModelAdmin):
    list_display = ['pillar', 'text']
    list_filter = ['pillar']

@admin.register(PinTemplateVariation)
class PinTemplateVariationAdmin(admin.ModelAdmin):
    list_display = [
        'headline', 'pillar_preview', 'thumbnail_preview',
        'cta', 'mockup_name', 'background_style'
    ]
    list_filter = ['headline__pillar']
    search_fields = ['cta', 'mockup_name', 'badge_icon', 'keywords']

    readonly_fields = ['pillar_preview', 'thumbnail_preview']

    fieldsets = (
        ('🧠 Content & Messaging', {
            'fields': ('headline', 'pillar_preview', 'description', 'cta', 'keywords'),
            'description': 'Tells the story for this pin variation — tied to headline and pillar.'
        }),
        ('🎨 Visual Design Details', {
            'fields': ('image', 'thumbnail_preview', 'background_style', 'mockup_name', 'badge_icon'),
            'description': 'These control the visual variation for the same message.'
        }),
    )

    def pillar_preview(self, obj):
        return obj.headline.pillar.name if obj.headline and obj.headline.pillar else "-"
    pillar_preview.short_description = 'Pillar'

    def thumbnail_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="width: 60px; height:auto;" />', obj.image.url)
        return "No Image"
    thumbnail_preview.short_description = 'Preview'

@admin.register(Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']

@admin.register(ScheduledPin)
class ScheduledPinAdmin(admin.ModelAdmin):
    list_display = ['pin', 'board', 'publish_date', 'slot_number', 'posted']
    list_filter = ['board', 'publish_date', 'posted']
    actions = ['mark_as_posted', 'export_today_pins_csv']

    @admin.action(description="✅ Mark selected pins as posted")
    def mark_as_posted(self, request, queryset):
        queryset.update(posted=True)

    @admin.action(description="📤 Export selected pins (or today's) to Pinterest CSV")
    def export_today_pins_csv(self, request, queryset):
        today = timezone.now().date()
        pins = queryset or ScheduledPin.objects.filter(publish_date=today)

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="pins_{today}.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'Title', 'Media URL', 'Pinterest board', 'Description',
            'Link', 'Publish date', 'Keywords'
        ])

        for scheduled in pins:
            pin = scheduled.pin
            writer.writerow([
                pin.headline.text[:100],
                pin.image.url if pin.image else '',
                scheduled.board.name,
                pin.description[:500],
                pin.link or '',
                scheduled.publish_date.isoformat(),
                getattr(pin, 'keywords', ''),
            ])

        return response