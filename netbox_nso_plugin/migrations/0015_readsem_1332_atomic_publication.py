# Generated for READSEM 1332.

from django.db import migrations, models


def copy_applied_to_admitted(apps, schema_editor):
    State = apps.get_model("netbox_nso_plugin", "NSOFamilyReadState")
    for row in State.objects.all().iterator():
        row.admitted_attempt_id = row.applied_attempt_id
        row.admitted_incarnation = row.applied_incarnation
        row.save(update_fields=["admitted_attempt_id", "admitted_incarnation"])


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0014_nsodevicemanagement_adapter_incarnation_and_more")]

    operations = [
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="adapter_source_epoch",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="source_epoch_aware",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="source_rekey_pending",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="reset_pending_source_epoch",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="observed_source_epoch",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="observed_payload_revision",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="admitted_attempt_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="admitted_incarnation",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="admitted_source_epoch",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="admitted_payload_revision",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="applied_source_epoch",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="applied_payload_revision",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="publication_sequence",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="nsofamilyreadstate",
            name="applied_publication_sequence",
            field=models.BigIntegerField(default=0),
        ),
        migrations.RunPython(copy_applied_to_admitted, migrations.RunPython.noop),
    ]
