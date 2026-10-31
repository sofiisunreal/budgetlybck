from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models


def preserve_historical_repayments(apps, schema_editor):
    Repayment = apps.get_model('budget', 'LoanRepayment')
    Repayment.objects.update(principal_amount=models.F('amount'), interest_amount=Decimal('0.00'))


class Migration(migrations.Migration):
    dependencies = [('budget', '0001_initial')]

    operations = [
        migrations.AddField(model_name='loan', name='annual_interest_rate', field=models.DecimalField(decimal_places=4, default=Decimal('0.0000'), max_digits=7, validators=[MinValueValidator(Decimal('0'))])),
        migrations.AddField(model_name='loan', name='interest_enabled', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='loanrepayment', name='interest_amount', field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=14)),
        migrations.AddField(model_name='loanrepayment', name='principal_amount', field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=14)),
        migrations.RunPython(preserve_historical_repayments, migrations.RunPython.noop),
    ]
