"""
Seed demo data: sample menu items, one rider, one customer.

Idempotent — every row is matched on a natural key, so running it twice
creates nothing twice. Only the accounts named below are ever touched; any
other user in the database is left alone.

    python manage.py seed_demo
    python manage.py seed_demo --password 'Something!Else1'
    python manage.py seed_demo --reset-passwords
    python manage.py seed_demo --dry-run
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Customer, Rider, User
from menu.models import Menu

DEFAULT_PASSWORD = "Kidapawan!2026"

# Usernames this command owns. Nothing outside this set is created,
# modified or deleted.
RIDER_USERNAME = "demo_rider"
CUSTOMER_USERNAME = "demo_customer"

MENU_ITEMS = [
    {
        "name": "Pepperoni Pizza",
        "description": "Classic pepperoni over mozzarella and house tomato sauce.",
        "price": Decimal("299.00"),
        "is_available": True,
    },
    {
        "name": "Hawaiian Pizza",
        "description": "Ham and sweet pineapple.",
        "price": Decimal("289.00"),
        "is_available": True,
    },
    {
        "name": "Four Cheese Pizza",
        "description": "Mozzarella, cheddar, parmesan and blue cheese.",
        "price": Decimal("349.00"),
        "is_available": True,
    },
    {
        "name": "Beef Supreme Pizza",
        "description": "Ground beef, bell pepper, onion and olives.",
        "price": Decimal("379.00"),
        "is_available": True,
    },
    {
        "name": "Yellow Ribbon Special",
        "description": "The house pizza: five toppings and extra cheese.",
        "price": Decimal("429.00"),
        "is_available": True,
    },
    {
        "name": "Carbonara Pasta",
        "description": "Creamy bacon and mushroom sauce.",
        "price": Decimal("150.50"),
        "is_available": True,
    },
    {
        "name": "Spaghetti Bolognese",
        "description": "Slow-cooked beef and tomato.",
        "price": Decimal("145.00"),
        "is_available": True,
    },
    {
        "name": "Garlic Bread",
        "description": "Toasted with herb butter. Four pieces.",
        "price": Decimal("79.00"),
        "is_available": True,
    },
    {
        "name": "Iced Tea 1L",
        "description": "House-brewed lemon iced tea.",
        "price": Decimal("65.00"),
        "is_available": True,
    },
    {
        # Deliberately off the menu so the Unavailable badge has something
        # to render during the demo.
        "name": "Seasonal Truffle Pizza",
        "description": "Available when truffle is in season.",
        "price": Decimal("549.00"),
        "is_available": False,
    },
]

RIDER = {
    "username": RIDER_USERNAME,
    "email": "rider@yellowribbon.test",
    "first_name": "Rico",
    "last_name": "Delos Santos",
    "phone": "09171234567",
    "vehicle_type": "Motorcycle",
    "plate_number": "ABC 1234",
}

CUSTOMER = {
    "username": CUSTOMER_USERNAME,
    "email": "customer@yellowribbon.test",
    "first_name": "Maria",
    "last_name": "Santos",
    "phone": "09181112222",
    "default_address": "Blue gate, Quezon Blvd, Kidapawan City",
    # Inside the 8 km service radius.
    "default_latitude": Decimal("7.008300"),
    "default_longitude": Decimal("125.089400"),
}


class Command(BaseCommand):
    help = "Create sample menu items, one rider and one customer. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help=f"Password for the demo accounts. Default: {DEFAULT_PASSWORD}",
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help=(
                "Also reset the password on demo accounts that already exist. "
                "Without this, existing accounts are left untouched."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        self.password = options["password"]
        self.reset_passwords = options["reset_passwords"]
        self.dry_run = options["dry_run"]

        if self.dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be written.\n"))

        try:
            with transaction.atomic():
                menu = self.seed_menu()
                rider = self.seed_rider()
                customer = self.seed_customer()

                if self.dry_run:
                    raise _Rollback()
        except _Rollback:
            pass

        self.report(menu, rider, customer)

    # -- menu ----------------------------------------------------------

    def seed_menu(self):
        created = updated = unchanged = 0

        for item in MENU_ITEMS:
            fields = {k: v for k, v in item.items() if k != "name"}
            existing = Menu.objects.filter(name=item["name"]).first()

            if existing is None:
                if not self.dry_run:
                    Menu.objects.create(**item)
                created += 1
                self.stdout.write(f"  + {item['name']}  {item['price']}")
                continue

            # Only fill in blanks; never overwrite a price the admin has
            # deliberately changed.
            changes = {}
            if not existing.description and item["description"]:
                changes["description"] = item["description"]
            if changes:
                if not self.dry_run:
                    for field, value in changes.items():
                        setattr(existing, field, value)
                    existing.save(update_fields=list(changes))
                updated += 1
                self.stdout.write(f"  ~ {item['name']} ({', '.join(changes)})")
            else:
                unchanged += 1

        return {"created": created, "updated": updated, "unchanged": unchanged}

    # -- accounts ------------------------------------------------------

    def _seed_user(self, data, role_flags):
        """
        Matched on username. Returns (user, created).

        Never touches a user whose username is not one of ours.
        """
        user = User.objects.filter(username=data["username"]).first()

        if user is not None:
            if self.reset_passwords and not self.dry_run:
                user.set_password(self.password)
                user.save(update_fields=["password"])
            return user, False

        if self.dry_run:
            return None, True

        user = User(
            username=data["username"],
            email=data["email"],
            first_name=data["first_name"],
            last_name=data["last_name"],
            phone=data["phone"],
            **role_flags,
        )
        user.set_password(self.password)
        user.save()
        return user, True

    def seed_rider(self):
        user, created = self._seed_user(
            RIDER, {"is_rider": True, "is_customer": False, "is_admin": False}
        )

        if self.dry_run:
            self.stdout.write(
                f"  {'+' if created else '='} rider {RIDER_USERNAME}"
            )
            return {"created": created, "username": RIDER_USERNAME}

        profile, profile_created = Rider.objects.get_or_create(
            user=user,
            defaults={
                "vehicle_type": RIDER["vehicle_type"],
                "plate_number": RIDER["plate_number"],
                # Off duty until the rider app toggles it on.
                "is_available": False,
            },
        )
        self.stdout.write(
            f"  {'+' if created or profile_created else '='} rider {RIDER_USERNAME}"
        )
        return {"created": created, "username": RIDER_USERNAME}

    def seed_customer(self):
        user, created = self._seed_user(
            CUSTOMER, {"is_customer": True, "is_rider": False, "is_admin": False}
        )

        if self.dry_run:
            self.stdout.write(
                f"  {'+' if created else '='} customer {CUSTOMER_USERNAME}"
            )
            return {"created": created, "username": CUSTOMER_USERNAME}

        profile, profile_created = Customer.objects.get_or_create(
            user=user,
            defaults={
                "default_address": CUSTOMER["default_address"],
                "default_latitude": CUSTOMER["default_latitude"],
                "default_longitude": CUSTOMER["default_longitude"],
            },
        )
        self.stdout.write(
            f"  {'+' if created or profile_created else '='} customer {CUSTOMER_USERNAME}"
        )
        return {"created": created, "username": CUSTOMER_USERNAME}

    # -- output --------------------------------------------------------

    def report(self, menu, rider, customer):
        self.stdout.write("")
        self.stdout.write(
            f"Menu: {menu['created']} created, {menu['updated']} updated, "
            f"{menu['unchanged']} already correct."
        )

        if self.dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing was written."))
            return

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo accounts:"))
        self.stdout.write(f"  rider     {RIDER_USERNAME}")
        self.stdout.write(f"  customer  {CUSTOMER_USERNAME}")

        if rider["created"] or customer["created"] or self.reset_passwords:
            self.stdout.write(f"  password  {self.password}")
        else:
            self.stdout.write(
                "  password  unchanged (pass --reset-passwords to set it)"
            )

        self.stdout.write("")
        self.stdout.write(
            "Admins are made with `python manage.py createsuperuser` "
            "(spec section 6). This command does not create one."
        )


class _Rollback(Exception):
    """Internal: unwinds the transaction on --dry-run."""
