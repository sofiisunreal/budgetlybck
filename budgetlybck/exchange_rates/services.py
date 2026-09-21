import requests


FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v2"


def get_exchange_rate(from_currency, to_currency):
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        return 1

    url = f"{FRANKFURTER_BASE_URL}/rate/{from_currency}/{to_currency}"

    response = requests.get(url, timeout=10)
    response.raise_for_status()

    data = response.json()

    return data["rate"]
