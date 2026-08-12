"""
Role-based DRF permissions (spec section 6).

Every endpoint declares one of these explicitly. Nothing is left on the
DRF default.
"""

from rest_framework.permissions import BasePermission


class _RolePermission(BasePermission):
    """Authenticated, active, and carrying the required role flag."""

    role_flag = None
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and getattr(user, self.role_flag, False)
        )


class IsAdminUser(_RolePermission):
    """Shadows DRF's IsAdminUser on purpose — ours checks `is_admin`, not `is_staff`."""

    role_flag = "is_admin"
    message = "Admin access required."


class IsRiderUser(_RolePermission):
    role_flag = "is_rider"
    message = "Rider access required."


class IsCustomerUser(_RolePermission):
    role_flag = "is_customer"
    message = "Customer access required."
