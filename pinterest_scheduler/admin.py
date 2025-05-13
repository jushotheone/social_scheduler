from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils import timezone
from django.shortcuts import render, redirect
from django.utils.html import format_html
from django.urls import path
from django.template.response import TemplateResponse
from django.db.models import Count, Q
from .models import Pillar, Headline
from datetime import timedelta
from django.db import transaction
from collections import defaultdict
import csv
import io
import random

from decimal import Decimal
from django.db.models import Max
from .models import Pillar, Headline, PinTemplateVariation, Board, ScheduledPin, Campaign, Keyword
from .forms import PinTemplateVariationForm, ScheduledPinForm, KeywordCSVUploadForm
import logging

logger = logging.getLogger(__name__)

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
    list_display = ['name', 'campaign','tagline', 'headline_progress', 'variation_progress']
    list_filter = ['campaign']

    def campaign(self, obj):
        return obj.campaign.name if obj.campaign else "—"
    campaign.short_description = "Campaign"

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
    search_fields = ['text']
    inlines = [VariationInline]


# ----------------------
# PIN TEMPLATE VARIATION ADMIN (structured with previews + grouping)
# ----------------------
@admin.register(PinTemplateVariation)
class PinTemplateVariationAdmin(admin.ModelAdmin):
    form = PinTemplateVariationForm
    change_list_template = "admin/change_list_with_upload_button.html"

    list_display = [
        'headline_display', 'title', 'pillar_preview', 'variation_position', 'thumbnail_preview',
        'cta', 'mockup_name', 'background_style', 'keyword_tiers'
    ]
    list_filter = ['headline__pillar', 'headline']
    filter_horizontal = ('keywords',)
    search_fields = ['cta', 'mockup_name', 'badge_icon', 'keywords__phrase']
    readonly_fields = ['pillar_preview', 'thumbnail_preview', 'variation_progress']
    list_select_related = ['headline__pillar']
    actions = ['auto_assign_keywords']

    fieldsets = (
        ('🧠 Content & Messaging', {
            'fields': ('headline', 'pillar_preview', 'title', 'variation_progress', 'description', 'cta', 'keywords'),
            'description': 'Tells the story for this pin variation — tied to headline and pillar.'
        }),
        ('🎨 Visual Design Details', {
            'fields': ('image_url', 'thumbnail_preview', 'background_style', 'mockup_name', 'badge_icon', 'link'),
            'description': 'These control the visual variation for the same message.'
        }),
    )


    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['upload_url'] = 'admin:upload_pin_variations_csv'
        extra_context['upload_label'] = 'Upload Pin Variations CSV'
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("upload-csv/", self.admin_site.admin_view(self.upload_pin_variations_csv), name="upload_pin_variations_csv"),
        ]
        return custom_urls + urls
    
    logger.info("🔁 upload_pin_variations_csv called")

    def upload_pin_variations_csv(self, request):
        if request.method == 'POST' and request.FILES.get('csv_file'):
            csv_file = request.FILES['csv_file']
            decoded = csv_file.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(decoded))

            added, skipped, errors = 0, 0, 0

            for row_num, row in enumerate(reader, start=2):  # header = row 1
                logger.debug(f"[Row {row_num}] Processing row: {row}")
                try:
                    campaign_name = row.get('campaign', '').strip()
                    pillar_name = row.get('pillar', '').strip()
                    headline_text = row.get('headline', '').strip().lower()
                    title = row.get('title', '').strip()
                    cta = row.get('cta', '').strip()
                    mockup = row.get('mockup_name', '').strip()
                    background = row.get('background_style', '').strip()
                    badge = row.get('badge_icon', '').strip()
                    image_url = row.get('image_url', '').strip()
                    description = row.get('description', '').strip()
                    link = row.get('link', '').strip()

                    if not all([campaign_name, pillar_name, headline_text, title, image_url, description]):
                        logger.debug(f"[Row {row_num}] Missing required fields: {row}")
                        skipped += 1
                        continue

                    try:
                        campaign = Campaign.objects.get(name=campaign_name)
                    except Campaign.DoesNotExist:
                        logger.debug(f"[Row {row_num}] Campaign not found: {campaign_name}")
                        skipped += 1
                        continue

                    try:
                        pillar = Pillar.objects.get(name=pillar_name, campaign=campaign)
                    except Pillar.DoesNotExist:
                        logger.debug(f"[Row {row_num}] Pillar not found: {pillar_name}")
                        skipped += 1
                        continue

                    headline = Headline.objects.filter(pillar=pillar, text__iexact=headline_text).first()

                    if not headline:
                        # 🔧 Create the headline if missing
                        headline = Headline.objects.create(pillar=pillar, text=headline_text.strip())
                        logger.debug(f"[Row {row_num}] Created new headline: {headline_text}")

                    # 🛡️ Defensive check: skip if a similar variation already exists
                    variation_exists = PinTemplateVariation.objects.filter(
                        headline=headline,
                        cta=cta,
                        mockup_name=mockup,
                        background_style=background,
                    ).filter(
                        Q(image_url=image_url) |
                        Q(title=title) |
                        Q(description=description)
                    ).exists()

                    if variation_exists:
                        logger.debug(f"[Row {row_num}] Variation already exists — skipping.")
                        skipped += 1
                        continue

                    # Assign next variation number (incremental)
                    last_number = headline.variations.aggregate(max_num=Max('variation_number'))['max_num'] or 0
                    variation_number = last_number + 1


                    PinTemplateVariation.objects.create(
                        headline=headline,
                        variation_number=variation_number,
                        title=title,
                        cta=cta,
                        background_style=background,
                        mockup_name=mockup,
                        badge_icon=badge,
                        image_url=image_url,
                        description=description,
                        link=link
                    )
                    logger.info(f"[Row {row_num}] ✅ Added variation: {title}")
                    added += 1

                except Exception as e:
                    logger.exception(f"[Row {row_num}] ❌ Error adding row: {e}")
                    errors += 1

            messages.success(request, f"✅ Added: {added} — 🔁 Skipped: {skipped} — ⚠️ Errors: {errors}")
            return redirect("..")

        messages.warning(request, "No CSV file uploaded.")
        return redirect("..")

    def auto_assign_keywords(self, request, queryset):
        keywords_by_tier = defaultdict(list)
        usage_counts = defaultdict(int)

        for k in Keyword.objects.all():
            keywords_by_tier[k.tier].append(k)

        assigned_total = 0
        for pin in queryset:
            pin.keywords.clear()

            def pick_keywords(tier, count, max_usage):
                pool = [k for k in keywords_by_tier[tier] if usage_counts[k.id] < max_usage]
                if len(pool) < count:
                    raise ValueError(f"Not enough keywords in tier: {tier}")
                selected = random.sample(pool, count)
                for k in selected:
                    usage_counts[k.id] += 1
                return selected

            try:
                high = pick_keywords('high', 3, 40)
                mid = pick_keywords('mid', 4, 30)
                niche = pick_keywords('niche', 3, 20)
                final_keywords = list(set(high + mid + niche))
                pin.keywords.set(final_keywords)
                assigned_total += 1
            except Exception as e:
                self.message_user(request, f"⚠️ Skipped pin {pin.id}: {e}", level=messages.WARNING)

        self.message_user(request, f"✅ Keywords assigned to {assigned_total} pins.", level=messages.SUCCESS)

    auto_assign_keywords.short_description = "🎯 Auto-assign 10 Keywords (3 High, 4 Mid, 3 Niche)"

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

    def keyword_tiers(self, obj):
        return ", ".join(set(k.tier for k in obj.keywords.all()))
    keyword_tiers.short_description = "Tiers"

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
    list_display = ['pin', 'board', 'campaign', 'publish_date', 'campaign_day', 'slot_number', 'posted'
    ]
    list_filter = ['campaign', 'board', 'publish_date', 'posted']
    actions = ['mark_as_posted', 'export_today_pins_csv', 'schedule_all_pins']
    list_select_related = ['campaign', 'pin', 'board']

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

class PillarInline(admin.TabularInline):
    model = Pillar
    extra = 0

@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ['name', 'start_date', 'end_date']
    inlines = [PillarInline]
    search_fields = ['name']
    ordering = ['start_date']


@admin.register(Keyword)
class KeywordAdmin(admin.ModelAdmin):
    list_display = ['phrase', 'avg_monthly_searches', 'competition', 'bid_low', 'bid_high', 'three_month_change', 'yoy_change', 'tier']
    search_fields = ['phrase']
    ordering = ['avg_monthly_searches']
    list_filter = ['competition', 'tier', 'currency', 'three_month_change', 'yoy_change']
    change_list_template = "admin/change_list_with_upload_button.html"

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['upload_url'] = 'admin:keywords_upload_csv'
        extra_context['upload_label'] = 'Upload Keyword CSV'
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('upload-csv/', self.admin_site.admin_view(self.process_csv_upload), name='keywords_upload_csv'),
        ]
        return custom_urls + urls

    def safe_int(self, val):
        try:
            return int(val.replace(',', '')) if val else 0
        except Exception:
            return 0

    def safe_float(self, val):
        try:
            return float(val.replace(',', '')) if val else 0.0
        except Exception:
            return 0.0

    def process_csv_upload(self, request):
        if request.method == 'POST':
            form = KeywordCSVUploadForm(request.POST, request.FILES)
            if form.is_valid():
                csv_file = form.cleaned_data['csv_file']
                decoded = csv_file.read().decode('utf-8')
                reader = csv.DictReader(io.StringIO(decoded))

                added = 0
                updated = 0
                errors = 0

                for row in reader:
                    phrase = row.get('Keyword', '').strip()
                    if not phrase:
                        continue

                    try:
                        volume = self.safe_int(row.get('Avg. monthly searches'))

                        # ✅ NEW LOGIC: Tier classification based on volume
                        if volume >= 1000:
                            tier = 'high'
                        elif 300 <= volume < 1000:
                            tier = 'mid'
                        elif 50 <= volume < 300:
                            tier = 'niche'
                        else:
                            tier = 'low'

                        obj, created = Keyword.objects.update_or_create(
                            phrase=phrase,
                            defaults={
                                'currency': row.get('Currency', '').strip(),
                                'avg_monthly_searches': volume,
                                'tier': tier,  # ✅ Save the calculated tier
                                'three_month_change': row.get('Three month change', '').strip(),
                                'yoy_change': row.get('YoY change', '').strip(),
                                'competition': row.get('Competition', '').strip(),
                                'competition_index': self.safe_float(row.get('Competition (indexed)')),
                                'bid_low': self.safe_float(row.get('Top of page bid (low range)')),
                                'bid_high': self.safe_float(row.get('Top of page bid (high range)')),
                                'searches_jan': self.safe_int(row.get('Searches: Jan')),
                                'searches_feb': self.safe_int(row.get('Searches: Feb')),
                                'searches_mar': self.safe_int(row.get('Searches: Mar')),
                                'searches_apr': self.safe_int(row.get('Searches: Apr')),
                                'searches_may': self.safe_int(row.get('Searches: May')),
                                'searches_jun': self.safe_int(row.get('Searches: Jun')),
                                'searches_jul': self.safe_int(row.get('Searches: Jul')),
                                'searches_aug': self.safe_int(row.get('Searches: Aug')),
                                'searches_sep': self.safe_int(row.get('Searches: Sep')),
                                'searches_oct': self.safe_int(row.get('Searches: Oct')),
                                'searches_nov': self.safe_int(row.get('Searches: Nov')),
                                'searches_dec': self.safe_int(row.get('Searches: Dec')),
                            }
                        )
                        added += 1 if created else 0
                        updated += 0 if created else 1

                    except Exception as e:
                        self.message_user(request, f"❌ Error processing '{phrase}': {e}", level=messages.WARNING)
                        errors += 1

                self.message_user(
                    request,
                    f"✅ {added} added, 🔁 {updated} updated, ⚠️ {errors} errors",
                    level=messages.SUCCESS
                )
                return redirect("..")

        return redirect("..")