"""Notification feed views."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from notifications.models import Notification


@login_required
def notification_list(request):
    """Full notification history for the signed-in user."""
    notifications = Notification.objects.for_user(request.user)[:100]
    return render(
        request,
        "notifications/list.html",
        {
            "notifications": notifications,
            "unread_total": Notification.objects.unread_count(request.user),
        },
    )


@login_required
@require_GET
def api_unread(request):
    """Polled by the navbar badge; also returns the newest few for a dropdown."""
    unread = Notification.objects.for_user(request.user).unread()[:8]
    return JsonResponse(
        {
            "count": Notification.objects.unread_count(request.user),
            "items": [
                {
                    "id": item.pk,
                    "title": item.title,
                    "body": item.body,
                    "url": item.url,
                    "icon": item.icon,
                    "tone": item.tone,
                    "created_at": item.created_at.isoformat(),
                }
                for item in unread
            ],
        }
    )


@login_required
def open_notification(request, notification_id: int):
    """Mark one notification read and forward to its target."""
    notification = get_object_or_404(
        Notification, pk=notification_id, recipient=request.user
    )
    notification.mark_read()
    return redirect(notification.url or "dashboard")


@login_required
@require_POST
def mark_all_read(request):
    count = Notification.objects.mark_all_read(request.user)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"marked": count, "count": 0})
    messages.success(request, f"Marked {count} notification(s) as read.")
    return redirect("notification_list")
