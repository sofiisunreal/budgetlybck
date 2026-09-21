from django.db import models
from django.conf import settings
from django.db.models.aggregates import Sum
from django.db.models.aggregates import Sum


class SavingsGoal(models.Model):
  SAVING_LOCATIONS = [
      ("mpesa", "M-Pesa"),
      ("bank", "Bank"),
      ("cash", "Cash"),
      ("sacco", "SACCO"),
      ("other", "Other"),
    ]

  STATUSES = [
        ("active", "Active"),
        ("target_reached", "Target Reached"),
        ("paused", "Paused"),
        ("closed", "Closed"),
        ("cancelled", "Cancelled"),
      ]

  user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_goals"
    )

  name = models.CharField(max_length=100)

  description = models.TextField(blank=True)

  target_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2
  )

  currency = models.CharField(
        max_length=3,
        default="KES"
  )

  target_date = models.DateField(
        null=True,
        blank=True
  )

  saving_method = models.CharField(
      max_length=20,
      choices=SAVING_LOCATIONS,
      default="tracking_only"
  )

  account = models.ForeignKey(
      "budget.Account",
      on_delete=models.SET_NULL,
      null=True,
      blank=True,
      related_name="savings_goals"
  )

  status = models.CharField(
        max_length=20,
        choices=STATUSES,
        default="active"
    )

  created_at = models.DateTimeField(auto_now_add=True)

  @property
  def current_saved(self):
    total_contributions = self.contributions.aggregate(
      total= Sum("amount")
    )["total"] or 0

    total_withdrawals = self.withdrawals.aggregate(
        total=Sum("amount")
    )["total"] or 0

    return total_contributions - total_withdrawals


  @property
  def remaining_amount(self):
    remaining = self.target_amount - self.current_saved

    return max(remaining, 0)
  @property
  def progress_percentage(self):
      if self.target_amount <= 0:
          return 0

      percentage = (self.current_saved / self.target_amount) * 100

      return min(percentage, 100)


  def close(self):
      self.status = "closed"
      self.save(update_fields=["status"])


  def cancel(self):
      self.status = "cancelled"
      self.save(update_fields=["status"])


  def pause(self):
      self.status = "paused"
      self.save(update_fields=["status"])


  def resume(self):
      self.status = "active"
      self.save(update_fields=["status"])


  def update_target_status(self):
      if self.status == "active" and self.current_saved >= self.target_amount:
          self.status = "target_reached"
          self.save(update_fields=["status"])


  def __str__(self):
      return self.name


class SavingsContribution(models.Model):
    goal = models.ForeignKey(
        SavingsGoal,
        on_delete=models.CASCADE,
        related_name="contributions"
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    contribution_date = models.DateField()

    note = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return f"{self.goal.name} - {self.amount}"

class SavingsWithdrawal(models.Model):
    goal = models.ForeignKey(
        SavingsGoal,
        on_delete=models.CASCADE,
        related_name="withdrawals"
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    withdrawal_date = models.DateField()

    note = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return f"{self.goal.name} - {self.amount}"

class SavingsPlan(models.Model):
    FREQUENCIES = [
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
    ]

    STATUSES = [
        ("active", "Active"),
        ("paused", "Paused"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    goal = models.ForeignKey(
        SavingsGoal,
        on_delete=models.CASCADE,
        related_name="plans"
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    frequency = models.CharField(
        max_length=10,
        choices=FREQUENCIES
    )

    start_date = models.DateField()
    end_date = models.DateField(
        null=True,
        blank=True
    )

    status = models.CharField(
        max_length=20,
        choices=STATUSES,
        default="active"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return f"{self.goal.name} - {self.amount} {self.frequency}"
