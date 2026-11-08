"""
Forms for the customer web app.

Signing up and placing an order validate through the same DRF serializers as
the Android app's API, so both apps follow exactly the same rules (password
strength, unique email, the service area, prices from the database). These
forms only collect the fields and show the errors.
"""

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from orders.constants import PaymentType


class SignInForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autocomplete": "username", "autocapitalize": "none"})
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})


class SignUpForm(forms.Form):
    first_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs={"autocomplete": "given-name"}))
    last_name = forms.CharField(max_length=150, required=False, widget=forms.TextInput(attrs={"autocomplete": "family-name"}))
    username = forms.CharField(
        max_length=150, widget=forms.TextInput(attrs={"autocomplete": "username", "autocapitalize": "none"})
    )
    phone = forms.CharField(
        max_length=20,
        help_text="The rider calls this number if they cannot find you.",
        widget=forms.TextInput(attrs={"autocomplete": "tel", "inputmode": "tel"}),
    )
    email = forms.EmailField(required=False, help_text="Optional.", widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password_confirm = forms.CharField(
        label="Password again", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )


class CheckoutForm(forms.Form):
    delivery_address = forms.CharField(
        label="Address or landmark",
        max_length=255,
        help_text="Something the rider can find: street, house colour, nearby store.",
        widget=forms.TextInput(attrs={"autocomplete": "street-address"}),
    )
    delivery_latitude = forms.DecimalField(max_digits=9, decimal_places=6, widget=forms.HiddenInput)
    delivery_longitude = forms.DecimalField(max_digits=9, decimal_places=6, widget=forms.HiddenInput)
    delivery_notes = forms.CharField(
        label="Notes for the rider", required=False, max_length=500, widget=forms.Textarea(attrs={"rows": 2})
    )
    payment_type = forms.ChoiceField(
        label="Pay with", choices=PaymentType.choices, widget=forms.RadioSelect, initial=PaymentType.COD
    )
    remember_address = forms.BooleanField(label="Save this as my usual address", required=False, initial=True)

    error_messages_location = "Drop the pin on the map where you want your order delivered."

    def clean(self):
        cleaned = super().clean()
        if self.errors.get("delivery_latitude") or self.errors.get("delivery_longitude"):
            self.errors.pop("delivery_latitude", None)
            self.errors.pop("delivery_longitude", None)
            raise forms.ValidationError(self.error_messages_location)
        return cleaned


class ProfileForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    phone = forms.CharField(max_length=20, widget=forms.TextInput(attrs={"inputmode": "tel"}))
    email = forms.EmailField(required=False)
