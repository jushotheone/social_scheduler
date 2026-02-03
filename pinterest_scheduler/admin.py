from django.contrib import admin, messages
from django.http import HttpResponse, HttpResponseRedirect, FileResponse
from django.utils import timezone
from django.shortcuts import render, redirect
from django.utils.html import format_html
from urllib.parse import unquote as urlunquote
from django.urls import path
from django.template.response import TemplateResponse
from django.db.models import Count, Q, Case, When
from .models import Pillar, Headline
from datetime import timedelta, datetime
from django.db import transaction
from collections import defaultdict
import csv
import io
import random
from decimal import Decimal
from django.db.models import Max
from .models import Pillar, Headline, PinTemplateVariation, Board, ScheduledPin, Campaign, Keyword, PinKeywordAssignment, RepurposedPostStatus
from .forms import PinTemplateVariationForm, ScheduledPinForm, KeywordCSVUploadForm, CampaignAdminForm
from pinterest_scheduler.services.exporter import export_scheduled_pins_to_csv
from pinterest_scheduler.services.hook_generator import build_context, generate_hook_openai
from django.utils.timezone import now, localtime, make_aware
import zipfile
import logging

from django.utils.safestring import mark_safe
from django.conf import settings

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None


logger = logging.getLogger(__name__)

admin.site.index_template = "admin/index.html"

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
        max_allowed = (obj.campaign.max_variations_per_headline or 4) * obj.headlines.count()
        return f"{total} / {max_allowed}"
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
        
class RepurposedStatusInline(admin.TabularInline):
    model = RepurposedPostStatus
    extra = 0
    fields = ['platform', 'created_at']
    readonly_fields = ['created_at']
    show_change_link = False

class CampaignFilter(admin.SimpleListFilter):
    title = 'Campaign'
    parameter_name = 'campaign'

    def lookups(self, request, model_admin):
        from .models import Campaign
        return [(c.id, c.name) for c in Campaign.objects.all()]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(headline__pillar__campaign__id=self.value())
        return queryset

@admin.register(PinTemplateVariation)
class PinTemplateVariationAdmin(admin.ModelAdmin):
    form = PinTemplateVariationForm
    change_list_template = "admin/change_list_with_upload_button.html"

    list_display = [
        'headline_display', 'title', 'repurpose_hook_preview', 'pillar_preview', 'variation_position', 'thumbnail_preview',
        'cta', 'mockup_name', 'background_style', 'keyword_list',
        'repurpose_tiktok', 'repurpose_instagram', 'repurpose_youtube'
    ]
    list_filter = ['headline__pillar', 'headline', HasKeywordsFilter, CampaignFilter]
    inlines = [PinKeywordInline, RepurposedStatusInline]
    # filter_horizontal = ('keywords',)
    search_fields = ['cta', 'mockup_name', 'badge_icon']
    readonly_fields = ['pillar_preview', 'thumbnail_preview', 'variation_progress']
    list_select_related = ['headline__pillar']
    actions = [
        'auto_assign_keywords',
        'smartloop_schedule',
        'mark_repurposed_tiktok',
        'mark_repurposed_instagram',
        'mark_repurposed_youtube',
        'mark_repurposed_all',
    ]

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
        max_allowed = (
            obj.headline.pillar.campaign.max_variations_per_headline
            if obj.headline.pillar.campaign.max_variations_per_headline is not None
            else 4
        )
        if count >= max_allowed:
            return format_html('<span style="color: red;">⚠️ {}/{} variations created (FULL)</span>', count, max_allowed)
        else:
            return format_html('<span style="color: green;">{}/{} variations created</span>', count, max_allowed)
    variation_progress.short_description = 'Variation Progress'

    @admin.display(description='Keywords')
    def keyword_list(self, obj):
        return ', '.join(k.phrase for k in obj.keywords.all())

    @admin.display(description='Hook')
    def repurpose_hook_preview(self, obj):
        hook = getattr(obj, 'repurpose_hook', '') or ''
        return hook[:60] if hook else '—'

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['upload_url'] = 'admin:upload_pin_variations_csv'
        extra_context['upload_label'] = 'Upload Pin Variations CSV'
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("upload-csv/", self.admin_site.admin_view(self.upload_pin_variations_csv), name="upload_pin_variations_csv"),
            path("repurpose/random/", self.admin_site.admin_view(self.random_repurpose_view), name="random_repurpose_view"),
        ]
        return custom_urls + urls

    def _get_openai_client(self):
        """Return an OpenAI client or None if not configured."""
        api_key = getattr(settings, 'OPENAI_API_KEY', None)
        if not api_key:
            logger.error("OPENAI_API_KEY missing in Django settings")
            return None
        if OpenAI is None:
            logger.error("OpenAI SDK import failed (OpenAI is None)")
            return None
        try:
            logger.info("OpenAI client initialised for hook generation")
            return OpenAI(api_key=api_key)
        except Exception as e:
            logger.exception("Failed to init OpenAI client: %s", e)
            return None

    def _generate_hook(self, pin, recent_hooks=None, max_chars=50):
        """Generate a punchy hook (<= max_chars) for a PinTemplateVariation.

        Single source of truth: delegates to pinterest_scheduler.services.hook_generator.

        IMPORTANT:
        - If OpenAI is not configured or generation fails, return an empty string.
        - We NEVER fall back to headline/title/tagline because that pollutes the DB/UI.
        """
        recent_hooks = recent_hooks or []

        pin_id = getattr(pin, 'id', None)
        logger.info("Hook gen start pin=%s recent_hooks=%s", pin_id, len(recent_hooks))

        # Build canonical context from the model
        ctx = build_context(pin)

        # If OpenAI isn't available, do not fabricate a "hook".
        client = self._get_openai_client()
        if client is None:
            logger.error("Hook gen skipped pin=%s (no OpenAI client)", pin_id)
            return ""

        try:
            hook = generate_hook_openai(
                context=ctx,
                client=client,
                recent_hooks=recent_hooks,
                max_chars=max_chars,
            )
        except Exception as e:
            logger.exception("Hook gen failed pin=%s: %s", pin_id, e)
            return ""

        hook = (hook or "").strip()[:max_chars]
        if hook:
            logger.info("Hook gen ok pin=%s len=%s", pin_id, len(hook))
        else:
            logger.error("Hook gen empty pin=%s", pin_id)
        return hook


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

                    # 🛡️ Defensive check: prevent over-creation
                    existing_count = headline.variations.count()
                    max_allowed = (
                        headline.pillar.campaign.max_variations_per_headline
                        if headline.pillar.campaign.max_variations_per_headline is not None
                        else 4
                    )
                    if existing_count >= max_allowed:
                        logger.warning(f"[Row {row_num}] Max variations reached ({existing_count}/{max_allowed}) for headline: {headline.text}")
                        skipped += 1
                        continue

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

    @admin.action(description="📅 SmartLoop: Auto-schedule pins across 30 days")
    def smartloop_schedule(self, request, queryset, dry_run=False, preview=False):
        boards = list(Board.objects.all())[:5]
        pins = list(queryset.select_related('headline__pillar'))
        logger.info(f"SmartLoop: {len(pins)} pins selected by admin.")

        # 1. Bucket pins into 6 groups of 20
        pin_count = len(pins)
        repeats_per_pin = 5
        total_slots = pin_count * repeats_per_pin
        days = 30
        pins_per_day = total_slots // days
        spacing = days // repeats_per_pin 

        # 2. Compute next Monday
        today = timezone.now().date()
        days_until_mon = (7 - today.weekday()) % 7 or 7
        start = today + timedelta(days=days_until_mon)

        # 3. Build schedule_by_day and collect per-pillar counts
        schedule_by_day   = defaultdict(list)
        pillar_diagnostics = defaultdict(lambda: defaultdict(int))  # pillar_diagnostics[date][pillar] = count

        # ✅ IMPROVED LOGIC: Each pin 5×, 6-day spaced, full rotation
        random.shuffle(pins)  # shuffle to introduce variety

        for i, pin in enumerate(pins):
            for rot in range(repeats_per_pin):
                day_index = (i + rot * spacing) % days
                pub_date = start + timedelta(days=day_index)
                board = boards[rot]
                schedule_by_day[pub_date].append((pin, board))
                pillar_diagnostics[pub_date][pin.headline.pillar.name] += 1

        # 4. Validate 20 pins/day
        # 🧪 Soft validation (optional): Warn if days have unusually high pin counts
        for pub_date, items in sorted(schedule_by_day.items()):
            if len(items) > pins_per_day + 5:
                self.message_user(
                    request,
                    f"⚠️ Scheduling warning: {pub_date} has {len(items)} pins (target ≈ {pins_per_day})",
                    level=messages.WARNING
                )

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
                    f"✅ {total_slots} pins scheduled: ≈{pins_per_day}/day, 6-day spacing, 5× per pin, CSV backup at /tmp/pins_schedule.csv",
                    level=messages.SUCCESS
                )
        
    auto_assign_keywords.short_description = "🎯 Smart Assign Keywords (Balanced + Unique)"

    def _platform_status(self, obj, platform):
        return "✅" if obj.repurposed_statuses.filter(platform=platform).exists() else "⛔"

    @admin.display(description="TikTok")
    def repurpose_tiktok(self, obj):
        return self._platform_status(obj, 'tiktok')

    @admin.display(description="Instagram")
    def repurpose_instagram(self, obj):
        return self._platform_status(obj, 'instagram')

    @admin.display(description="YouTube")
    def repurpose_youtube(self, obj):
        return self._platform_status(obj, 'youtube')

    @admin.action(description="✅ Mark as repurposed to TikTok")
    def mark_repurposed_tiktok(self, request, queryset):
        self._mark_repurposed(request, queryset, 'tiktok')

    @admin.action(description="✅ Mark as repurposed to Instagram")
    def mark_repurposed_instagram(self, request, queryset):
        self._mark_repurposed(request, queryset, 'instagram')

    @admin.action(description="✅ Mark as repurposed to YouTube")
    def mark_repurposed_youtube(self, request, queryset):
        self._mark_repurposed(request, queryset, 'youtube')

    @admin.action(description="✅ Mark as repurposed to All")
    def mark_repurposed_all(self, request, queryset):
        self._mark_repurposed(request, queryset, 'all')

    def _mark_repurposed(self, request, queryset, platform):
        from pinterest_scheduler.models import RepurposedPostStatus

        if not queryset.exists():
            selected_ids = request.POST.getlist('_selected_action')
            queryset = PinTemplateVariation.objects.filter(pk__in=selected_ids)

        added = 0
        platforms = ['tiktok', 'instagram', 'youtube'] if platform == 'all' else [platform]

        for variation in queryset:
            for p in platforms:
                if not RepurposedPostStatus.objects.filter(variation=variation, platform=p).exists():
                    RepurposedPostStatus.objects.create(
                        variation=variation,
                        platform=p,
                        campaign=variation.headline.pillar.campaign
                    )
                    added += 1

        self.message_user(
            request,
            f"✅ Marked as repurposed to {', '.join(platforms)} ({queryset.count()} pins × {len(platforms)} platforms)",
            level=messages.SUCCESS
        )

    def _looks_like_real_hook(self, text: str) -> bool:
        """Heuristic: distinguish a real AI hook from placeholders/labels.

        We treat short labels like "profit/loss question" or "origin of the dish" as NOT a hook.
        """
        t = (text or "").strip()
        if not t:
            return False

        low = t.lower()
        # Common placeholder labels / buckets that have shown up in DB
        banned = {
            "profit/loss question",
            "industry stat trivia",
            "origin of the dish",
            "ingredient origin or source quiz",
            "tool-for-task quiz",
            "hack-or-myth challenge",
            "flavour pair challenge",
        }
        if low in banned:
            return False

        # Too short to be a hook
        if len(t) < 18:
            return False

        # Hooks usually read like a sentence / question
        if not any(ch in t for ch in ["?", "!", "."]):
            return False

        return True

    def random_repurpose_view(self, request):
        from pinterest_scheduler.models import RepurposedPostStatus

        campaign_id = request.GET.get('campaign')
        platform = request.GET.get("platform", "all")

        if not campaign_id:
            self.message_user(request, "❌ Campaign ID is required in query params.", level=messages.ERROR)
            return redirect("..")

        # ---------------------------
        # CSV export for the *current* Daily 4 picks
        # - supports exporting by explicit ids: ?export=1&ids=1,2,3,4
        # - or exporting the persisted session Daily 4: ?export=1
        #
        # IMPORTANT:
        # - Must be FAST (no hook generation, no random selection)
        # - Must be deterministic (preserve the Daily 4 order)
        # ---------------------------
        if request.GET.get("export") == "1":
            ids_raw = (request.GET.get("ids") or "").strip()
            ids = []
            for part in ids_raw.split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))

            # If ids not provided, fall back to the persisted "today's 4" in session
            today_key = now().date().isoformat()
            session_key = f"daily4:{campaign_id}:{today_key}"
            if not ids:
                ids = request.session.get(session_key) or []

            logger.info("repurpose_random EXPORT start campaign=%s ids=%s", campaign_id, ids)

            if not ids:
                self.message_user(
                    request,
                    "⚠️ No Daily 4 saved for today yet. Refresh the page once, then export.",
                    level=messages.WARNING,
                )
                return redirect(request.get_full_path().split("?")[0] + f"?campaign={campaign_id}")

            # Fetch pins (small set) and preserve the requested order
            export_qs = (
                PinTemplateVariation.objects
                .filter(id__in=ids, headline__pillar__campaign_id=campaign_id)
                .select_related("headline__pillar", "headline__pillar__campaign")
                .prefetch_related("keywords")
            )
            by_id = {p.id: p for p in export_qs}
            ordered = [by_id[i] for i in ids if i in by_id]

            csv_buffer = io.StringIO()
            writer = csv.writer(csv_buffer)
            writer.writerow([
                "pin_id",
                "campaign",
                "pillar",
                "headline",
                "title",
                "hook",
                "hook_generated_at",
                "description",
                "image_url",
                "link",
                "keywords",
            ])

            for pin in ordered:
                keywords = ", ".join(k.phrase for k in pin.keywords.all())
                writer.writerow([
                    pin.id,
                    getattr(pin.headline.pillar.campaign, "name", "") if pin.headline_id else "",
                    getattr(pin.headline.pillar, "name", "") if pin.headline_id else "",
                    getattr(pin.headline, "text", "") if pin.headline_id else "",
                    pin.title or "",
                    (getattr(pin, "repurpose_hook", "") or "").strip(),
                    getattr(pin, "repurpose_hook_generated_at", "") or "",
                    pin.description or "",
                    pin.image_url or "",
                    pin.link or "",
                    keywords,
                ])

            filename = f"daily4_repurpose_picks_campaign_{campaign_id}_{today_key}.csv"
            resp = HttpResponse(csv_buffer.getvalue(), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'

            logger.info("repurpose_random EXPORT ok rows=%s", len(ordered))
            return resp

        # GET mode: Select 4 random pins not fully repurposed
        qs = PinTemplateVariation.objects.annotate(
            repurposed_count=Count('repurposed_statuses')
        ).filter(
            headline__pillar__campaign_id=campaign_id,
            repurposed_count__lt=3
        ).select_related('headline__pillar')

        # Persist "today's 4" so POST/redirect doesn't reshuffle.
        today_key_daily4 = now().date().isoformat()
        session_key_daily4 = f"daily4:{campaign_id}:{today_key_daily4}"
        saved_ids = request.session.get(session_key_daily4) or []

        selected = []
        if saved_ids:
            selected = list(qs.filter(id__in=saved_ids))
            # Preserve the original order
            selected_by_id = {p.id: p for p in selected}
            selected = [selected_by_id[i] for i in saved_ids if i in selected_by_id]

        # If no saved set, pick fresh 4 with unique pillar + headline
        if len(selected) < 4:
            pins = list(qs)
            random.shuffle(pins)

            used_pillars = set()
            used_headlines = set()
            picked = []

            for pin in pins:
                pillar_id = pin.headline.pillar_id
                headline_id = pin.headline_id

                if pillar_id in used_pillars or headline_id in used_headlines:
                    continue

                picked.append(pin)
                used_pillars.add(pillar_id)
                used_headlines.add(headline_id)

                if len(picked) == 4:
                    break

            selected = picked
            request.session[session_key_daily4] = [p.id for p in selected]

        if len(selected) < 4:
            self.message_user(request, f"⚠️ Only {len(selected)} eligible unique variations found.", level=messages.WARNING)

        # ✅ Auto-generate missing hooks for today's 4 (GET)
        # Keeps your workflow fast: open the page and hooks are ready.
        try:
            logger.info("repurpose_random GET auto-gen start selected=%s", len(selected))
            recent_hooks = list(
                PinTemplateVariation.objects.exclude(repurpose_hook__isnull=True)
                .exclude(repurpose_hook='')
                .order_by('-repurpose_hook_generated_at')
                .values_list('repurpose_hook', flat=True)[:20]
            )

            auto_updated = 0
            for p in selected:
                logger.info("repurpose_random GET auto-gen check pin=%s", getattr(p, "id", None))
                current = (getattr(p, 'repurpose_hook', '') or '').strip()
                if current and self._looks_like_real_hook(current):
                    logger.info("repurpose_random GET auto-gen skip pin=%s (already real hook)", getattr(p, "id", None))
                    recent_hooks.append(current)
                    continue

                hook = (self._generate_hook(p, recent_hooks=recent_hooks, max_chars=50) or "").strip()
                if not hook:
                    logger.error("repurpose_random GET auto-gen failed pin=%s (empty hook)", getattr(p, "id", None))
                    # Don't save junk / placeholders. Leave it empty so UI shows "No hook yet".
                    continue

                if hasattr(p, 'repurpose_hook'):
                    p.repurpose_hook = hook
                if hasattr(p, 'repurpose_hook_generated_at'):
                    p.repurpose_hook_generated_at = now()

                p.save(update_fields=['repurpose_hook', 'repurpose_hook_generated_at'])
                # Ensure the object used by the template has the new values
                p.repurpose_hook = hook
                p.repurpose_hook_generated_at = now()
                logger.info("repurpose_random GET auto-gen saved pin=%s hook_len=%s", getattr(p, "id", None), len(hook))
                recent_hooks.append(hook)
                auto_updated += 1

            if auto_updated:
                self.message_user(request, f"✅ Auto-generated {auto_updated} hooks for today.", level=messages.SUCCESS)
        except Exception as e:
            # Never break the page if hook generation fails.
            logger.exception("repurpose_random GET auto-gen exception: %s", e)

        # ✅ Handle POST actions:
        # - mark repurposed (existing)
        # - generate hooks (new)
        if request.method == "POST":
            logger.info("repurpose_random POST path=%s", request.path)
            logger.info("repurpose_random POST keys=%s", list(request.POST.keys()))
            logger.info("repurpose_random POST action=%s", request.POST.get("action"))
            logger.info("repurpose_random POST generate_hooks=%s", request.POST.get("generate_hooks"))
            logger.info("repurpose_random POST regenerate=%s", request.POST.get("regenerate"))
            logger.info("repurpose_random POST force=%s", request.POST.get("force"))
            logger.info("repurpose_random POST single_id=%s", request.POST.get("single_id"))
            selected_ids = request.POST.getlist("_selected_action")
            logger.info("repurpose_random selected_ids=%s", selected_ids)

            # Action detection (works even if template isn't updated)
            action = (request.POST.get("action") or "").strip()
            if not action:
                # If a submit button named generate_hooks was used
                if "generate_hooks" in request.POST:
                    action = "generate_hooks"
                else:
                    action = "mark_repurposed"
            logger.info("repurpose_random resolved action=%s", action)

            force = request.POST.get("force") == "1" or request.POST.get("regenerate") == "1"
            logger.info("repurpose_random force=%s", force)

            if action == "generate_hooks":
                # If user ticked checkboxes, use those; otherwise generate for today's 4.
                if selected_ids:
                    queryset = (
                        PinTemplateVariation.objects
                        .filter(id__in=selected_ids)
                        .select_related("headline__pillar", "headline__pillar__campaign")
                    )
                else:
                    queryset = (
                        PinTemplateVariation.objects
                        .filter(id__in=[p.id for p in selected])
                        .select_related("headline__pillar", "headline__pillar__campaign")
                    )

                # Optional single regenerate support (template JS can post single_id)
                single_id = (request.POST.get("single_id") or "").strip()
                if single_id:
                    queryset = (
                        PinTemplateVariation.objects
                        .filter(id=single_id)
                        .select_related("headline__pillar", "headline__pillar__campaign")
                    )

                logger.info("repurpose_random generate_hooks queryset_count=%s", queryset.count())

                # Pull recent hooks to avoid repeats
                recent_hooks = list(
                    PinTemplateVariation.objects
                    .exclude(repurpose_hook__isnull=True)
                    .exclude(repurpose_hook='')
                    .order_by('-repurpose_hook_generated_at')
                    .values_list('repurpose_hook', flat=True)[:30]
                )

                updated, skipped, failed = 0, 0, 0

                for pin in queryset:
                    logger.info("repurpose_random gen pin=%s", getattr(pin, "id", None))
                    current = (getattr(pin, "repurpose_hook", "") or "").strip()
                    if current and self._looks_like_real_hook(current) and not force:
                        logger.info("repurpose_random skip pin=%s (already has real hook)", getattr(pin, "id", None))
                        skipped += 1
                        continue

                    try:
                        hook = (self._generate_hook(pin, recent_hooks=recent_hooks, max_chars=50) or "").strip()
                    except Exception:
                        logger.exception("❌ Hook generation exception for pin=%s", getattr(pin, "id", None))
                        failed += 1
                        continue

                    if not hook:
                        logger.error("repurpose_random failed pin=%s (empty hook)", getattr(pin, "id", None))
                        failed += 1
                        continue

                    pin.repurpose_hook = hook
                    pin.repurpose_hook_generated_at = now()
                    pin.save(update_fields=["repurpose_hook", "repurpose_hook_generated_at"])
                    logger.info("repurpose_random saved pin=%s hook_len=%s", getattr(pin, "id", None), len(hook))

                    recent_hooks.append(hook)
                    updated += 1

                messages.success(
                    request,
                    f"✅ Hooks generated: {updated} | Skipped: {skipped} | Failed: {failed}"
                )

                # We rely on the session-persisted Daily 4 selection above,
                # so a redirect will re-render the same cards with fresh hooks.
                return redirect(request.get_full_path())

            # Default: mark as repurposed
            if not selected_ids:
                self.message_user(request, "⚠️ No pins selected.", level=messages.WARNING)
            else:
                queryset = PinTemplateVariation.objects.filter(id__in=selected_ids)
                self._mark_repurposed(request, queryset, platform)
                self.message_user(request, f"✅ {len(selected_ids)} pins marked as repurposed to {platform.title()}")
                return redirect(request.get_full_path())  # refresh the page

        today_key_daily4 = now().date().isoformat()
        session_key_daily4 = f"daily4:{campaign_id}:{today_key_daily4}"
        context = {
            'title': "🎯 Daily 4 Repurpose Picks (Unique Pillars & Headlines)",
            'pins': selected,
            'platform': platform,
            'opts': self.model._meta,
            # Export the current Daily 4 (uses session ids server-side)
            'export_url': f"?campaign={campaign_id}&platform={platform}&export=1",
        }
        return TemplateResponse(request, "admin/repurpose_random_list.html", context)



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
        writer.writerow(["Board", "Title", "Hook", "Description", "Link", "Image URL", "Alt Text"])

        image_urls = []

        for pin in queryset:
            title = pin.pin.title or pin.pin.headline.text
            if not title:
                self.message_user(request, f"❌ Missing title for pin ID {pin.pin.id}.", level=messages.ERROR)
                return HttpResponse(status=400)

            hook = (getattr(pin.pin, 'repurpose_hook', '') or '').strip()

            writer.writerow([
                pin.board.name,
                title,
                hook,
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
    form = CampaignAdminForm 
    list_display = ['name', 'start_date', 'end_date', 'repurpose_summary', 'daily_repurpose_link', 'view_dashboard_link']
    inlines = [PillarInline]
    search_fields = ['name']
    ordering = ['start_date']

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:campaign_id>/repurpose-picks/',
                self.admin_site.admin_view(self.daily_repurpose_redirect),
                name='campaign-daily-repurpose'
            ),
            path(
                'repurpose-summary/',
                lambda request: redirect('/admin-tools/repurpose-dashboard/'),
                name='repurpose_summary_redirect'
            ),
        ]
        return custom_urls + urls


    def daily_repurpose_link(self, obj):
        return format_html(
            '<a class="button" href="{}">🎯 Daily Repurpose Picks</a>',
            f"/admin/pinterest_scheduler/pintemplatevariation/repurpose/random/?campaign={obj.id}"
        )
    daily_repurpose_link.short_description = "Repurpose Tools"
    daily_repurpose_link.allow_tags = True

    def daily_repurpose_redirect(self, request, campaign_id):
        return redirect(f"/admin/pinterest_scheduler/pintemplatevariation/repurpose/random/?campaign={campaign_id}")

    def repurpose_summary(self, obj):
        total = obj.pillars.prefetch_related('headlines__variations').aggregate(
            count=Count('headlines__variations')
        )['count'] or 0

        repurposed = RepurposedPostStatus.objects.filter(campaign=obj).values('variation').distinct().count()
        percent = int((repurposed / (total * 3)) * 100) if total > 0 else 0

        color = "green" if percent == 100 else "orange" if percent >= 50 else "red"
        return mark_safe(f'<b style="color:{color}">{percent}% repurposed</b>')

    repurpose_summary.short_description = "🎯 Repurpose Progress"

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['repurpose_dashboard_url'] = '/admin/repurpose-dashboard/'
        return super().changelist_view(request, extra_context=extra_context)
    
    def view_dashboard_link(self, obj):
        return format_html(
            '<a class="button" href="{}?campaign={}">📊 View Summary Dashboard</a>',
            '/admin-tools/repurpose-dashboard/',
            obj.id
        )
    view_dashboard_link.short_description = "📈 Dashboard"


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
        "Hook",
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

        hook = (getattr(pin.pin, 'repurpose_hook', '') or '').strip()

        title = (pin.pin.title or '')[:100]
        writer.writerow([
            title,
            hook,
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

@admin.site.admin_view
def repurpose_summary_dashboard(request):
    from pinterest_scheduler.models import Campaign, RepurposedPostStatus

    platforms = ['tiktok', 'instagram', 'youtube']
    rows = []

    for campaign in Campaign.objects.all().order_by('start_date'):
        total = PinTemplateVariation.objects.filter(
            headline__pillar__campaign=campaign
        ).count()

        platform_counts = {}
        for platform in platforms:
            repurposed = RepurposedPostStatus.objects.filter(
                campaign=campaign,
                platform=platform
            ).count()
            platform_counts[platform] = repurposed

        rows.append({
            'campaign': campaign,
            'total': total,
            'platform_counts': platform_counts,
        })

    return TemplateResponse(request, 'admin/repurpose_summary_dashboard.html', {
        'rows': rows,
        'platforms': platforms,
    })