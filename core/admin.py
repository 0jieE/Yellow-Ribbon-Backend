"""
Django admin branding.

Section 13 makes the admin the demo fallback if a Flutter screen misbehaves
during the defense, so it is worth it not looking like a stock install.
"""

from django.contrib import admin

admin.site.site_header = "Yellow Ribbon Pizza"
admin.site.site_title = "Yellow Ribbon Pizza"
admin.site.index_title = "Delivery system administration"
