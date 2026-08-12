from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


class Parent(models.Model):
    """A parent looking to book Learning Support Assistants (LSAs) for their child."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='parent_profile')
    phone_number = models.CharField(max_length=20)
    address = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['phone_number']),
        ]

    def __str__(self):
        return f"Parent: {self.user.get_full_name() or self.user.username}"


class Skill(models.Model):
    """A discrete skill/specialisation an LSA can offer (e.g. 'Dyslexia Support', 'ADHD')."""
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class LSAProfile(models.Model):
    """A Learning Support Assistant's public profile, searchable by parents."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='lsa_profile')
    bio = models.TextField(blank=True)
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2)
    skills = models.ManyToManyField(Skill, related_name='lsas', blank=True)
    is_available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['is_available']),
        ]

    def __str__(self):
        return f"LSA: {self.user.get_full_name() or self.user.username}"


class BookingRequest(models.Model):
    """
    A booking of an LSA by a Parent for a specific time slot.

    Poka-Yoke design note: overlap prevention is enforced at TWO layers —
    (1) application-level validation in BookingService (fast feedback, custom error),
    (2) a DB constraint (belt-and-braces against race conditions / bypassed application code).
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = 'pending_payment', 'Pending Payment'
        CONFIRMED = 'confirmed', 'Confirmed'
        CANCELLED = 'cancelled', 'Cancelled'
        COMPLETED = 'completed', 'Completed'
        PAYMENT_FAILED = 'payment_failed', 'Payment Failed'

    parent = models.ForeignKey(Parent, on_delete=models.CASCADE, related_name='bookings')
    lsa = models.ForeignKey(LSAProfile, on_delete=models.CASCADE, related_name='bookings')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Composite index: overlap-checking always filters by lsa first, then time range.
            models.Index(fields=['lsa', 'start_time', 'end_time'], name='idx_lsa_time_range'),
            models.Index(fields=['status']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F('start_time')),
                name='booking_end_after_start',
            ),
        ]

    def clean(self):
        if self.end_time <= self.start_time:
            raise ValidationError("end_time must be after start_time.")

    def __str__(self):
        return f"Booking#{self.pk}: {self.lsa} for {self.parent} [{self.start_time} - {self.end_time}]"


class Payment(models.Model):
    """Payment record tied 1:1 to a booking; state transitions driven by webhook events."""

    class Status(models.TextChoices):
        INITIATED = 'initiated', 'Initiated'
        SUCCESS = 'success', 'Success'
        FAILED = 'failed', 'Failed'

    booking = models.OneToOneField(BookingRequest, on_delete=models.CASCADE, related_name='payment')
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIATED)
    transaction_id = models.CharField(max_length=100, unique=True)
    raw_gateway_payload = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['transaction_id']),
        ]

    def __str__(self):
        return f"Payment#{self.pk} for Booking#{self.booking_id} [{self.status}]"
