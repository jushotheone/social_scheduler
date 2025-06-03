from django.urls import path
from .admin import export_today_csv, dry_run_preview, bundle_export, repurpose_summary_dashboard

urlpatterns = [
    path("export_today_csv/", export_today_csv, name="export_today_csv"),
    path("dry_run_preview/", dry_run_preview, name="dry_run_preview"),
    path("bundle_export/", bundle_export, name="bundle_export"),
    path("repurpose-dashboard/", repurpose_summary_dashboard, name="repurpose_dashboard"),  # ✅ Add name


]