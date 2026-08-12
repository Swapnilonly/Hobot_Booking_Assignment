from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Parent, LSAProfile, Skill, BookingRequest, Payment
from bookings.services import BookingService, OverlappingBookingError, PaymentWebhookService

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def parent(db):
    user = User.objects.create_user(username='parent1', password='pass1234', first_name='Priya')
    return Parent.objects.create(user=user, phone_number='9999999999')


@pytest.fixture
def lsa(db):
    user = User.objects.create_user(username='lsa1', password='pass1234', first_name='Anjali')
    profile = LSAProfile.objects.create(user=user, hourly_rate=500, is_available=True)
    skill = Skill.objects.create(name='Dyslexia Support')
    profile.skills.add(skill)
    return profile


@pytest.mark.django_db
class TestBookingCreation:
    """Covers success, overlap (edge), and validation-failure cases for the booking API."""

    def now_plus(self, hours):
        return timezone.now() + timedelta(hours=hours)

    def test_create_booking_success(self, api_client, parent, lsa):
        url = reverse('booking-create')
        payload = {
            "parent_id": parent.id,
            "lsa_id": lsa.id,
            "start_time": self.now_plus(1).isoformat(),
            "end_time": self.now_plus(2).isoformat(),
        }
        response = api_client.post(url, payload, format='json')
        assert response.status_code == 201
        assert response.data['status'] == BookingRequest.Status.PENDING_PAYMENT

    def test_overlapping_booking_is_rejected(self, api_client, parent, lsa):
        # First booking occupies 1hr -> 3hr from now.
        BookingRequest.objects.create(
            parent=parent, lsa=lsa,
            start_time=self.now_plus(1), end_time=self.now_plus(3),
            status=BookingRequest.Status.CONFIRMED,
        )
        # Second request overlaps (2hr -> 4hr) and must be rejected.
        with pytest.raises(OverlappingBookingError):
            BookingService.create_booking(
                parent=parent, lsa_id=lsa.id,
                start_time=self.now_plus(2), end_time=self.now_plus(4),
            )

    def test_adjacent_non_overlapping_booking_is_allowed(self, api_client, parent, lsa):
        # Booking ending exactly when the new one starts should NOT count as overlap.
        BookingRequest.objects.create(
            parent=parent, lsa=lsa,
            start_time=self.now_plus(1), end_time=self.now_plus(2),
            status=BookingRequest.Status.CONFIRMED,
        )
        booking = BookingService.create_booking(
            parent=parent, lsa_id=lsa.id,
            start_time=self.now_plus(2), end_time=self.now_plus(3),
        )
        assert booking.id is not None

    def test_cancelled_booking_does_not_block_overlap(self, api_client, parent, lsa):
        BookingRequest.objects.create(
            parent=parent, lsa=lsa,
            start_time=self.now_plus(1), end_time=self.now_plus(3),
            status=BookingRequest.Status.CANCELLED,
        )
        booking = BookingService.create_booking(
            parent=parent, lsa_id=lsa.id,
            start_time=self.now_plus(2), end_time=self.now_plus(4),
        )
        assert booking.id is not None

    def test_end_time_before_start_time_is_rejected(self, api_client, parent, lsa):
        url = reverse('booking-create')
        payload = {
            "parent_id": parent.id,
            "lsa_id": lsa.id,
            "start_time": self.now_plus(3).isoformat(),
            "end_time": self.now_plus(1).isoformat(),
        }
        response = api_client.post(url, payload, format='json')
        assert response.status_code == 400

    def test_missing_parent_id_returns_400(self, api_client, lsa):
        url = reverse('booking-create')
        payload = {
            "lsa_id": lsa.id,
            "start_time": self.now_plus(1).isoformat(),
            "end_time": self.now_plus(2).isoformat(),
        }
        response = api_client.post(url, payload, format='json')
        assert response.status_code == 400


@pytest.mark.django_db
class TestLSASearch:
    def test_search_returns_only_matching_skill(self, api_client, lsa):
        other_user = User.objects.create_user(username='lsa2', password='pass1234')
        other_lsa = LSAProfile.objects.create(user=other_user, hourly_rate=400, is_available=True)
        other_lsa.skills.add(Skill.objects.create(name='ADHD Coaching'))

        url = reverse('lsa-search')
        response = api_client.get(url, {"skills": "Dyslexia Support"})
        assert response.status_code == 200
        returned_ids = [item['id'] for item in response.data]
        assert lsa.id in returned_ids
        assert other_lsa.id not in returned_ids

    def test_search_query_count_stays_constant_regardless_of_lsa_count(self, api_client, lsa, django_assert_num_queries):
        # Add several more LSAs with skills to prove prefetch_related avoids N+1.
        for i in range(5):
            u = User.objects.create_user(username=f'extra_lsa_{i}', password='pass1234')
            p = LSAProfile.objects.create(user=u, hourly_rate=300, is_available=True)
            p.skills.add(Skill.objects.create(name=f'Skill-{i}'))

        url = reverse('lsa-search')
        # Exactly 2 queries regardless of N: 1 for the LSA list (joined to user),
        # 1 prefetch for all related skills. Without prefetch_related this would be N+1.
        with django_assert_num_queries(2):
            response = api_client.get(url)
        assert response.status_code == 200
        assert len(response.data) == 6


@pytest.mark.django_db
class TestPaymentWebhook:
    def _make_pending_booking_with_payment(self, parent, lsa):
        booking = BookingRequest.objects.create(
            parent=parent, lsa=lsa,
            start_time=timezone.now() + timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=2),
            status=BookingRequest.Status.PENDING_PAYMENT,
        )
        payment = Payment.objects.create(
            booking=booking, amount=500, transaction_id='txn_test_001',
            status=Payment.Status.INITIATED,
        )
        return booking, payment

    def test_successful_payment_confirms_booking(self, api_client, parent, lsa):
        booking, payment = self._make_pending_booking_with_payment(parent, lsa)
        url = reverse('payment-webhook')
        response = api_client.post(url, {
            "transaction_id": payment.transaction_id,
            "event_type": "payment.success",
            "amount": "500.00",
        }, format='json')

        assert response.status_code == 200
        booking.refresh_from_db()
        payment.refresh_from_db()
        assert booking.status == BookingRequest.Status.CONFIRMED
        assert payment.status == Payment.Status.SUCCESS

    def test_failed_payment_marks_booking_payment_failed(self, api_client, parent, lsa):
        booking, payment = self._make_pending_booking_with_payment(parent, lsa)
        url = reverse('payment-webhook')
        response = api_client.post(url, {
            "transaction_id": payment.transaction_id,
            "event_type": "payment.failed",
        }, format='json')

        assert response.status_code == 200
        booking.refresh_from_db()
        assert booking.status == BookingRequest.Status.PAYMENT_FAILED

    def test_webhook_is_idempotent_on_replay(self, api_client, parent, lsa):
        booking, payment = self._make_pending_booking_with_payment(parent, lsa)
        url = reverse('payment-webhook')
        payload = {"transaction_id": payment.transaction_id, "event_type": "payment.success"}

        first = api_client.post(url, payload, format='json')
        second = api_client.post(url, payload, format='json')  # gateway retry

        assert first.status_code == 200
        assert second.status_code == 200
        payment.refresh_from_db()
        assert payment.status == Payment.Status.SUCCESS  # unchanged, no duplicate side-effect

    def test_webhook_for_unknown_transaction_returns_400(self, api_client):
        url = reverse('payment-webhook')
        response = api_client.post(url, {
            "transaction_id": "does_not_exist",
            "event_type": "payment.success",
        }, format='json')
        assert response.status_code == 400
