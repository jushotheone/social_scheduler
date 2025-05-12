from django import forms
from django.utils.html import format_html, format_html_join
from django.utils import timezone
from datetime import timedelta
from django.utils.timezone import now
from .models import PinTemplateVariation, ScheduledPin, Board, Headline

class PinTemplateVariationForm(forms.ModelForm):
    class Meta:
        model = PinTemplateVariation
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        headline_id = self.initial.get('headline') or self.data.get('headline')

        if headline_id:
            try:
                headline = Headline.objects.get(pk=headline_id)
                pillar_name = headline.pillar.name
                variation_count = headline.variations.count()
                
                colour = "#33cc33" if variation_count < 4 else "#cc3333"
                emoji = "🟢" if variation_count < 4 else "❌"

                self.fields['headline'].help_text = format_html(
                    '<div style="margin-top:5px;">'
                    '{} <strong style="color:{};">Pillar:</strong> {} &nbsp;|&nbsp; '
                    '<strong style="color:{};">Variations:</strong> {}/4 created'
                    '</div>',
                    emoji, colour, pillar_name, colour, variation_count
                )

            except Headline.DoesNotExist:
                pass


class ScheduledPinForm(forms.ModelForm):
    class Meta:
        model = ScheduledPin
        fields = ['pin', 'board', 'posted']  # Only user-facing fields

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        pin_id = self.initial.get('pin') or self.data.get('pin')

        if pin_id:
            try:
                pin = PinTemplateVariation.objects.get(pk=pin_id)
                scheduled = ScheduledPin.objects.filter(pin=pin).select_related('board').order_by('campaign_day')

                # Show summary of where the pin is already scheduled
                rows = format_html_join(
                    '\n',
                    '<li><b>Board:</b> {} | <b>Day:</b> {} | <b>Date:</b> {}</li>',
                    [(s.board.name, s.campaign_day, s.publish_date) for s in scheduled]
                )
                self.fields['pin'].help_text = format_html(
                    "<div style='margin-top:10px;padding:10px;background:#f9f9f9;border:1px solid #ddd;'>"
                    "<b>📋 Already scheduled to:</b><ul>{}</ul></div>",
                    rows or "<li>No schedules yet.</li>"
                )

                # Suggest next board only (we no longer need to touch day/date/slot)
                used_board_ids = {s.board.id for s in scheduled}
                available_boards = Board.objects.exclude(id__in=used_board_ids)
                if available_boards.exists():
                    self.fields['board'].initial = available_boards.first()
                else:
                    self.fields['board'].help_text = "⚠️ All boards already used for this pin."

            except PinTemplateVariation.DoesNotExist:
                pass

class KeywordCSVUploadForm(forms.Form):
    csv_file = forms.FileField(label="Upload Google Keyword CSV")