from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models


def preserve_v1_agreements(apps, schema_editor):
    Loan = apps.get_model('budget', 'Loan')
    Loan.objects.filter(annual_interest_rate__gt=Decimal('0')).update(interest_method='annual_percentage')


class Migration(migrations.Migration):
    dependencies = [('budget', '0002_loan_interest')]
    operations = [
        migrations.AddField(model_name='loan', name='interest_method', field=models.CharField(choices=[('none', 'No interest'), ('annual_percentage', 'Annual percentage'), ('daily_percentage', 'Daily percentage'), ('weekly_percentage', 'Weekly percentage'), ('monthly_percentage', 'Monthly percentage'), ('fixed_daily', 'Fixed daily'), ('fixed_weekly', 'Fixed weekly'), ('fixed_monthly', 'Fixed monthly'), ('fixed_total', 'Fixed total')], default='none', max_length=24)),
        migrations.AddField(model_name='loan', name='periodic_interest_rate', field=models.DecimalField(blank=True, decimal_places=4, max_digits=9, null=True, validators=[MinValueValidator(Decimal('0'))])),
        migrations.AddField(model_name='loan', name='fixed_interest_amount', field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, validators=[MinValueValidator(Decimal('0'))])),
        migrations.AddField(model_name='loan', name='fixed_charge_behavior', field=models.CharField(choices=[('full_until_settled', 'Full until settled'), ('proportional_to_principal', 'Proportional to principal')], default='full_until_settled', max_length=28)),
        migrations.RunPython(preserve_v1_agreements, migrations.RunPython.noop),
    ]
