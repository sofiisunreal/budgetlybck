from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError

from .services import get_exchange_rate


class ExchangeRateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from_currency = request.query_params.get("from")
        to_currency = request.query_params.get("to")

        if not from_currency or not to_currency:
            raise ValidationError({
                "detail": "Both 'from' and 'to' currencies are required."
            })

        from_currency = from_currency.upper()
        to_currency = to_currency.upper()

        try:
            rate = get_exchange_rate(
                from_currency,
                to_currency
            )
        except Exception:
            raise ValidationError({
                "detail": "Unable to retrieve the exchange rate."
            })

        return Response({
            "from": from_currency,
            "to": to_currency,
            "rate": rate,
            "source": "live",
        })
