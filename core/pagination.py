"""Pagination for the admin order queue."""

from rest_framework.pagination import PageNumberPagination


class AdminOrderPagination(PageNumberPagination):
    """
    Applied only to the admin order list, which is the one endpoint whose
    result set grows without bound. The customer, menu and rider lists stay
    unpaginated so their response shape does not change.

    Responds with {"count", "next", "previous", "results"}.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
