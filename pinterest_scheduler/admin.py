from django.contrib import admin, messages
from django.http import HttpResponse, HttpResponseRedirect
from django.utils import timezone
from django.shortcuts import render, redirect
from django.utils.html import format_html
from urllib.parse import unquote as urlunquote
from django.urls import path
from django.template.response import TemplateResponse
from django.db.models import Count, Q
from .models import Pillar, Headline
from datetime import timedelta, datetime
from django.db import transaction
from collections import defaultdict
import csv
import io
import random
from decimal import Decimal
from django.db.models import Max
from .models import Pillar, Headline, PinTemplateVariation, Board, ScheduledPin, Campaign, Keyword, PinKeywordAssignment
from .forms import PinTemplateVariationForm, ScheduledPinForm, KeywordCSVUploadForm
from pinterest_scheduler.services.exporter import export_scheduled_pins_to_csv
from django.utils.timezone import now, localtime, make_aware
import zipfile
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
# PIN KEYWORD ASSIGNMENT ADMIN (for auto-assigning keywords)
# ----------------------
class PinKeywordInline(admin.TabularInline):
    model = PinKeywordAssignment
    extra = 0
    autocomplete_fields = ['keyword']
    readonly_fields = ['assigned_at', 'auto_assigned']


# ----------------------
# PIN TEMPLATE VARIATION ADMIN (structured with previews + grouping)
# ----------------------

class HasKeywordsFilter(admin.SimpleListFilter):
    title = 'Has Keywords'
    parameter_name = 'has_keywords'

    def lookups(self, request, model_admin):
        return (
            ('yes', 'Yes'),
            ('no', 'No'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(keywords__isnull=False).distinct()
        if self.value() == 'no':
            return queryset.filter(keywords__isnull=True)
        
@admin.register(PinTemplateVariation)
class PinTemplateVariationAdmin(admin.ModelAdmin):
    form = PinTemplateVariationForm
    change_list_template = "admin/change_list_with_upload_button.html"

    list_display = [
        'headline_display', 'title', 'pillar_preview', 'variation_position', 'thumbnail_preview',
        'cta', 'mockup_name', 'background_style', 'keyword_list'
    ]
    list_filter = ['headline__pillar', 'headline', HasKeywordsFilter]
    inlines = [PinKeywordInline]
    # filter_horizontal = ('keywords',)
    search_fields = ['cta', 'mockup_name', 'badge_icon']
    readonly_fields = ['pillar_preview', 'thumbnail_preview', 'variation_progress']
    list_select_related = ['headline__pillar']
    actions = ['auto_assign_keywords', 'smartloop_schedule']

    fieldsets = (
        ('🧠 Content & Messaging', {
            'fields': ('headline', 'pillar_preview', 'title', 'variation_progress', 'description', 'cta'),
            'description': 'Tells the story for this pin variation — tied to headline and pillar.'
        }),
        ('🎨 Visual Design Details', {
            'fields': ('image_url', 'thumbnail_preview', 'background_style', 'mockup_name', 'badge_icon', 'link'),
            'description': 'These control the visual variation for the same message.'
        }),
    )

    def headline_display(self, obj):
        return f"{obj.headline.pillar.name} — {obj.headline.text[:50]}"
    headline_display.short_description = 'Headline'

    def pillar_preview(self, obj):
        return obj.headline.pillar.name if obj.headline and obj.headline.pillar else "-"
    pillar_preview.short_description = 'Pillar'

    def variation_position(self, obj):
        siblings = list(obj.headline.variations.order_by('id'))
        if obj.pk in [s.pk for s in siblings]:
            index = siblings.index(obj) + 1
            return f"Variation {index} of {len(siblings)}"
        return "-"
    variation_position.short_description = 'Variation Position'

    def thumbnail_preview(self, obj):
        if obj.image_url:
            return format_html('<img src="{}" style="width: 60px; height:auto;" />', obj.image_url)
        return "No Image"
    thumbnail_preview.short_description = 'Thumbnail Preview'

    def variation_progress(self, obj):
        count = obj.headline.variations.count()
        if count >= 4:
            return format_html('<span style="color: red;">⚠️ {}/4 variations created (FULL)</span>', count)
        else:
            return format_html('<span style="color: green;">{}/4 variations created</span>', count)
    variation_progress.short_description = 'Variation Progress'

    @admin.display(description='Keywords')
    def keyword_list(self, obj):
        return ', '.join(k.phrase for k in obj.keywords.all())

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
        from random import randint
        from collections import defaultdict
        import logging

        logger = logging.getLogger(__name__)
        keywords_by_tier = defaultdict(list)

        logger.info("🔁 Smart keyword assignment with global rotation")

        # Step 1: Global usage tracker — ALL keywords already in use across the DB
        global_usage = defaultdict(int)
        for pin in PinTemplateVariation.objects.prefetch_related('keywords'):
            for kw in pin.keywords.all():
                global_usage[kw.id] += 1

        # Step 2: Bucket keywords by tier
        all_keywords = Keyword.objects.all()
        for k in all_keywords:
            keywords_by_tier[k.tier].append(k)

        assigned_total = 0
        used_keywords = set()

        for pin in queryset:
            pin.keywords.clear()

            # Smart tier balancing with overflow prevention
            def smart_mix(max_keywords=7):
                high = min(len(keywords_by_tier['high']), randint(2, 4))
                mid = min(len(keywords_by_tier['mid']), randint(1, 2))
                niche = min(len(keywords_by_tier['niche']), randint(1, 2))

                total = high + mid + niche
                if total > max_keywords:
                    overflow = total - max_keywords
                    while overflow > 0:
                        if niche > 1:
                            niche -= 1
                        elif mid > 1:
                            mid -= 1
                        elif high > 2:
                            high -= 1
                        overflow -= 1
                return {'high': high, 'mid': mid, 'niche': niche}

            # Prioritise least-used keywords first
            def pick_keywords(tier, count):
                pool = [k for k in keywords_by_tier[tier] if global_usage.get(k.id, 0) == 0]
                if len(pool) < count:
                    logger.warning(f"⚠️ Not enough unused {tier}-tier keywords. Allowing reuse.")
                    pool = sorted(keywords_by_tier[tier], key=lambda k: global_usage.get(k.id, 0))
                if len(pool) < count:
                    raise ValueError(f"Not enough keywords in tier: {tier}")
                selected = pool[:count]
                for k in selected:
                    global_usage[k.id] += 1
                    used_keywords.add(k.id)
                return selected

            try:
                mix = smart_mix()
                selected_keywords = (
                    pick_keywords('high', mix['high']) +
                    pick_keywords('mid', mix['mid']) +
                    pick_keywords('niche', mix['niche'])
                )
                pin.keywords.set(selected_keywords)
                assigned_total += 1
            except Exception as e:
                self.message_user(request, f"⚠️ Skipped pin {pin.id}: {e}", level=messages.WARNING)

        self.message_user(
            request,
            f"✅ Keywords assigned (smart mix, no overuse) to {assigned_total} pins.",
            level=messages.SUCCESS
        )

        # ✅ Log unused keywords to console
        unused_keywords = [k.phrase for k in all_keywords if k.id not in used_keywords]
        logger.info(f"📉 Unused keywords: {len(unused_keywords)}")
        for phrase in unused_keywords:
            print(f"🔍 Unused: {phrase}")

    @admin.action(description="📅 SmartLoop: Schedule 120 pins × 5 boards (20/day × 30 days)")
    def smartloop_schedule(self, request, queryset, dry_run=False, preview=False):
        boards = list(Board.objects.all())[:5]
        pins  = list(PinTemplateVariation.objects.select_related('headline__pillar').all())  # 120 unique

        # 1. Bucket pins into 6 groups of 20
        random.shuffle(pins)
        day_buckets = [pins[i*20:(i+1)*20] for i in range(6)]

        # 2. Compute next Monday
        today = timezone.now().date()
        days_until_mon = (7 - today.weekday()) % 7 or 7
        start = today + timedelta(days=days_until_mon)

        # 3. Build schedule_by_day and collect per-pillar counts
        schedule_by_day   = defaultdict(list)
        pillar_diagnostics = defaultdict(lambda: defaultdict(int))  # pillar_diagnostics[date][pillar] = count

        # ✅ IMPROVED LOGIC: Each pin 5×, 6-day spaced, full rotation
        random.shuffle(pins)  # shuffle to introduce variety

        for pin_index, pin in enumerate(pins):  # 120 pins
            for rot in range(5):  # 5 board assignments
                day_offset = (pin_index + rot * 6) % 30  # 6 days apart
                pub_date = start + timedelta(days=day_offset)
                board = boards[rot]  # rotate across boards in order
                schedule_by_day[pub_date].append((pin, board))
                pillar_diagnostics[pub_date][pin.headline.pillar.name] += 1

        # 4. Validate 20 pins/day
        for pub_date, items in schedule_by_day.items():
            if len(items) != 20:
                self.message_user(
                    request,
                    f"❌ Scheduling error: {pub_date} has {len(items)} pins (expected 20).",
                    level=messages.ERROR
                )
                return

        # 5. Generate CSV preview
        csv_buffer = io.StringIO()
        csv_writer = csv.writer(csv_buffer)
        csv_writer.writerow(['publish_date','campaign_day','slot_number','pin_id','pillar','board_id'])
        for pub_date, items in sorted(schedule_by_day.items()):
            campaign_day = (pub_date - start).days + 1
            for slot_num, (pin, board) in enumerate(items, start=1):
                csv_writer.writerow([
                    pub_date.isoformat(),
                    campaign_day,
                    slot_num,
                    pin.id,
                    pin.headline.pillar.name,
                    board.id
                ])
        # Write file for download or storage
        with open('/tmp/pins_schedule.csv','w', newline='') as f:
            f.write(csv_buffer.getvalue())

        # 6. If dry_run, bail here after diagnostics & CSV export
        if dry_run:
            # show pillar spread summary in messages
            for date, diag in sorted(pillar_diagnostics.items()):
                spread = ', '.join(f"{pillar}:{count}" for pillar,count in diag.items())
                self.message_user(request, f"{date}: {spread}", level=messages.INFO)
            self.message_user(
                request,
                "✅ Dry run complete. CSV written to /tmp/pins_schedule.csv",
                level=messages.SUCCESS
            )
            return

        # 7. Actual DB write (or preview mode)
        with transaction.atomic():
            # clear existing for the month
            ScheduledPin.objects.filter(
                publish_date__range=(start, start+timedelta(days=29))
            ).delete()
            if preview:
                # bulk-create in SmartLoopPreview for admin UI
                preview_objs = []
                for pub_date, items in schedule_by_day.items():
                    campaign_day = (pub_date - start).days + 1
                    for slot_num, (pin, board) in enumerate(items, start=1):
                        preview_objs.append(
                            SmartLoopPreview(
                                pin=pin,
                                board=board,
                                publish_date=pub_date,
                                campaign_day=campaign_day,
                                slot_number=slot_num,
                            )
                        )
                SmartLoopPreview.objects.bulk_create(preview_objs)
                self.message_user(
                    request,
                    f"✅ Preview created ({len(preview_objs)} entries). Check SmartLoopPreview in admin.",
                    level=messages.SUCCESS
                )
            else:
                # real scheduling
                for pub_date, items in schedule_by_day.items():
                    campaign_day = (pub_date - start).days + 1
                    for slot_num, (pin, board) in enumerate(items, start=1):
                        ScheduledPin.objects.create(
                            pin=pin,
                            board=board,
                            publish_date=pub_date,
                            campaign_day=campaign_day,
                            slot_number=slot_num,
                            status='scheduled'
                        )
                self.message_user(
                    request,
                    "✅ 600 pins scheduled: 20/day, 6-day spacing, 5× per pin, CSV backup at /tmp/pins_schedule.csv",
                    level=messages.SUCCESS
                )
        
    auto_assign_keywords.short_description = "🎯 Smart Assign Keywords (Balanced + Unique)"

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
    change_list_template = "admin/scheduled_pins_changelist.html"
    list_display = ['pin', 'board', 'campaign', 'publish_date', 'campaign_day', 'slot_number', 'status']
    list_filter = ['campaign', 'board', 'publish_date', 'status']
    list_select_related = ['campaign', 'pin', 'board']
    actions = ['mark_as_posted']

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("export-today/", self.admin_site.admin_view(self.export_today_csv), name="export_today_csv"),
        ]
        return custom_urls + urls

    def export_today_csv(self, request):
        target_date = request.GET.get("date")
        dry_run = request.GET.get("dry_run") == "1"
        include_zip = request.GET.get("bundle") == "1"
        board_slug = request.GET.get("board")
        campaign_id = request.GET.get("campaign")

        today = now().date()
        target_date = target_date or today
        try:
            target_date = timezone.datetime.strptime(str(target_date), "%Y-%m-%d").date()
        except Exception:
            self.message_user(request, f"❌ Invalid date format: {target_date}", level=messages.ERROR)
            return HttpResponse(status=400)

        queryset = ScheduledPin.objects.filter(publish_date=target_date).select_related(
            'pin__headline__pillar', 'board', 'campaign'
        )

        if board_slug:
            queryset = queryset.filter(board__slug=board_slug)
        if campaign_id:
            queryset = queryset.filter(campaign_id=campaign_id)

        if not queryset.exists():
            self.message_user(request, f"⚠️ No scheduled pins found for {target_date}.", level=messages.WARNING)
            return HttpResponse(status=204)

        if dry_run:
            for pin in queryset:
                title = pin.pin.title or pin.pin.headline.text
                self.message_user(
                    request,
                    f"{pin.publish_date} | {pin.board.name} | {title}",
                    level=messages.INFO
                )
            self.message_user(request, f"✅ Dry run complete for {target_date}.", level=messages.SUCCESS)
            return HttpResponse("Dry run complete")

        # Build CSV
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["Board", "Title", "Description", "Link", "Image URL", "Alt Text"])

        image_urls = []

        for pin in queryset:
            title = pin.pin.title or pin.pin.headline.text
            if not title:
                self.message_user(request, f"❌ Missing title for pin ID {pin.pin.id}.", level=messages.ERROR)
                return HttpResponse(status=400)

            writer.writerow([
                pin.board.name,
                title,
                pin.pin.description or "",
                pin.pin.link or "",
                pin.pin.image_url,
                pin.pin.cta or pin.pin.headline.pillar.tagline
            ])

            if include_zip:
                image_urls.append((title, pin.pin.image_url))

        csv_filename = f"scheduled_pins_{target_date}.csv"
        csv_bytes = io.BytesIO()
        csv_bytes.write(csv_buffer.getvalue().encode("utf-8"))
        csv_bytes.seek(0)

        if include_zip:
            # Bundle CSV + placeholders for images into zip
            zip_stream = io.BytesIO()
            with zipfile.ZipFile(zip_stream, "w", zipfile.ZIP_DEFLATED) as zipf:
                zipf.writestr(csv_filename, csv_bytes.read())
                for title, url in image_urls:
                    zipf.writestr(f"images/{title[:50]}.txt", f"Image URL: {url}")
            zip_stream.seek(0)
            response = FileResponse(zip_stream, as_attachment=True, filename=f"scheduled_pins_bundle_{target_date}.zip")
        else:
            response = HttpResponse(csv_bytes, content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="{csv_filename}"'

        return response

    @admin.action(description="✅ Mark selected pins as posted")
    def mark_as_posted(self, request, queryset):
        queryset.update(status='posted')

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
    list_display = ['phrase', 'avg_monthly_searches', 'competition', 'bid_low', 'bid_high', 'three_month_change', 'used_in_pins', 'yoy_change', 'tier']
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
        
    def used_in_pins(self, obj):
        return obj.pin_variations.count()
    used_in_pins.short_description = 'Used In Pins'

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
    

def get_filtered_pins(request, target_date):
    board_slug = request.GET.get("board")
    campaign_slug = request.GET.get("campaign")

    queryset = ScheduledPin.objects.filter(
        publish_date=target_date
    ).select_related('pin__headline__pillar', 'board', 'campaign')

    if board_slug:
        queryset = queryset.filter(board__slug=urlunquote(board_slug))

    if campaign_slug:
        queryset = queryset.filter(campaign__name__iexact=urlunquote(campaign_slug))

    return queryset

@admin.site.admin_view
def export_today_csv(request):
    date_str = request.GET.get("date")
    interval_minutes = int(request.GET.get("interval", 60))  # default 1 hour
    start_str = request.GET.get("start")
    allow_all_hours = request.GET.get("all_hours") == "1"

    # Parse target date
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else now().date()
    current_time = localtime(now())

    # Determine staggered start time
    if start_str:
        try:
            hour, minute = map(int, start_str.split(":"))
            start_time = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
            start_time = make_aware(start_time)
            start_time = localtime(start_time)
        except ValueError:
            messages.warning(request, "⚠️ Invalid start time format. Use HH:MM.")
            return HttpResponseRedirect(request.META.get("HTTP_REFERER", "/admin/"))
    else:
        start_time = current_time.replace(hour=current_time.hour, minute=0, second=0, microsecond=0)

    # Ensure timezone-aware start_time
    start_time = localtime(start_time)

    # Smart gap window (9:00–21:00)
    smart_start = start_time.replace(hour=9, minute=0)
    smart_end = start_time.replace(hour=21, minute=0)

    pins = get_filtered_pins(request, target_date)
    if not pins.exists():
        messages.warning(request, f"⚠️ No scheduled pins found for {target_date}")
        return HttpResponseRedirect(request.META.get("HTTP_REFERER", "/admin/"))

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="scheduled_pins_{target_date}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "Title",
        "Media URL",
        "Pinterest board",
        "Thumbnail",
        "Description",
        "Link",
        "Publish date",
        "Keywords"
    ])


    for i, pin in enumerate(pins):
        publish_time = start_time + timedelta(minutes=i * interval_minutes)

        if not allow_all_hours:
            if publish_time.time() < smart_start.time():
                continue
            if publish_time.time() > smart_end.time():
                messages.warning(request, f"⛔ Export stopped at {publish_time.strftime('%H:%M')} – exceeds 21:00")
                break

        title = pin.pin.title[:100]
        writer.writerow([
            title,
            pin.pin.image_url,
            pin.board.name,
            "",  # Thumbnail
            pin.pin.description or "",
            pin.pin.link or "",
            publish_time.isoformat(),
            ", ".join([kw.phrase for kw in pin.pin.keywords.all()])
        ])


    return response


@admin.site.admin_view
def dry_run_preview(request):
    date_str = request.GET.get("date")
    interval_minutes = int(request.GET.get("interval", 60))
    start_str = request.GET.get("start")
    allow_all_hours = request.GET.get("all_hours") == "1"

    target_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else now().date()
    current_time = localtime(now())

    if start_str:
        try:
            hour, minute = map(int, start_str.split(":"))
            start_time = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
            start_time = make_aware(start_time)
            start_time = localtime(start_time)
        except ValueError:
            messages.warning(request, "⚠️ Invalid start time format. Use HH:MM.")
            return HttpResponseRedirect(request.META.get("HTTP_REFERER", "/admin/"))
    else:
        start_time = current_time.replace(hour=current_time.hour, minute=0, second=0, microsecond=0)

    # Make timezone-aware
    start_time = localtime(start_time)

    # Smart hours: 09:00–21:00
    smart_start = start_time.replace(hour=9, minute=0)
    smart_end = start_time.replace(hour=21, minute=0)

    pins = get_filtered_pins(request, target_date)
    if not pins.exists():
        messages.warning(request, f"⚠️ No scheduled pins found for {target_date}")
        return HttpResponseRedirect(request.META.get("HTTP_REFERER", "/admin/"))

    messages.info(request, f"📅 Dry run for {target_date} | {pins.count()} pins")

    for i, pin in enumerate(pins):
        publish_time = start_time + timedelta(minutes=i * interval_minutes)

        if not allow_all_hours:
            if publish_time.time() < smart_start.time():
                continue
            if publish_time.time() > smart_end.time():
                messages.warning(request, f"⛔ Preview stopped at {publish_time.strftime('%H:%M')} – exceeds 21:00")
                break

        title = pin.pin.title or pin.pin.headline.text[:60]
        messages.info(request, f"🕒 {publish_time.strftime('%H:%M')} | {pin.board.name} | {title}")

    return HttpResponseRedirect(request.META.get("HTTP_REFERER", "/admin/"))

@admin.site.admin_view
def bundle_export(request):
    date_str = request.GET.get("date")
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else now().date()

    pins = get_filtered_pins(request, target_date)

    if not pins.exists():
        messages.warning(request, f"No pins scheduled for {target_date}")
        return HttpResponseRedirect(request.META.get("HTTP_REFERER", "/admin/"))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zip_file:
        # CSV
        csv_io = io.StringIO()
        csv_writer = csv.writer(csv_io)
        csv_writer.writerow([
            "Pinterest board",
            "Title",
            "Media URL",
            "Thumbnail",
            "Description",
            "Link",
            "Publish date",
            "Keywords"
        ])
        for pin in pins:
            title = pin.pin.title or pin.pin.headline.text[:100]
            csv_writer.writerow([
                pin.board.name,
                title,
                pin.pin.image_url,
                "",  # Only required for video
                pin.pin.description or "",
                pin.pin.link or "",
                target_date.isoformat(),
                ", ".join([kw.phrase for kw in pin.pin.keywords.all()])
            ])
        zip_file.writestr("scheduled_pins.csv", csv_io.getvalue())

        # Image URL list (optional)
        image_urls = "\n".join([pin.pin.image_url for pin in pins])
        zip_file.writestr("image_urls.txt", image_urls)

    buffer.seek(0)
    response = HttpResponse(buffer, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="scheduled_pins_bundle_{target_date}.zip"'
    return response