from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Feedback
from .views import FeedbackSubmissionThrottle


User = get_user_model()


class FeedbackApiTests(APITestCase):
    url = "/api/feedback/"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("feedback-user", "feedback@example.com", "SafePassword123!")
        self.other_user = User.objects.create_user("other-user", "other@example.com", "SafePassword123!")

    def payload(self, **overrides):
        data = {"category": "bug", "title": "Balance screen", "description": "The chart should show a clearer error."}
        data.update(overrides)
        return data

    def test_authentication_and_validation_are_required(self):
        self.assertEqual(self.client.post(self.url, self.payload(), format="json").status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, self.payload(category="invalid"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("category", response.data)

    def test_submission_uses_request_user_and_default_status(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, self.payload(status="resolved"), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.user, self.user)
        self.assertEqual(feedback.status, Feedback.Status.NEW)

    def test_users_cannot_list_or_retrieve_other_users_feedback(self):
        other_feedback = Feedback.objects.create(
            user=self.other_user, category=Feedback.Category.GENERAL, title="Private", description="Only the owner may view this."
        )
        self.client.force_authenticate(self.user)
        list_response = self.client.get(self.url)
        detail_response = self.client.get(f"{self.url}{other_feedback.pk}/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data, [])
        self.assertEqual(detail_response.status_code, status.HTTP_404_NOT_FOUND)


class FeedbackThrottleTests(APITestCase):
    url = "/api/feedback/"

    def setUp(self):
        cache.clear()
        self.original_rates = FeedbackSubmissionThrottle.THROTTLE_RATES
        FeedbackSubmissionThrottle.THROTTLE_RATES = {"feedback": "1/min"}
        self.user = User.objects.create_user("throttled-user", "throttled@example.com", "SafePassword123!")
        self.client.force_authenticate(self.user)

    def tearDown(self):
        FeedbackSubmissionThrottle.THROTTLE_RATES = self.original_rates
        cache.clear()

    def test_submission_is_throttled(self):
        payload = {"category": "general", "title": "First", "description": "One submission."}
        self.assertEqual(self.client.post(self.url, payload, format="json").status_code, status.HTTP_201_CREATED)
        payload["title"] = "Second"
        self.assertEqual(self.client.post(self.url, payload, format="json").status_code, status.HTTP_429_TOO_MANY_REQUESTS)
