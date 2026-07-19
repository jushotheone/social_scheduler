"""
pinterest_scheduler/api_views.py

Thin, token-authed JSON API over the Pinterest Scheduler.

Purpose: let the BEIA workforce drive this scheduler as a TOOL — read campaign
state, run SmartLoop scheduling, build the ready-to-upload Pinterest rows, pick
the daily repurpose set (with AI hooks), and write status back — without a human
in the admin.

Design rules:
- Additive only. The Django admin actions are the proven source of truth; this
  module MIRRORS their logic (SmartLoop schedule, export rows, repurpose picks,
  hook generation via services.hook_generator) so the admin is left untouched.
- Auth is a shared secret in the `X-BEIA-Token` header, compared to the
  BEIA_API_TOKEN env var. No token configured => every call is refused (fail closed).
- Machine API => CSRF-exempt on writes. No new dependencies (plain Django JSON).
- Read endpoints never mutate. Write endpoints are explicit POSTs.
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import timedelta

from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import (
    Board,
    Campaign,
    PinTemplateVariation,
    RepurposedPostStatus,
    ScheduledPin,
)
from .services.hook_generator import build_context, generate_hook_openai

try:  # pragma: no cover - openai is optional at import time
    from openai import OpenAI
except Exception:  # noqa: BLE001
    OpenAI = None

logger = logging.getLogger(__name__)

REPURPOSE_PLATFORMS = ["tiktok", "instagram", "youtube"]


# ─────────────────────────── auth ───────────────────────────

def _configured_token() -> str:
    return (os.getenv("BEIA_API_TOKEN") or "").strip()


def _authorised(request) -> bool:
    token = _configured_token()
    if not token:
        # Fail closed: no server token means the API is disabled.
        return False
    supplied = (request.headers.get("X-BEIA-Token") or "").strip()
    return bool(supplied) and supplied == token


def _unauthorised() -> JsonResponse:
    return JsonResponse({"ok": False, "error": "unauthorised"}, status=401)


def _bad_request(msg: str) -> JsonResponse:
    return JsonResponse({"ok": False, "error": msg}, status=400)


def _load_body(request) -> dict:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


# ─────────────────────── serialisers ────────────────────────

def _campaign_dict(c: Campaign) -> dict:
    today = now().date()
    pillars = list(c.pillars.all())
    variation_total = PinTemplateVariation.objects.filter(
        headline__pillar__campaign=c
    ).count()
    repurposed = (
        RepurposedPostStatus.objects.filter(campaign=c)
        .values("variation")
        .distinct()
        .count()
    )
    denom = variation_total * len(REPURPOSE_PLATFORMS)
    percent = int((repurposed / denom) * 100) if denom else 0
    return {
        "id": c.id,
        "name": c.name,
        "start_date": c.start_date.isoformat() if c.start_date else None,
        "end_date": c.end_date.isoformat() if c.end_date else None,
        "active": bool(c.start_date and c.end_date and c.start_date <= today <= c.end_date),
        "pillars": [{"id": p.id, "name": p.name, "tagline": p.tagline} for p in pillars],
        "variation_count": variation_total,
        "repurpose_percent": percent,
    }


def _pin_dict(pin: PinTemplateVariation) -> dict:
    return {
        "id": pin.id,
        "title": pin.title or (pin.headline.text if pin.headline_id else ""),
        "hook": (getattr(pin, "repurpose_hook", "") or "").strip(),
        "image_url": pin.image_url or "",
        "description": pin.description or "",
        "link": pin.link or "",
        "cta": pin.cta or "",
        "pillar": pin.headline.pillar.name if pin.headline_id else "",
        "campaign": (
            pin.headline.pillar.campaign.name
            if pin.headline_id and pin.headline.pillar.campaign_id
            else ""
        ),
        "keywords": [k.phrase for k in pin.keywords.all()],
    }


# ─────────────────────── read endpoints ─────────────────────

@require_http_methods(["GET"])
def health(request):
    if not _authorised(request):
        return _unauthorised()
    return JsonResponse({"ok": True, "campaigns": Campaign.objects.count()})


@require_http_methods(["GET"])
def campaigns(request):
    if not _authorised(request):
        return _unauthorised()
    data = [_campaign_dict(c) for c in Campaign.objects.all().order_by("start_date")]
    return JsonResponse({"ok": True, "campaigns": data})


def _scheduled_queryset(request):
    date_str = request.GET.get("date")
    target_date = now().date()
    if date_str:
        try:
            target_date = timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None, None
    qs = ScheduledPin.objects.filter(publish_date=target_date).select_related(
        "pin__headline__pillar", "pin__headline__pillar__campaign", "board", "campaign"
    ).prefetch_related("pin__keywords")
    campaign_id = request.GET.get("campaign")
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)
    board_slug = request.GET.get("board")
    if board_slug:
        qs = qs.filter(board__slug=board_slug)
    return target_date, qs


@require_http_methods(["GET"])
def scheduled(request):
    """All scheduled pins for a date (default today), with full pin fields."""
    if not _authorised(request):
        return _unauthorised()
    target_date, qs = _scheduled_queryset(request)
    if qs is None:
        return _bad_request("invalid date format, use YYYY-MM-DD")
    rows = []
    for sp in qs:
        row = _pin_dict(sp.pin)
        row.update(
            {
                "scheduled_pin_id": sp.id,
                "board": sp.board.name,
                "board_slug": sp.board.slug,
                "publish_date": sp.publish_date.isoformat(),
                "campaign_day": sp.campaign_day,
                "slot_number": sp.slot_number,
                "status": sp.status,
            }
        )
        rows.append(row)
    return JsonResponse(
        {"ok": True, "date": target_date.isoformat(), "count": len(rows), "pins": rows}
    )


@require_http_methods(["GET"])
def export_rows(request):
    """Ready-to-upload Pinterest rows for a date — mirrors admin export_today_csv."""
    if not _authorised(request):
        return _unauthorised()
    target_date, qs = _scheduled_queryset(request)
    if qs is None:
        return _bad_request("invalid date format, use YYYY-MM-DD")
    rows = []
    for sp in qs:
        pin = sp.pin
        title = pin.title or (pin.headline.text if pin.headline_id else "")
        alt_text = pin.cta or (
            pin.headline.pillar.tagline if pin.headline_id else ""
        )
        rows.append(
            {
                "board": sp.board.name,
                "title": title,
                "hook": (getattr(pin, "repurpose_hook", "") or "").strip(),
                "description": pin.description or "",
                "link": pin.link or "",
                "image_url": pin.image_url or "",
                "alt_text": alt_text,
                "publish_date": sp.publish_date.isoformat(),
                "keywords": ", ".join(k.phrase for k in pin.keywords.all()),
            }
        )
    return JsonResponse(
        {"ok": True, "date": target_date.isoformat(), "count": len(rows), "rows": rows}
    )


# ─────────────────────── action endpoints ───────────────────

@csrf_exempt
@require_http_methods(["POST"])
def schedule(request):
    """Run SmartLoop for a campaign's pins. Mirrors admin.smartloop_schedule.

    Body: {"campaign_id": <int>, "pin_ids": [<int>...] (optional subset)}
    Each pin is scheduled 5x, 6-day spaced, ~20/day across 30 days from next Monday.
    Existing ScheduledPins in that 30-day window are cleared first (idempotent re-run).
    """
    if not _authorised(request):
        return _unauthorised()
    body = _load_body(request)
    campaign_id = body.get("campaign_id")
    if not campaign_id:
        return _bad_request("campaign_id is required")
    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except Campaign.DoesNotExist:
        return _bad_request(f"campaign {campaign_id} not found")

    pins_qs = PinTemplateVariation.objects.filter(
        headline__pillar__campaign=campaign
    ).select_related("headline__pillar")
    pin_ids = body.get("pin_ids")
    if pin_ids:
        pins_qs = pins_qs.filter(id__in=pin_ids)
    pins = list(pins_qs)
    if not pins:
        return _bad_request("no pins found for this campaign")

    boards = list(Board.objects.all()[:5])
    if len(boards) < 5:
        return _bad_request(f"need at least 5 boards, found {len(boards)}")

    repeats_per_pin = 5
    days = 30
    spacing = days // repeats_per_pin  # 6
    total_slots = len(pins) * repeats_per_pin

    today = timezone.now().date()
    days_until_mon = (7 - today.weekday()) % 7 or 7
    start = today + timedelta(days=days_until_mon)

    from collections import defaultdict

    schedule_by_day = defaultdict(list)
    random.shuffle(pins)
    for i, pin in enumerate(pins):
        for rot in range(repeats_per_pin):
            day_index = (i + rot * spacing) % days
            pub_date = start + timedelta(days=day_index)
            schedule_by_day[pub_date].append((pin, boards[rot]))

    created = 0
    with transaction.atomic():
        ScheduledPin.objects.filter(
            campaign=campaign,
            publish_date__range=(start, start + timedelta(days=days - 1)),
        ).delete()
        for pub_date, items in schedule_by_day.items():
            campaign_day = (pub_date - start).days + 1
            for slot_num, (pin, board) in enumerate(items, start=1):
                ScheduledPin.objects.create(
                    campaign=campaign,
                    pin=pin,
                    board=board,
                    publish_date=pub_date,
                    campaign_day=campaign_day,
                    slot_number=slot_num,
                    status="scheduled",
                )
                created += 1

    logger.info(
        "[BEIA-API] SmartLoop campaign=%s pins=%s created=%s start=%s",
        campaign_id,
        len(pins),
        created,
        start,
    )
    return JsonResponse(
        {
            "ok": True,
            "campaign_id": campaign.id,
            "pins": len(pins),
            "scheduled": created,
            "expected": total_slots,
            "start_date": start.isoformat(),
            "days": days,
        }
    )


def _openai_client():
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        logger.error("[BEIA-API] OpenAI init failed: %s", exc)
        return None


@require_http_methods(["GET"])
def repurpose_picks(request):
    """Daily repurpose picks for a campaign — mirrors admin.random_repurpose_view.

    Query: ?campaign=<int>&count=4
    Picks pins not yet fully repurposed (unique pillar + headline), deterministic
    per (campaign, day), and generates the <=50 char Ruoth hook where missing.
    """
    if not _authorised(request):
        return _unauthorised()
    campaign_id = request.GET.get("campaign")
    if not campaign_id:
        return _bad_request("campaign query param is required")
    try:
        count = int(request.GET.get("count", 4))
    except ValueError:
        count = 4
    # exclude_platform: skip pins already repurposed to that platform. Lets a single-track
    # cycle (run N×/day) pick a FRESH item each run instead of the same deterministic one.
    exclude_platform = (request.GET.get("exclude_platform") or "").strip().lower()

    qs = (
        PinTemplateVariation.objects.annotate(
            repurposed_count=Count("repurposed_statuses")
        )
        .filter(
            headline__pillar__campaign_id=campaign_id,
            repurposed_count__lt=len(REPURPOSE_PLATFORMS),
        )
        .select_related("headline__pillar")
        .prefetch_related("keywords")
    )
    if exclude_platform in REPURPOSE_PLATFORMS:
        qs = qs.exclude(repurposed_statuses__platform=exclude_platform)
    pins = list(qs)
    # Deterministic per campaign per day so repeated calls return the same set.
    rng = random.Random(f"{campaign_id}:{now().date().isoformat()}")
    rng.shuffle(pins)

    used_pillars, used_headlines, picked = set(), set(), []
    for pin in pins:
        if pin.headline.pillar_id in used_pillars or pin.headline_id in used_headlines:
            continue
        picked.append(pin)
        used_pillars.add(pin.headline.pillar_id)
        used_headlines.add(pin.headline_id)
        if len(picked) == count:
            break

    # Generate missing hooks (best-effort; never fabricate on failure).
    client = _openai_client()
    if client is not None:
        recent = list(
            PinTemplateVariation.objects.exclude(repurpose_hook__isnull=True)
            .exclude(repurpose_hook="")
            .order_by("-repurpose_hook_generated_at")
            .values_list("repurpose_hook", flat=True)[:20]
        )
        for pin in picked:
            current = (getattr(pin, "repurpose_hook", "") or "").strip()
            if current:
                recent.append(current)
                continue
            try:
                hook = (
                    generate_hook_openai(
                        context=build_context(pin),
                        client=client,
                        recent_hooks=recent,
                        max_chars=50,
                    )
                    or ""
                ).strip()
            except Exception as exc:  # noqa: BLE001
                logger.error("[BEIA-API] hook gen failed pin=%s: %s", pin.id, exc)
                hook = ""
            if hook:
                pin.repurpose_hook = hook
                pin.repurpose_hook_generated_at = now()
                pin.save(
                    update_fields=["repurpose_hook", "repurpose_hook_generated_at"]
                )
                recent.append(hook)

    return JsonResponse(
        {
            "ok": True,
            "campaign_id": int(campaign_id),
            "count": len(picked),
            "picks": [_pin_dict(p) for p in picked],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def mark_posted(request):
    """Mark scheduled pins posted. Body: {"scheduled_pin_ids": [...]} or {"date","campaign_id"}."""
    if not _authorised(request):
        return _unauthorised()
    body = _load_body(request)
    ids = body.get("scheduled_pin_ids")
    if ids:
        qs = ScheduledPin.objects.filter(id__in=ids)
    else:
        date_str = body.get("date")
        campaign_id = body.get("campaign_id")
        if not date_str:
            return _bad_request("provide scheduled_pin_ids or date (+ optional campaign_id)")
        try:
            target_date = timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return _bad_request("invalid date format, use YYYY-MM-DD")
        qs = ScheduledPin.objects.filter(publish_date=target_date)
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
    updated = qs.update(status="posted")
    logger.info("[BEIA-API] mark_posted updated=%s", updated)
    return JsonResponse({"ok": True, "updated": updated})


@csrf_exempt
@require_http_methods(["POST"])
def mark_repurposed(request):
    """Record repurpose status. Body: {"variation_ids": [...], "platform": "tiktok|instagram|youtube|all"}."""
    if not _authorised(request):
        return _unauthorised()
    body = _load_body(request)
    variation_ids = body.get("variation_ids")
    platform = (body.get("platform") or "all").strip().lower()
    if not variation_ids:
        return _bad_request("variation_ids is required")
    if platform == "all":
        platforms = list(REPURPOSE_PLATFORMS)
    elif platform in REPURPOSE_PLATFORMS:
        platforms = [platform]
    else:
        return _bad_request(f"platform must be one of {REPURPOSE_PLATFORMS + ['all']}")

    variations = PinTemplateVariation.objects.filter(
        id__in=variation_ids
    ).select_related("headline__pillar__campaign")
    added = 0
    for variation in variations:
        campaign = (
            variation.headline.pillar.campaign
            if variation.headline_id and variation.headline.pillar.campaign_id
            else None
        )
        for p in platforms:
            _, created = RepurposedPostStatus.objects.get_or_create(
                variation=variation, platform=p, defaults={"campaign": campaign}
            )
            if created:
                added += 1
    logger.info("[BEIA-API] mark_repurposed added=%s platforms=%s", added, platforms)
    return JsonResponse({"ok": True, "recorded": added, "platforms": platforms})
