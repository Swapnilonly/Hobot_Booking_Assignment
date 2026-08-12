import logging

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from .models import Parent
from .serializers import (
    BookingCreateSerializer,
    BookingResponseSerializer,
    LSASearchResultSerializer,
    PaymentWebhookSerializer,
)
from .services import BookingService, LSASearchService, PaymentWebhookService

logger = logging.getLogger(__name__)


class LSASearchView(APIView):
    """GET /api/v1/lsas/search/?skills=Dyslexia,ADHD"""

    def get(self, request):
        skills_param = request.query_params.get('skills')
        skill_names = [s.strip() for s in skills_param.split(',')] if skills_param else None

        queryset = LSASearchService.search(skill_names=skill_names)
        serializer = LSASearchResultSerializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class BookingCreateView(APIView):
    """POST /api/v1/bookings/"""

    def post(self, request):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # NOTE: in a real deployment `parent` comes from request.user via auth
        # (IsAuthenticated + request.user.parent_profile). For this assessment,
        # parent_id is accepted explicitly to keep the endpoint testable without
        # standing up full auth.
        parent_id = request.data.get('parent_id')
        if not parent_id:
            raise ValidationError({"parent_id": "This field is required."})
        try:
            parent = Parent.objects.get(pk=parent_id)
        except Parent.DoesNotExist:
            raise ValidationError({"parent_id": "Parent does not exist."})

        booking = BookingService.create_booking(
            parent=parent,
            lsa_id=data['lsa_id'],
            start_time=data['start_time'],
            end_time=data['end_time'],
            notes=data.get('notes', ''),
        )
        return Response(BookingResponseSerializer(booking).data, status=status.HTTP_201_CREATED)


class PaymentWebhookView(APIView):
    """
    POST /api/payments/webhook/
    Listens for payment success/failure events from the (mock) payment gateway
    and drives the booking's state machine. Designed to be safely re-triggered
    (idempotent) since gateways commonly retry webhook delivery.
    """

    def post(self, request):
        serializer = PaymentWebhookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            payment = PaymentWebhookService.process_event(
                transaction_id=data['transaction_id'],
                event_type=data['event_type'],
                amount=data.get('amount'),
                raw_payload=request.data,
            )
        except ValidationError:
            raise
        except Exception:
            logger.exception("Unexpected error processing webhook for %s", data.get('transaction_id'))
            return Response({"detail": "Internal error processing webhook."},
                             status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"transaction_id": payment.transaction_id, "status": payment.status},
                         status=status.HTTP_200_OK)
