# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_nso_plugin"

router = NetBoxRouter()
router.register("instances", views.NSOInstanceViewSet)
router.register("device-management", views.NSODeviceManagementViewSet)
router.register("interface-state", views.NSOInterfaceStateViewSet)

urlpatterns = router.urls
