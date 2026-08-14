"""Authentication and account-settings forms."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from core.choices import Role
from core.forms import StyledFormMixin

User = get_user_model()


class UserRegisterForm(StyledFormMixin, UserCreationForm):
    """Sign-up form. Role choice drives which profile gets created afterwards."""

    first_name = forms.CharField(max_length=150, required=True, label="First name")
    last_name = forms.CharField(max_length=150, required=True, label="Last name")
    email = forms.EmailField(required=True, label="Email address")
    phone = forms.CharField(
        max_length=20,
        required=False,
        label="Phone number",
        help_text="Only shared with a donor or recipient after a match is agreed.",
    )
    role = forms.ChoiceField(
        choices=Role.choices,
        initial=Role.DONOR,
        label="I am registering as",
        widget=forms.RadioSelect,
    )
    accept_terms = forms.BooleanField(
        required=True,
        label="I confirm my details are accurate and consent to being contacted about donations.",
    )

    class Meta:
        model = User
        fields = (
            "first_name",
            "last_name",
            "username",
            "email",
            "phone",
            "role",
            "password1",
            "password2",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # RadioSelect must not receive the text-input class from the mixin.
        self.fields["role"].widget.attrs.pop("class", None)
        self.fields["username"].help_text = "Letters, digits and @ . + - _ only."

    def clean_email(self):
        """Enforce case-insensitive email uniqueness.

        ``EmailField(unique=True)`` alone would still allow Bob@x.com alongside
        bob@x.com, which would split one person's account in two.
        """
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.phone = self.cleaned_data.get("phone", "")
        user.role = self.cleaned_data["role"]
        if commit:
            user.save()
        return user


class UserLoginForm(StyledFormMixin, AuthenticationForm):
    """Login accepting either a username or an email address."""

    username = forms.CharField(label="Username or email")

    def clean(self):
        # Translate an email into the matching username before authenticating.
        identifier = self.cleaned_data.get("username", "")
        if identifier and "@" in identifier:
            match = User.objects.filter(email__iexact=identifier.strip()).first()
            if match:
                self.cleaned_data["username"] = match.username
                self.data = self.data.copy()
                self.data["username"] = match.username
        return super().clean()


class AccountSettingsForm(StyledFormMixin, forms.ModelForm):
    """Edit the identity fields that live on the user rather than the profile."""

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone", "avatar")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another account already uses this email.")
        return email

    def clean_avatar(self):
        """Reject oversized uploads before they reach disk."""
        avatar = self.cleaned_data.get("avatar")
        if avatar and getattr(avatar, "size", 0):
            from django.conf import settings

            limit = getattr(settings, "MAX_UPLOAD_SIZE_BYTES", 5 * 1024 * 1024)
            if avatar.size > limit:
                raise forms.ValidationError(
                    f"Image is too large. Maximum size is {limit // (1024 * 1024)} MB."
                )
        return avatar
