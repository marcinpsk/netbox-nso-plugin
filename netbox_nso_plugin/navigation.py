# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins.navigation import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label="NSO",
    groups=(
        (
            "Management",
            (
                PluginMenuItem(
                    link="plugins:netbox_nso_plugin:onboarding_dashboard",
                    link_text="NSO Devices",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_nso_plugin:nsodevicemanagement_add",
                            title="Add managed device",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_nso_plugin:nsoinstance_list",
                    link_text="NSO Instances",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_nso_plugin:nsoinstance_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_nso_plugin:nsointerfacestate_list",
                    link_text="Interface Drift",
                ),
                PluginMenuItem(
                    link="plugins:netbox_nso_plugin:nsoplatformnedmapping_list",
                    link_text="Platform → NED Mappings",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_nso_plugin:nsoplatformnedmapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_nso_plugin:adapterconnection",
                    link_text="Adapter Connection",
                ),
            ),
        ),
    ),
    icon_class="mdi mdi-router-network",
)
