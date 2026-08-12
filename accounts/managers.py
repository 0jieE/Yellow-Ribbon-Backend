from django.contrib.auth.models import UserManager as DjangoUserManager


class UserManager(DjangoUserManager):
    """
    Email is unique and required by the schema, so it is enforced here
    rather than left to the serializer.
    """

    def _create_user(self, username, email, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        return super()._create_user(username, email, password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # `createsuperuser` is how admins are made (spec section 6), so the
        # role flag has to line up with Django's own staff/superuser flags.
        extra_fields.setdefault("is_admin", True)
        extra_fields.setdefault("is_customer", False)
        return super().create_superuser(username, email, password, **extra_fields)
