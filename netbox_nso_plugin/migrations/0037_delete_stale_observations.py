from django.db import migrations
from django.db.models import F

STALE_FAMILIES = ("interface_attributes", "interface_ip", "redistribution")


def delete_stale_observations(apps, schema_editor):
    """Delete snapshots stored before #1833 added presence and status; the next authoritative read stores them again."""
    NSOFamilyObservation = apps.get_model("netbox_nso_plugin", "NSOFamilyObservation")
    NSOFamilyReadState = apps.get_model("netbox_nso_plugin", "NSOFamilyReadState")
    states = list(
        NSOFamilyReadState.objects.filter(family__in=STALE_FAMILIES, observation__isnull=False).values_list(
            "pk", flat=True
        )
    )
    NSOFamilyObservation.objects.filter(read_state_id__in=states).delete()
    NSOFamilyReadState.objects.filter(pk__in=states).update(publication_sequence=F("publication_sequence") + 1)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0036_deployment_probe"),
    ]

    operations = [
        migrations.RunPython(delete_stale_observations, migrations.RunPython.noop),
    ]
