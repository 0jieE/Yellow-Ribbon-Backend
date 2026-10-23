from django.contrib.auth.models import UserManager as DjangoUserManager


class UserManager(DjangoUserManager):
    """
    Email is optional. Django's own manager already stores a missing address
    as "", which is what the conditional unique constraint on User expects.
    """

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # `createsuperuser` is how admins are made (spec section 6), so the
        # role flag has to line up with Django's own staff/superuser flags.
        extra_fields.setdefault("is_admin", True)
        extra_fields.setdefault("is_customer", False)
        return super().create_superuser(username, email, password, **extra_fields)
