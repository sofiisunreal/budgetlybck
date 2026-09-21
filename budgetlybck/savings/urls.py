from rest_framework.routers import DefaultRouter
from .views import SavingsContributionViewSet, SavingsGoalViewSet, SavingsWithdrawalViewSet, SavingsPlanViewSet

router = DefaultRouter()
router.register(r"goals", SavingsGoalViewSet, basename="savings-goal")
router.register(r"contributions",SavingsContributionViewSet,basename="savings-contribution")
router.register(r"withdrawals",SavingsWithdrawalViewSet,basename="savings-withdrawal")
router.register(r"plans",SavingsPlanViewSet,basename="savings-plan")


urlpatterns = router.urls
