from django import forms
from django.utils.html import format_html
from .models import PinTemplateVariation, Headline

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