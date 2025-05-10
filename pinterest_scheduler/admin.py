from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils import timezone
from django.utils.html import format_html
from django.urls import path
from django.template.response import TemplateResponse
from django.db.models import Count
from .models import Pillar, Headline
from datetime import timedelta
from django.db import transaction
import csv
from .models import Pillar, Headline, PinTemplateVariation, Board, ScheduledPin
from .forms import PinTemplateVariationForm, ScheduledPinForm

# -----------------------
# CUSTOM ADMIN ACTIONS
# -----------------------

class PinterestSchedulerAdminSite(admin.AdminSite):
    site_header = "Pinterest Scheduler Admin"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('pinterest-summary/', self.admin_view(self.pinterest_summary), name='pinterest_summary'),
        ]
        return custom_urls + urls

    def pinterest_summary(self, request):
        data = []
        for pillar in Pillar.objects.all():
            headlines = pillar.headlines.annotate(variation_count=Count('variations'))
            variation_total = sum(h.variation_count for h in headlines)
            data.append({
                'pillar': pillar.name,
                'headline_count': f"{headlines.count()} / 5",
                'variation_count': f"{variation_total} / 20",
                'status': "✅ 100%" if variation_total == 20 else "🔄 In Progress",
            })
        return TemplateResponse(request, "admin/pinterest_summary.html", {'data': data})




# ----------------------
# PILLAR ADMIN
# ----------------------
@admin.register(Pillar)
class PillarAdmin(admin.ModelAdmin):
    list_display = ['name', 'tagline', 'headline_progress', 'variation_progress']

    def headline_progress(self, obj):
        count = obj.headlines.count()
        return f"{count} / 5"
    headline_progress.short_description = "Headlines"

    def variation_progress(self, obj):
        total = sum(h.variations.count() for h in obj.headlines.all())
        return f"{total} / 20"
    variation_progress.short_description = "Variations"

# ----------------------
# INLINE for HEADLINE (shows 4 pin variations per headline)
# ----------------------
class PinTemplateVariationInline(admin.TabularInline):
    model = PinTemplateVariation
    extra = 0
    fields = ['image_preview', 'cta', 'mockup_name', 'background_style', 'badge_icon', 'link']
    readonly_fields = ['image_preview']
    show_change_link = True

    def image_preview(self, obj):
        if obj.image_url:
            return format_html('<img src="{}" style="width: 80px; height:auto;" />', obj.image_url)
        return "No Image"
    image_preview.short_description = 'Preview'

# ----------------------
# HEADLINE ADMIN (with inline preview of variations)
# ----------------------

class VariationInline(admin.TabularInline):
    model = PinTemplateVariation
    fields = ['variation_number', 'cta', 'mockup_name', 'image_url']
    readonly_fields = ['variation_number', 'cta', 'mockup_name', 'image_url']
    extra = 0
    can_delete = False
    show_change_link = True

@admin.register(Headline)
class HeadlineAdmin(admin.ModelAdmin):
    list_display = ['pillar', 'text']
    list_filter = ['pillar']
    inlines = [VariationInline]

# ----------------------
# PIN TEMPLATE VARIATION ADMIN (structured with previews + grouping)
# ----------------------
@admin.register(PinTemplateVariation)
class PinTemplateVariationAdmin(admin.ModelAdmin):
    form = PinTemplateVariationForm 
    list_display = [
        'headline_display', 'pillar_preview', 'variation_position', 'thumbnail_preview',
        'cta', 'mockup_name', 'background_style'
    ]
    list_filter = ['headline__pillar', 'headline']
    search_fields = ['cta', 'mockup_name', 'badge_icon', 'keywords']
    readonly_fields = ['pillar_preview', 'thumbnail_preview', 'variation_progress']

    fieldsets = (
        ('🧠 Content & Messaging', {
            'fields': ('headline', 'pillar_preview', 'variation_progress', 'description', 'cta', 'keywords'),
            'description': 'Tells the story for this pin variation — tied to headline and pillar.'
        }),
        ('🎨 Visual Design Details', {
            'fields': ('image_url', 'thumbnail_preview', 'background_style', 'mockup_name', 'badge_icon', 'link'),
            'description': 'These control the visual variation for the same message.'
        }),
    )

    def pillar_preview(self, obj):
        return obj.headline.pillar.name if obj.headline and obj.headline.pillar else "-"
    pillar_preview.short_description = 'Pillar'

    def thumbnail_preview(self, obj):
        if obj.image_url:
            return format_html('<img src="{}" style="width: 60px; height:auto;" />', obj.image_url)
        return "No Image"
    thumbnail_preview.short_description = 'Preview'

    def variation_position(self, obj):
        siblings = list(obj.headline.variations.order_by('id'))
        if obj.pk in [s.pk for s in siblings]:
            index = siblings.index(obj) + 1
            return f"Variation {index} of {len(siblings)}"
        return "-"
    variation_position.short_description = 'Variation'

    def variation_progress(self, obj):
        count = obj.headline.variations.count()
        if count >= 4:
            return format_html('<span style="color: red;">⚠️ {}/4 variations created (FULL)</span>', count)
        else:
            return format_html('<span style="color: green;">{}/4 variations created</span>', count)
    variation_progress.short_description = 'Variation Progress'

    def headline_display(self, obj):
        return f"{obj.headline.pillar.name} — {obj.headline.text[:50]}"
    headline_display.short_description = 'Headline'

# ----------------------
# BOARD ADMIN
# ----------------------
@admin.register(Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']

# ----------------------
# SCHEDULED PIN ADMIN (with export + mark posted actions)
# ----------------------
@admin.register(ScheduledPin)
class ScheduledPinAdmin(admin.ModelAdmin):
    form = ScheduledPinForm
    list_display = ['pin', 'board', 'publish_date', 'campaign_day', 'slot_number', 'posted']
    list_filter = ['board', 'publish_date', 'posted']
    actions = ['mark_as_posted', 'export_today_pins_csv', 'schedule_all_pins']

    # ✅ Auto-assign logic for manual entry
    def save_model(self, request, obj, form, change):
        if not obj.pk:
            boards = list(Board.objects.all())[:5]
            today = timezone.now().date()
            days_until_monday = (7 - today.weekday()) % 7 or 7
            next_monday = today + timedelta(days=days_until_monday)

            total_pins = ScheduledPin.objects.filter().count()
            campaign_day = (total_pins // 20) + 1  # 20 pins per day
            slot_number = ScheduledPin.objects.filter(campaign_day=campaign_day).count() + 1
            publish_date = next_monday + timedelta(days=campaign_day - 1)

            obj.campaign_day = campaign_day
            obj.slot_number = slot_number
            obj.publish_date = publish_date

        super().save_model(request, obj, form, change)

    @admin.action(description="✅ Mark selected pins as posted")
    def mark_as_posted(self, request, queryset):
        queryset.update(posted=True)

    @admin.action(description="📤 Export selected pins (or today’s) to Pinterest CSV")
    def export_today_pins_csv(self, request, queryset):
        # Use your existing CSV export code here
        pass

    @admin.action(description="📅 Auto-Schedule All Pins (120 variations → 600 pins)")
    def schedule_all_pins(self, request, queryset):
        boards = list(Board.objects.all())[:5]
        pins = list(PinTemplateVariation.objects.all().order_by('id'))

        if len(boards) < 5:
            self.message_user(request, "❌ You need at least 5 boards.", level=messages.ERROR)
            return

        if len(pins) != 120:
            self.message_user(request, f"⚠️ Found {len(pins)} variations (expected 120). Proceeding anyway.", level=messages.WARNING)

        today = timezone.now().date()
        days_until_monday = (7 - today.weekday()) % 7 or 7
        next_monday = today + timedelta(days=days_until_monday)

        scheduled_count = 0

        with transaction.atomic():
            for i, pin in enumerate(pins):
                campaign_day = (i // 4) + 1
                publish_date = next_monday + timedelta(days=campaign_day - 1)

                for slot, board in enumerate(boards, start=1):
                    if not ScheduledPin.objects.filter(pin=pin, board=board).exists():
                        ScheduledPin.objects.create(
                            pin=pin,
                            board=board,
                            campaign_day=campaign_day,
                            publish_date=publish_date,
                            slot_number=slot,
                            posted=False
                        )
                        scheduled_count += 1

        self.message_user(
            request,
            f"✅ {scheduled_count} pins scheduled across 30 days starting {next_monday}",
            level=messages.SUCCESS
        )