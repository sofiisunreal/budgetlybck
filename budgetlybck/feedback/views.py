from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle

from .models import Feedback
from .serializers import FeedbackSerializer


class FeedbackSubmissionThrottle(UserRateThrottle):
    scope = "feedback"


class FeedbackViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Users may submit and view only their own feedback; staff manage status in admin."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [FeedbackSubmissionThrottle]
    serializer_class = FeedbackSerializer

    def get_queryset(self):
        return Feedback.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
