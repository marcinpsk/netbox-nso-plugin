# SPDX-License-Identifier: Apache-2.0
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0002_nsoisisinterfacestate_isis_interface"),
    ]

    operations = [
        migrations.AddField(
            model_name="adapterconnection",
            name="static_route_auto_create",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="adapterconnection",
            name="interface_ip_auto_create",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="adapterconnection",
            name="vrf_auto_create",
            field=models.BooleanField(default=False),
        ),
    ]
