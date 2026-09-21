from rest_framework import serializers
from .models import SavingsGoal, SavingsContribution
from django.db.models import Sum
from rest_framework import serializers

from .models import SavingsGoal, SavingsContribution, SavingsWithdrawal,SavingsPlan


from rest_framework import serializers
from .models import SavingsGoal, SavingsContribution, SavingsWithdrawal


class SavingsGoalSerializer(serializers.ModelSerializer):
    current_saved = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    progress_percentage = serializers.SerializerMethodField()

    class Meta:
        model = SavingsGoal
        fields = [
            "id",
            "name",
            "description",
            "target_amount",
            "currency",
            "target_date",
            "saving_method",
            "account",
            "status",
            "current_saved",
            "remaining_amount",
            "progress_percentage",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "current_saved",
            "remaining_amount",
            "progress_percentage",
            "created_at",
        ]

    def get_current_saved(self, obj):
        return obj.current_saved

    def get_remaining_amount(self, obj):
        return obj.remaining_amount

    def get_progress_percentage(self, obj):
        return obj.progress_percentage

    def validate_target_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(
                "Target amount must be greater than zero."
            )
        return value

    def validate_currency(self, value):
        value = value.upper()

        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError(
                "Currency must be a valid 3-letter currency code."
            )

        return value

    def validate(self, attrs):
        account = attrs.get('account', getattr(self.instance, 'account', None))
        request = self.context.get('request')

        if account is not None and request and account.user_id != request.user.id:
            raise serializers.ValidationError({
                'account': 'You can only use your own account for a savings goal.'
            })
        return attrs

class SavingsContributionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsContribution
        fields = [
            "id",
            "goal",
            "amount",
            "contribution_date",
            "note",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
        ]

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(
                "Contribution amount must be greater than zero."
            )
        return value

    def validate(self, attrs):
      goal = attrs.get("goal", getattr(self.instance, "goal", None))
      request = self.context.get("request")

      if request and goal.user != request.user:
          raise serializers.ValidationError({
              "goal": "You can only contribute to your own savings goals."
          })

      if goal.status in ["paused", "closed", "cancelled"]:
          raise serializers.ValidationError({
              "goal": f"You cannot contribute to a {goal.status} savings goal."
          })

      return attrs


class SavingsWithdrawalSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsWithdrawal
        fields = [
            "id",
            "goal",
            "amount",
            "withdrawal_date",
            "note",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
        ]

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(
                "Withdrawal amount must be greater than zero."
            )
        return value

    def validate(self, attrs):
        goal = attrs.get("goal", getattr(self.instance, "goal", None))
        request = self.context.get("request")
        amount = attrs.get("amount", getattr(self.instance, "amount", None))

        if request and goal.user != request.user:
            raise serializers.ValidationError({
                "goal": "You can only withdraw from your own savings goals."
            })

        if goal.status not in ["active", "paused"]:
            raise serializers.ValidationError({
                "goal": f"You cannot withdraw from a {goal.status} savings goal."
            })

        total_contributions = goal.contributions.aggregate(
            total=Sum("amount")
        )["total"] or 0

        total_withdrawals = goal.withdrawals.exclude(
            pk=getattr(self.instance, "pk", None)
        ).aggregate(
            total=Sum("amount")
        )["total"] or 0

        current_saved = total_contributions - total_withdrawals

        if amount > current_saved:
            raise serializers.ValidationError({
                "amount": (
                    f"Withdrawal cannot exceed the current saved amount "
                    f"of {current_saved}."
                )
            })

        return attrs

class SavingsPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsPlan
        fields = [
            "id",
            "goal",
            "amount",
            "frequency",
            "start_date",
            "end_date",
            "status",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "created_at",
        ]

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(
                "Plan amount must be greater than zero."
            )
        return value

    def validate(self, attrs):
        goal = attrs.get("goal", getattr(self.instance, "goal", None))
        request = self.context.get("request")

        if request and goal.user != request.user:
            raise serializers.ValidationError({
                "goal": "You can only create a savings plan for your own savings goal."
            })

        if goal.status in ["closed", "cancelled"]:
            raise serializers.ValidationError({
                "goal": f"You cannot create a plan for a {goal.status} savings goal."
            })

        start_date = attrs.get("start_date")
        end_date = attrs.get("end_date")

        if end_date and end_date < start_date:
            raise serializers.ValidationError({
                "end_date": "End date cannot be earlier than the start date."
            })

        return attrs
