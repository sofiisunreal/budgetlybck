from django.urls import path

from .views import (BillingAccessView, PaystackCallbackView, PaystackWebhookView,
                    PurchaseDetailView, PurchaseInitializeView, PurchaseVerifyView)


urlpatterns = [
    path('access/', BillingAccessView.as_view(), name='billing-access'),
    path('purchases/initialize/', PurchaseInitializeView.as_view(), name='purchase-initialize'),
    path('purchases/<str:reference>/', PurchaseDetailView.as_view(), name='purchase-detail'),
    path('purchases/<str:reference>/verify/', PurchaseVerifyView.as_view(), name='purchase-verify'),
    path('paystack/callback/', PaystackCallbackView.as_view(), name='paystack-callback'),
    path('paystack/webhook/', PaystackWebhookView.as_view(), name='paystack-webhook'),
]
