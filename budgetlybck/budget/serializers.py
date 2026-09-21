from calendar import monthrange
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum
from rest_framework import serializers
from exchange_rates.services import get_exchange_rate


from .models import Account, Budget, Category, Loan, LoanRepayment, Transaction, Transfer


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'category_type', 'user']
        read_only_fields = ['id', 'user']


class CategoryRelatedSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'category_type']


class AccountRelatedSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ['id', 'name', 'currency']


class AccountSerializer(serializers.ModelSerializer):
    current_balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Account
        fields = ['id', 'name', 'account_type', 'currency', 'starting_balance', 'is_active', 'current_balance', 'created_at']
        read_only_fields = ['id', 'current_balance', 'created_at']

    def validate_currency(self, value):
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError('Use a three-letter currency code, such as KES or USD.')
        return value.upper()


class TransactionSerializer(serializers.ModelSerializer):
    category_detail = CategoryRelatedSerializer(source='category', read_only=True)
    account_detail = AccountRelatedSerializer(source='account', read_only=True)

    class Meta:
        model = Transaction
        fields = ['id', 'account', 'account_detail', 'category', 'category_detail', 'transaction_type', 'description', 'amount', 'transaction_date', 'notes', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        request = self.context['request']
        account = attrs.get('account', getattr(self.instance, 'account', None))
        category = attrs.get('category', getattr(self.instance, 'category', None))
        transaction_type = attrs.get('transaction_type', getattr(self.instance, 'transaction_type', None))
        if account is None:
            raise serializers.ValidationError({'account': 'An account is required.'})
        if account.user_id != request.user.id:
            raise serializers.ValidationError({'account': 'This account is not available to you.'})
        if category.user_id not in (None, request.user.id):
            raise serializers.ValidationError({'category': 'This category is not available to you.'})
        if category.category_type != transaction_type:
            raise serializers.ValidationError({'category': 'The category type must match the transaction type.'})
        return attrs







class TransferSerializer(serializers.ModelSerializer):

    from_account_detail = AccountRelatedSerializer(
        source="from_account",
        read_only=True
    )

    to_account_detail = AccountRelatedSerializer(
        source="to_account",
        read_only=True
    )

    to_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True
    )
    exchange_rate = serializers.DecimalField(
        max_digits=18,
        decimal_places=8,
        required=False
    )

    class Meta:
        model = Transfer

        fields = [
            "id",
            "from_account",
            "from_account_detail",
            "to_account",
            "to_account_detail",
            "from_amount",
            "to_amount",
            "exchange_rate",
            "rate_source",
            "transaction_date",
            "description",
            "created_at",
        ]

        read_only_fields = [
            "id",
            "to_amount",
            "created_at",
        ]

    def validate(self, attrs):

        request = self.context["request"]

        if self.instance:
            immutable_fields = {
                "from_account": "from account",
                "to_account": "to account",
                "from_amount": "from amount",
                "exchange_rate": "exchange rate",
                "rate_source": "rate source",
            }
            changed_fields = []
            for field, label in immutable_fields.items():
                if field in attrs and attrs[field] != getattr(self.instance, field):
                    changed_fields.append(label)

            if changed_fields:
                raise serializers.ValidationError({
                    "detail": (
                        "Historical transfer financial fields cannot be changed: "
                        + ", ".join(changed_fields) + "."
                    )
                })

            # A PATCH/PUT of non-financial fields must preserve the financial
            # snapshot and must never ask the live provider for a new rate.
            attrs["exchange_rate"] = self.instance.exchange_rate
            attrs["to_amount"] = self.instance.to_amount
            return attrs

        from_account = attrs.get(
            "from_account",
            getattr(self.instance, "from_account", None)
        )

        to_account = attrs.get(
            "to_account",
            getattr(self.instance, "to_account", None)
        )

        from_amount = attrs.get(
            "from_amount",
            getattr(self.instance, "from_amount", None)
        )

        # CHANGED:
        # Get the rate source so we can determine whether to
        # fetch a live rate or use a custom rate.
        rate_source = attrs.get(
            "rate_source",
            getattr(self.instance, "rate_source", "custom")
        )

        # CHANGED:
        # Get the exchange rate that was supplied by the user.
        # We may replace this value below if rate_source is "live".
        exchange_rate = attrs.get(
            "exchange_rate",
            getattr(self.instance, "exchange_rate", None)
        )

        # Check that both accounts belong to the logged-in user.
        if (
            from_account.user_id != request.user.id
            or to_account.user_id != request.user.id
        ):
            raise serializers.ValidationError(
                "Both accounts must belong to you."
            )

        # The source and destination accounts cannot be the same.
        if from_account == to_account:
            raise serializers.ValidationError({
                "to_account": "The destination account must be different."
            })

        # CHANGED:
        # If both accounts use the same currency, the exchange rate
        # is always 1. We don't need to call the external API.
        if from_account.currency == to_account.currency:

            exchange_rate = Decimal("1")

        # CHANGED:
        # For different currencies, fetch the live rate when
        # rate_source is set to "live".
        elif rate_source == "live":

            try:
                exchange_rate = Decimal(
                    str(
                        get_exchange_rate(
                            from_account.currency,
                            to_account.currency
                        )
                    )
                )

            except Exception:
                raise serializers.ValidationError({
                    "exchange_rate": (
                        "Unable to retrieve the live exchange rate."
                    )
                })

        # CHANGED:
        # For custom rates, the user must provide an exchange rate.
        elif rate_source == "custom":

            if exchange_rate is None:
                raise serializers.ValidationError({
                    "exchange_rate": (
                        "Exchange rate is required for custom rates."
                    )
                })

        # CHANGED:
        # Reject an invalid rate source instead of silently
        # accepting an unexpected value.
        else:
            raise serializers.ValidationError({
                "rate_source": (
                    'Rate source must be either "live" or "custom".'
                )
            })

        # CHANGED:
        # Make sure the final exchange rate is positive.
        if exchange_rate <= 0:
            raise serializers.ValidationError({
                "exchange_rate": (
                    "Exchange rate must be greater than zero."
                )
            })

        # Same-currency transfers must always have a rate of 1.
        if (
            from_account.currency == to_account.currency
            and exchange_rate != Decimal("1")
        ):
            raise serializers.ValidationError({
                "exchange_rate": (
                    "Same-currency transfers must use "
                    "an exchange rate of 1."
                )
            })

        # Calculate the destination amount using the final
        # exchange rate, whether it came from the API or the user.
        attrs["exchange_rate"] = exchange_rate

        attrs["to_amount"] = (
            from_amount * exchange_rate
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

        return attrs

class LoanRepaymentSerializer(serializers.ModelSerializer):
    account_detail = AccountRelatedSerializer(source='account', read_only=True)

    class Meta:
        model = LoanRepayment
        fields = ['id', 'loan', 'account', 'account_detail', 'amount', 'repayment_date', 'note', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate(self, attrs):
        request = self.context['request']
        loan = attrs.get('loan', getattr(self.instance, 'loan', None))
        account = attrs.get('account', getattr(self.instance, 'account', None))
        amount = attrs.get('amount', getattr(self.instance, 'amount', None))
        if loan.user_id != request.user.id:
            raise serializers.ValidationError({'loan': 'This loan is not available to you.'})
        if account.user_id != request.user.id:
            raise serializers.ValidationError({'account': 'This account is not available to you.'})
        if account.currency != loan.currency:
            raise serializers.ValidationError({
                'account': 'Repayment account currency must match the loan currency.'
            })
        repaid = loan.repayments.exclude(pk=getattr(self.instance, 'pk', None)).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        if amount + repaid > loan.original_amount:
            raise serializers.ValidationError({'amount': 'Repayment cannot exceed the remaining loan amount.'})
        return attrs


class LoanSerializer(serializers.ModelSerializer):
    account_detail = AccountRelatedSerializer(source='account', read_only=True)
    remaining_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    repayments = LoanRepaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Loan
        fields = ['id', 'loan_type', 'person_name', 'original_amount', 'currency', 'account', 'account_detail', 'loan_date', 'due_date', 'description', 'status', 'remaining_amount', 'repayments', 'created_at']
        read_only_fields = ['id', 'status', 'remaining_amount', 'repayments', 'created_at']

    def validate(self, attrs):
        request = self.context['request']
        account = attrs.get('account', getattr(self.instance, 'account', None))
        currency = attrs.get('currency', getattr(self.instance, 'currency', None))
        original_amount = attrs.get(
            'original_amount', getattr(self.instance, 'original_amount', None)
        )
        if account.user_id != request.user.id:
            raise serializers.ValidationError({'account': 'This account is not available to you.'})
        if currency.upper() != account.currency:
            raise serializers.ValidationError({'currency': 'Loan currency must match the account currency.'})
        if self.instance:
            repaid = self.instance.repayments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            if original_amount < repaid:
                raise serializers.ValidationError({
                    'original_amount': 'Original amount cannot be less than recorded repayments.'
                })
        return attrs

    def validate_currency(self, value):
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError('Use a three-letter currency code.')
        return value.upper()


class BudgetSerializer(serializers.ModelSerializer):
    category_detail = CategoryRelatedSerializer(source='category', read_only=True)
    spent_amount = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    percentage_used = serializers.SerializerMethodField()

    class Meta:
        model = Budget
        fields = ['id', 'category', 'category_detail', 'amount', 'period_type', 'start_date', 'end_date', 'rollover', 'spent_amount', 'remaining_amount', 'percentage_used', 'created_at', 'updated_at']
        read_only_fields = ['id', 'spent_amount', 'remaining_amount', 'percentage_used', 'created_at', 'updated_at']

    def validate(self, attrs):
        request = self.context['request']
        category = attrs.get('category', getattr(self.instance, 'category', None))
        period_type = attrs.get('period_type', getattr(self.instance, 'period_type', None))
        start_date = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end_date = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        if category.user_id not in (None, request.user.id):
            raise serializers.ValidationError({'category': 'This category is not available to you.'})
        if category.category_type != 'expense':
            raise serializers.ValidationError({'category': 'Budgets can only be set for expense categories.'})
        if end_date < start_date:
            raise serializers.ValidationError({'end_date': 'End date must be on or after start date.'})
        if period_type == 'weekly' and end_date != start_date + timedelta(days=6):
            raise serializers.ValidationError({'end_date': 'Weekly budgets must span seven days.'})
        if period_type == 'monthly':
            expected_end = start_date.replace(day=monthrange(start_date.year, start_date.month)[1])
            if start_date.day != 1 or end_date != expected_end:
                raise serializers.ValidationError('Monthly budgets must cover a calendar month.')
        return attrs

    def _spent_amount(self, instance):
        return instance.user.transactions.filter(category=instance.category, transaction_type='expense', transaction_date__range=(instance.start_date, instance.end_date)).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    def get_spent_amount(self, instance):
        return self._spent_amount(instance)

    def get_remaining_amount(self, instance):
        return instance.amount - self._spent_amount(instance)

    def get_percentage_used(self, instance):
        if not instance.amount:
            return Decimal('0.00')
        return (self._spent_amount(instance) / instance.amount * 100).quantize(Decimal('0.01'))
