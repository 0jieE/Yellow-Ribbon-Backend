from django.urls import path

from . import views

app_name = "website"

urlpatterns = [
    path("", views.home, name="home"),
    path("staff/", views.staff_apps, name="staff-apps"),
    path("staff/sign-out/", views.sign_out, name="sign-out"),
    path("download/<str:app>.apk", views.download, name="download"),
]
