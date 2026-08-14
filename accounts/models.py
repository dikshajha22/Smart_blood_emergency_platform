from django.contrib.auth.models import AbstractUser
from django.core.validators import FileExtensionValidator, RegexValidator
from django.db import models

from core.choices import Role

phone_validator = RegexValidator(
    regex=r"^\+?[0-9\s\-()]{7,20}$",
    message="Enter a valid phone number (7-20 digits, optional +, spaces or dashes).",
)


class CustomUser(AbstractUser):
    """Project user. ``role`` decides which profile and dashboard applies."""

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.DONOR,
        db_index=True,
        help_text="Determines which profile type and dashboard this account uses.",
    )
    email = models.EmailField(unique=True)
    phone = models.CharField(
        max_length=20,
        blank=True,
        validators=[phone_validator],
        help_text="Used for urgent contact once a donation is agreed.",
    )
    # FileField rather than ImageField: ImageField requires Pillow, and this
    # project deliberately runs on the standard library plus Django alone.
    # The extension whitelist plus the size check in AccountSettingsForm gives
    # equivalent safety for our purposes.
    avatar = models.FileField(
        upload_to="avatars/",
        null=True,
        blank=True,
        validators=[
            FileExtensionValidator(
                allowed_extensions=["jpg", "jpeg", "png", "webp", "gif"]
            )
        ],
    )
    is_phone_verified = models.BooleanField(default=False)

    # AbstractUser already supplies `date_joined`, so no extra creation stamp
    # is needed here.

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"
        indexes = [models.Index(fields=["role", "is_active"])]

    def __str__(self) -> str:
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def display_name(self) -> str:
        """Best available human name, never blank."""
        return self.get_full_name().strip() or self.username

    @property
    def profile(self):
        """The role-appropriate profile object, or ``None`` if not created yet."""
        mapping = {
            Role.DONOR: "donor_profile",
            Role.RECIPIENT: "recipient_profile",
            Role.HOSPITAL: "hospital_profile",
        }
        attribute = mapping.get(self.role)
        return getattr(self, attribute, None) if attribute else None

    @property
    def dashboard_url_name(self) -> str:
        return {
            Role.DONOR: "donor_dashboard",
            Role.RECIPIENT: "recipient_dashboard",
            Role.HOSPITAL: "hospital_dashboard",
        }.get(self.role, "home")
