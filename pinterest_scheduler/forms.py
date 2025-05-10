from django import forms
from django.utils.html import format_html
from django.utils import timezone
from datetime import timedelta
from .models import PinTemplateVariation, Headline, ScheduledPin

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

# ----------------------
#  Schedule form
# ----------------------


class ScheduledPinForm(forms.ModelForm):
    class Meta:
        model = ScheduledPin
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        pin = self.initial.get('pin') or self.data.get('pin')

        if pin:
            try:
                pin_obj = PinTemplateVariation.objects.get(pk=pin)
                existing = ScheduledPin.objects.filter(pin=pin_obj)
                boards = Board.objects.all()
                used_boards = set(existing.values_list('board_id', flat=True))
                unused_boards = [b for b in boards if b.id not in used_boards]

                if unused_boards:
                    self.fields['board'].initial = unused_boards[0]
                else:
                    self.fields['board'].help_text = "⚠️ All boards used for this pin."

                next_day = (existing.count() // 5) + 1
                today = timezone.now().date()
                days_until_monday = (7 - today.weekday()) % 7 or 7
                first_day = today + timedelta(days=days_until_monday)
                suggested_date = first_day + timedelta(days=next_day - 1)

                self.fields['campaign_day'].initial = next_day
                self.fields['publish_date'].initial = suggested_date

            except PinTemplateVariation.DoesNotExist:
                pass