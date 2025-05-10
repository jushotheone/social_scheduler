# ruoth_pins/wsgi.py
import os
from django.core.wsgi import get_wsgi_application
from dotenv import load_dotenv  # <-- Add this line

load_dotenv()  # <-- Add this line (loads .env variables)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ruoth_pins.settings')
application = get_wsgi_application()