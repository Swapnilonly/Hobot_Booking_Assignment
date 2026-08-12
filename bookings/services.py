import logging

from django.db import transaction
from django.db.models import Q, Prefetch
from rest_framework.exceptions import ValidationError as DRFValidationError

from .models import BookingRequest, LSAProfile, Payment, Skill

logger = logging.getLogger(__name__)


class OverlappingBookingError(DRFValidationError):
    """Raised when a requested slot overlaps an existing active booking for the same LSA."""
    default_detail = "This LSA already has a confirmed or pending booking that overlaps this time slot."
    default_code = "overlapping_booking"


class BookingService:
    """
    Encapsulates all booking-creation business rules so views stay thin
    and the overlap-prevention logic lives in exactly one place (Poka-Yoke).
    """

    # Bookings in these states block a new overlapping request.
    BLOCKING_STATUSES = [
        BookingRequest.Status.PENDING_PAYMENT,
        BookingRequest.Status.CONFIRMED,
    ]

    @classmethod
    def has_overlap(cls, lsa: LSAProfile, start_time, end_time, exclude_booking_id=None) -> bool:
        """
        Two ranges [a_start, a_end) and [b_start, b_end) overlap iff
        a_start < b_end AND b_start < a_end.
        This is evaluated as a single indexed query (idx_lsa_time_range), not in Python,
        so it stays O(log n) instead of loading every booking for the LSA.
        """
        qs = BookingRequest.objects.filter(
            lsa=lsa,
            status__in=cls.BLOCKING_STATUSES,
            start_time__lt=end_time,
            end_time__gt=start_time,
        )
        if exclude_booking_id:
            qs = qs.exclude(pk=exclude_booking_id)
        return qs.exists()

    @classmethod
    @transaction.atomic
    def create_booking(cls, *, parent, lsa_id, start_time, end_time, notes=""):
        # select_for_update locks the LSA's booking rows for the duration of this
        # transaction so two concurrent requests for the same slot can't both pass
        # the overlap check (classic TOCTOU race condition).
        lsa = LSAProfile.objects.select_for_update().get(pk=lsa_id)

        if end_time <= start_time:
            raise DRFValidationError({"end_time": "end_time must be after start_time."})

        if cls.has_overlap(lsa, start_time, end_time):
            logger.warning("Overlapping booking rejected for LSA %s at %s-%s", lsa_id, start_time, end_time)
            raise OverlappingBookingError()

        booking = BookingRequest.objects.create(
            parent=parent,
            lsa=lsa,
            start_time=start_time,
            end_time=end_time,
            notes=notes,
            status=BookingRequest.Status.PENDING_PAYMENT,
        )
        logger.info("Booking %s created for LSA %s by parent %s", booking.pk, lsa_id, parent.pk)
        return booking


class LSASearchService:
    """Optimized read-path for LSA discovery. Solves the N+1 problem explicitly."""

    @staticmethod
    def search(skill_names=None, available_only=True):
        """
        Without prefetch_related, serializing `lsa.skills.all()` for N LSAs fires
        1 query for the LSA list + N queries for each LSA's skills (N+1).
        prefetch_related collapses that into exactly 2 queries total, regardless of N.
        """
        qs = LSAProfile.objects.select_related('user').prefetch_related(
            Prefetch('skills', queryset=Skill.objects.only('id', 'name'))
        )

        if available_only:
            qs = qs.filter(is_available=True)

        if skill_names:
            # distinct() needed because filtering across an M2M can duplicate rows
            qs = qs.filter(skills__name__in=skill_names).distinct()

        return qs.order_by('id')


class PaymentWebhookService:
    """
    Processes inbound payment-gateway webhook events and transitions the
    related booking's state accordingly. Idempotent: replaying the same
    transaction_id with the same outcome is a safe no-op.
    """

    @staticmethod
    @transaction.atomic
    def process_event(*, transaction_id, event_type, amount=None, raw_payload=None):
        try:
            payment = Payment.objects.select_for_update().select_related('booking').get(
                transaction_id=transaction_id
            )
        except Payment.DoesNotExist:
            logger.error("Webhook received for unknown transaction_id=%s", transaction_id)
            raise DRFValidationError({"transaction_id": "No payment found for this transaction_id."})

        booking = payment.booking

        if event_type == "payment.success":
            if payment.status == Payment.Status.SUCCESS:
                logger.info("Duplicate success webhook for %s ignored (idempotent).", transaction_id)
                return payment  # already processed - idempotent no-op
            payment.status = Payment.Status.SUCCESS
            booking.status = BookingRequest.Status.CONFIRMED

        elif event_type == "payment.failed":
            if payment.status == Payment.Status.FAILED:
                logger.info("Duplicate failure webhook for %s ignored (idempotent).", transaction_id)
                return payment
            payment.status = Payment.Status.FAILED
            booking.status = BookingRequest.Status.PAYMENT_FAILED

        else:
            raise DRFValidationError({"event_type": f"Unrecognized event_type '{event_type}'."})

        payment.raw_gateway_payload = raw_payload
        payment.save(update_fields=["status", "raw_gateway_payload", "updated_at"])
        booking.save(update_fields=["status", "updated_at"])

        logger.info("Payment %s -> %s ; Booking %s -> %s", transaction_id, payment.status, booking.pk, booking.status)
        return payment
