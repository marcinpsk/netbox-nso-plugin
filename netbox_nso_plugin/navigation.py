# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins.navigation import PluginMenu, PluginMenuButton, PluginMenuItem

# The sidebar stays lean — three entries. "Settings" and "Links" each land on the first
# tab of a tabbed area page (rendered by the ``inc/_settings_tabs.html`` /
# ``inc/_links_tabs.html`` partials), so the individual config/link screens are reached via
# tabs on the page, not a long flat menu.
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
                    link_text="Settings",
                ),
                PluginMenuItem(
                    link="plugins:netbox_nso_plugin:nsolinkrole_list",
                    link_text="Links",
                ),
            ),
        ),
    ),
    icon_class="mdi mdi-router-network",
)
