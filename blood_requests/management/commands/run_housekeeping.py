"""Periodic housekeeping: expire stale invitations and overdue requests.

Intended for cron / systemd timer:

    */15 * * * * cd /path/to/project && python manage.py run_housekeeping
"""

from django.core.management.base import BaseCommand

from blood_requests.services import (
    expire_overdue_requests,
    expire_stale_invitations,
    refresh_donor_availability,
)


class Command(BaseCommand):
    help = "Expire stale invitations/requests and reactivate rested donors."

    def handle(self, *args, **options):
        invitations = expire_stale_invitations()
        requests = expire_overdue_requests()
        reactivated = refresh_donor_availability()

        self.stdout.write(self.style.SUCCESS("Housekeeping complete."))
        self.stdout.write(f"  invitations expired : {invitations}")
        self.stdout.write(f"  requests expired    : {requests}")
        self.stdout.write(f"  donors reactivated  : {reactivated}")
