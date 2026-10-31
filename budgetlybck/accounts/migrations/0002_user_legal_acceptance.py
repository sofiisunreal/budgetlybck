from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.AddField(model_name="user", name="accepted_policy_version", field=models.CharField(blank=True, max_length=50)),
        migrations.AddField(model_name="user", name="age_eligibility_confirmed", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="user", name="privacy_policy_accepted_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="user", name="terms_accepted_at", field=models.DateTimeField(blank=True, null=True)),
    ]
