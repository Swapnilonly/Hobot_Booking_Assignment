from rest_framework import serializers

from .models import BookingRequest, LSAProfile, Skill, Payment


class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = ['id', 'name']


class LSASearchResultSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='user.get_full_name', read_only=True)
    skills = SkillSerializer(many=True, read_only=True)

    class Meta:
        model = LSAProfile
        fields = ['id', 'name', 'bio', 'hourly_rate', 'is_available', 'skills']


class BookingCreateSerializer(serializers.Serializer):
    lsa_id = serializers.IntegerField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_lsa_id(self, value):
        if not LSAProfile.objects.filter(pk=value).exists():
            raise serializers.ValidationError("LSA does not exist.")
        return value


class BookingResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingRequest
        fields = ['id', 'parent', 'lsa', 'start_time', 'end_time', 'status', 'notes', 'created_at']
        read_only_fields = fields


class PaymentWebhookSerializer(serializers.Serializer):
    """
    Mirrors a typical mock payment-gateway webhook payload, e.g.:
    {
      "transaction_id": "txn_abc123",
      "event_type": "payment.success",
      "amount": "45.00"
    }
    """
    transaction_id = serializers.CharField()
    event_type = serializers.ChoiceField(choices=["payment.success", "payment.failed"])
    amount = serializers.DecimalField(max_digits=8, decimal_places=2, required=False)
