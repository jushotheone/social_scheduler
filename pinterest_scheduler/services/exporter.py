import csv
from django.utils.timezone import now
from django.conf import settings
from pinterest_scheduler.models import ScheduledPin
from pathlib import Path

EXPORT_HEADERS = [
    "Title",
    "Media URL",
    "Pinterest board",
    "Description",
    "Link",
    "Publish date",
    "Keywords"
]

def export_scheduled_pins_to_csv(target_date=None, output_path="scheduled_pins_export.csv"):
    if not target_date:
        target_date = now().date()

    pins = ScheduledPin.objects.filter(publish_date=target_date, posted=False).select_related('pin', 'board')
    
    output_file = Path(settings.BASE_DIR) / output_path

    with open(output_file, mode='w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=EXPORT_HEADERS)
        writer.writeheader()

        for scheduled_pin in pins:
            pin = scheduled_pin.pin
            writer.writerow({
                "Title": pin.headline.text[:100],
                "Media URL": pin.image_url(),  # Make sure image is hosted and public
                "Pinterest board": scheduled_pin.board.name,
                "Description": pin.description[:500],
                "Link": pin.link or "",
                "Publish date": scheduled_pin.publish_date.isoformat(),
                "Keywords": pin.keywords
            })

    return output_file