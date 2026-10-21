"""Tenant gym operations discovered by Django's model/migration registry."""
from .members.models import MemberInvitation, MemberProfile
from .memberships.models import (MembershipApplication, MembershipEvent, MembershipPlan, MemberSubscription)

from .attendance.models import AttendanceScan, AttendanceVisit
from .classes.models import Trainer, ClassSession, ClassBooking, BookingEvent
from .notifications.models import NotificationSettings, EmailNotification, EmailAttempt
