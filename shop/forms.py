"""
Forms for the customer web app.

Signing up and placing an order validate through the same DRF serializers as
the Android app's API, so both apps follow exactly the same rules (password
strength, unique username, the service area, prices from the database). These
forms only collect the fields and show the errors. The fields and their
wording follow the Android app's screens.
"""

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from orders.constants import PaymentType


def _input(placeholder="", **attrs):
    return {"class": "input", "placeholder": placeholder, **attrs}


class SignInForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            _input("Username", autocomplete="username", autocapitalize="none")
        )
        self.fields["password"].widget.attrs.update(_input("Password", autocomplete="current-password"))


class SignUpForm(forms.Form):
    """The app's Create Account form: one name box, split into first and last name."""

    name = forms.CharField(max_length=301, widget=forms.TextInput(attrs=_input("Name", autocomplete="name")))
    username = forms.CharField(
        max_length=150, widget=forms.TextInput(attrs=_input("Username", autocomplete="username", autocapitalize="none"))
    )
    phone = forms.CharField(
        max_length=20, widget=forms.TextInput(attrs=_input("Phone Number", autocomplete="tel", inputmode="tel"))
    )
    password = forms.CharField(widget=forms.PasswordInput(attrs=_input("Password", autocomplete="new-password")))
    password_confirm = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(attrs=_input("Confirm Password", autocomplete="new-password")),
    )

    def registration_data(self):
        """What RegisterSerializer takes: the name split on its first space, as the app does."""
        data = dict(self.cleaned_data)
        first, _, last = data.pop("name").strip().partition(" ")
        data["first_name"] = first
        data["last_name"] = last.strip()
        return data


class CheckoutForm(forms.Form):
    delivery_address = forms.CharField(
        label="Address",
        max_length=255,
        widget=forms.Textarea(attrs=_input("House number, street, landmark", rows=2, autocomplete="street-address")),
    )
    delivery_latitude = forms.DecimalField(max_digits=9, decimal_places=6, widget=forms.HiddenInput)
    delivery_longitude = forms.DecimalField(max_digits=9, decimal_places=6, widget=forms.HiddenInput)
    delivery_notes = forms.CharField(
        label="Delivery notes", required=False, max_length=500,
        widget=forms.TextInput(attrs=_input("Delivery notes (optional)")),
    )
    payment_type = forms.ChoiceField(
        label="Payment", choices=PaymentType.choices, widget=forms.RadioSelect, initial=PaymentType.COD
    )
    remember_address = forms.BooleanField(label="Save as my default delivery location", required=False, initial=True)

    error_messages_location = "Drop the pin on the map where you want your order delivered."

    def clean(self):
        cleaned = super().clean()
        if self.errors.get("delivery_latitude") or self.errors.get("delivery_longitude"):
            self.errors.pop("delivery_latitude", None)
            self.errors.pop("delivery_longitude", None)
            raise forms.ValidationError(self.error_messages_location)
        return cleaned


class ProfileForm(forms.Form):
    """The app's Edit Profile form."""

    first_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs=_input("First name", autocomplete="given-name")))
    last_name = forms.CharField(
        max_length=150, required=False, widget=forms.TextInput(attrs=_input("Last name", autocomplete="family-name"))
    )
    phone = forms.CharField(max_length=20, widget=forms.TextInput(attrs=_input("Phone", inputmode="tel", autocomplete="tel")))
    default_address = forms.CharField(
        label="Address", max_length=255, required=False,
        widget=forms.Textarea(attrs=_input("House number, street, landmark", rows=2, autocomplete="street-address")),
    )
