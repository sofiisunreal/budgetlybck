from rest_framework.viewsets import ModelViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import SavingsGoal, SavingsContribution, SavingsWithdrawal,SavingsPlan
from .serializers import SavingsGoalSerializer, SavingsContributionSerializer, SavingsPlanSerializer,  SavingsWithdrawalSerializer


class SavingsGoalViewSet(ModelViewSet):
    serializer_class = SavingsGoalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavingsGoal.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)



class SavingsGoalViewSet(ModelViewSet):
    serializer_class = SavingsGoalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavingsGoal.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        goal = self.get_object()

        if goal.status != "active":
            return Response(
                {"detail": "Only active goals can be paused."},
                status=400
            )

        goal.pause()

        return Response({
            "detail": "Savings goal paused successfully.",
            "status": goal.status
        })

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        goal = self.get_object()

        if goal.status != "paused":
            return Response(
                {"detail": "Only paused goals can be resumed."},
                status=400
            )

        goal.resume()

        return Response({
            "detail": "Savings goal resumed successfully.",
            "status": goal.status
        })

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        goal = self.get_object()

        if goal.status == "closed":
            return Response(
                {"detail": "This savings goal is already closed."},
                status=400
            )

        if goal.status == "cancelled":
            return Response(
                {"detail": "A cancelled savings goal cannot be closed."},
                status=400
            )

        goal.close()

        return Response({
            "detail": "Savings goal closed successfully.",
            "status": goal.status
     })

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        goal = self.get_object()

        if goal.status == "closed":
            return Response(
                {"detail": "A closed savings goal cannot be cancelled."},
                status=400
            )

        if goal.status == "cancelled":
            return Response(
                {"detail": "This savings goal is already cancelled."},
                status=400
            )

        goal.cancel()

        return Response({
            "detail": "Savings goal cancelled successfully.",
            "status": goal.status
        })


class SavingsContributionViewSet(ModelViewSet):
    serializer_class = SavingsContributionSerializer
    permission_classes = [IsAuthenticated]


    def get_queryset(self):
        return SavingsContribution.objects.filter(
            goal__user=self.request.user
        )

    def perform_create(self, serializer):
      contribution = serializer.save()
      contribution.goal.update_target_status()


class SavingsWithdrawalViewSet(ModelViewSet):
    serializer_class = SavingsWithdrawalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavingsWithdrawal.objects.filter(
            goal__user=self.request.user
        )

class SavingsPlanViewSet(ModelViewSet):
    serializer_class = SavingsPlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavingsPlan.objects.filter(
            goal__user=self.request.user
        )

    def perform_create(self, serializer):
        serializer.save()

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        plan = self.get_object()

        if plan.status != "active":
            return Response(
                {"detail": "Only active plans can be paused."},
                status=400
            )

        plan.status = "paused"
        plan.save(update_fields=["status"])

        return Response({
            "detail": "Savings plan paused successfully.",
            "status": plan.status
        })

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        plan = self.get_object()

        if plan.status != "paused":
            return Response(
                {"detail": "Only paused plans can be resumed."},
                status=400
            )

        plan.status = "active"
        plan.save(update_fields=["status"])

        return Response({
            "detail": "Savings plan resumed successfully.",
            "status": plan.status
        })

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        plan = self.get_object()

        if plan.status == "completed":
            return Response(
                {"detail": "This savings plan is already completed."},
                status=400
            )

        if plan.status == "cancelled":
            return Response(
                {"detail": "A cancelled plan cannot be completed."},
                status=400
            )

        plan.status = "completed"
        plan.save(update_fields=["status"])

        return Response({
            "detail": "Savings plan completed successfully.",
            "status": plan.status
        })

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        plan = self.get_object()

        if plan.status == "cancelled":
            return Response(
                {"detail": "This savings plan is already cancelled."},
                status=400
            )

        if plan.status == "completed":
            return Response(
                {"detail": "A completed plan cannot be cancelled."},
                status=400
            )

        plan.status = "cancelled"
        plan.save(update_fields=["status"])

        return Response({
            "detail": "Savings plan cancelled successfully.",
            "status": plan.status
        })
