"""
pinterest_scheduler/api_urls.py

Token-authed JSON API for the BEIA workforce. Mounted under /api/ by the project
urlconf. See api_views for the auth model (X-BEIA-Token header vs BEIA_API_TOKEN).
"""

from django.urls import path

from . import api_views

urlpatterns = [
    path("health", api_views.health, name="beia_api_health"),
    path("campaigns", api_views.campaigns, name="beia_api_campaigns"),
    path("scheduled", api_views.scheduled, name="beia_api_scheduled"),
    path("export", api_views.export_rows, name="beia_api_export"),
    path("schedule", api_views.schedule, name="beia_api_schedule"),
    path("repurpose/picks", api_views.repurpose_picks, name="beia_api_repurpose_picks"),
    path("mark-posted", api_views.mark_posted, name="beia_api_mark_posted"),
    path("mark-repurposed", api_views.mark_repurposed, name="beia_api_mark_repurposed"),
]
