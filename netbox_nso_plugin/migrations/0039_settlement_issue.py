# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Record immutable settlement issue revisions and acknowledged obligations."""

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models

_ISSUE_TABLE = "netbox_nso_plugin_nsosettlementissue"
_OWNER_TABLE = "netbox_nso_plugin_nsosettlementissueowner"
_FREEZE_SQL = f"""
CREATE FUNCTION nso_settlement_issue_freeze() RETURNS trigger AS $$
BEGIN
    IF ROW(NEW.management_id, NEW.mapping_epoch, NEW.adapter_device_id, NEW.store_incarnation, NEW.job_id,
           NEW.settle_seq, NEW.request_fence, NEW.stall_class, NEW.stall_reason, NEW.position, NEW.cursor_before,
           NEW.cursor_after, NEW.evidence_digest, NEW.impact_digest, NEW.contract_fingerprint, NEW.revision_digest,
           NEW.proposal, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.management_id, OLD.mapping_epoch, OLD.adapter_device_id, OLD.store_incarnation, OLD.job_id,
           OLD.settle_seq, OLD.request_fence, OLD.stall_class, OLD.stall_reason, OLD.position, OLD.cursor_before,
           OLD.cursor_after, OLD.evidence_digest, OLD.impact_digest, OLD.contract_fingerprint, OLD.revision_digest,
           OLD.proposal, OLD.created_at) THEN
        RAISE EXCEPTION 'settlement issue revision % is immutable', OLD.id;
    END IF;
    IF OLD.state = 'acknowledged' AND
       ROW(NEW.state, NEW.closed_at, NEW.actor_id, NEW.actor_label, NEW.acknowledgement_reason, NEW.acknowledged_at,
           NEW.committed_effects)
       IS DISTINCT FROM
       ROW(OLD.state, OLD.closed_at, OLD.actor_id, OLD.actor_label, OLD.acknowledgement_reason, OLD.acknowledged_at,
           OLD.committed_effects) THEN
        RAISE EXCEPTION 'settlement issue acknowledgement % is immutable', OLD.id;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
CREATE TRIGGER nso_settlement_issue_freeze BEFORE UPDATE ON {_ISSUE_TABLE}
    FOR EACH ROW EXECUTE FUNCTION nso_settlement_issue_freeze();
CREATE FUNCTION nso_settlement_issue_owner_freeze() RETURNS trigger AS $$
BEGIN
    IF ROW(NEW.issue_id, NEW.model_label, NEW.overlay_pk, NEW.snapshot_ids, NEW.attempt_id, NEW.selection_ids,
           NEW.contributors, NEW.unknown, NEW.authority, NEW.order_bound, NEW.proposed_effect)
       IS DISTINCT FROM
       ROW(OLD.issue_id, OLD.model_label, OLD.overlay_pk, OLD.snapshot_ids, OLD.attempt_id, OLD.selection_ids,
           OLD.contributors, OLD.unknown, OLD.authority, OLD.order_bound, OLD.proposed_effect)
       OR OLD.committed_effect IS NOT NULL AND NEW.committed_effect IS DISTINCT FROM OLD.committed_effect
       OR NOT (NEW.covered @> OLD.covered) THEN
        RAISE EXCEPTION 'settlement issue owner % is immutable', OLD.id;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
CREATE TRIGGER nso_settlement_issue_owner_freeze BEFORE UPDATE ON {_OWNER_TABLE}
    FOR EACH ROW EXECUTE FUNCTION nso_settlement_issue_owner_freeze();
"""
_THAW_SQL = f"""
DROP TRIGGER IF EXISTS nso_settlement_issue_owner_freeze ON {_OWNER_TABLE};
DROP FUNCTION IF EXISTS nso_settlement_issue_owner_freeze();
DROP TRIGGER IF EXISTS nso_settlement_issue_freeze ON {_ISSUE_TABLE};
DROP FUNCTION IF EXISTS nso_settlement_issue_freeze();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0038_settlement_evidence_error"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSOSettlementIssue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("mapping_epoch", models.CharField(max_length=64)),
                ("adapter_device_id", models.PositiveBigIntegerField()),
                ("store_incarnation", models.CharField(max_length=64)),
                ("job_id", models.PositiveBigIntegerField()),
                ("settle_seq", models.PositiveBigIntegerField()),
                ("request_fence", models.JSONField()),
                ("stall_class", models.CharField(max_length=32)),
                ("stall_reason", models.CharField(max_length=128)),
                ("position", models.CharField(max_length=16)),
                ("cursor_before", models.PositiveBigIntegerField()),
                ("cursor_after", models.PositiveBigIntegerField()),
                ("evidence_digest", models.CharField(max_length=64)),
                ("impact_digest", models.CharField(max_length=64)),
                ("contract_fingerprint", models.CharField(max_length=64)),
                ("revision_digest", models.CharField(max_length=64)),
                ("proposal", models.JSONField()),
                ("state", models.CharField(default="open", max_length=16)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("actor_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("actor_label", models.CharField(blank=True, default="", max_length=150)),
                ("acknowledgement_reason", models.TextField(blank=True, default="")),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("committed_effects", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
            ],
        ),
        migrations.CreateModel(
            name="NSOSettlementIssueOwner",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("model_label", models.CharField(max_length=100)),
                ("overlay_pk", models.PositiveBigIntegerField()),
                ("snapshot_ids", models.JSONField(default=list)),
                ("attempt_id", models.UUIDField(blank=True, null=True)),
                ("selection_ids", models.JSONField(default=list)),
                ("contributors", models.JSONField(default=list)),
                ("unknown", models.JSONField(default=list)),
                ("covered", models.JSONField(default=list)),
                ("authority", models.JSONField()),
                ("order_bound", models.PositiveBigIntegerField()),
                ("proposed_effect", models.CharField(max_length=16)),
                ("committed_effect", models.CharField(blank=True, max_length=16, null=True)),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="nsoapplyexecution",
            name="nso_execution_consumed_terminal",
        ),
        migrations.AddField(
            model_name="nsosettlementissue",
            name="management",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="settlement_issues",
                to="netbox_nso_plugin.nsodevicemanagement",
            ),
        ),
        migrations.AddField(
            model_name="nsoapplyexecution",
            name="settlement_issue",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="acknowledged_executions",
                to="netbox_nso_plugin.nsosettlementissue",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsoapplyexecution",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("consumption_state__in", ("unconsumed", "unresolved", "acknowledged")),
                    models.Q(("consumed_at__isnull", False), ("terminal_projection__isnull", False)),
                    _connector="OR",
                ),
                name="nso_execution_consumed_terminal",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsoapplyexecution",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("carrier_job_id__isnull", False),
                        ("consumed_at__isnull", True),
                        ("consumption_state", "acknowledged"),
                        ("generation_id__isnull", False),
                        ("settle_seq__gt", 0),
                        ("settle_seq__isnull", False),
                        ("settlement_issue__isnull", False),
                        ("store_incarnation__isnull", False),
                        models.Q(
                            ("terminal_projection__isnull", False),
                            ("commit_acknowledged__isnull", True),
                            _connector="OR",
                        ),
                    ),
                    models.Q(
                        models.Q(("consumption_state", "acknowledged"), _negated=True),
                        ("settlement_issue__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="nso_execution_acknowledged",
            ),
        ),
        migrations.AddField(
            model_name="nsosettlementissueowner",
            name="issue",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="owners",
                to="netbox_nso_plugin.nsosettlementissue",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissue",
            constraint=models.UniqueConstraint(
                fields=(
                    "management",
                    "mapping_epoch",
                    "adapter_device_id",
                    "store_incarnation",
                    "job_id",
                    "revision_digest",
                ),
                name="nso_issue_revision",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissue",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "open")), fields=("management",), name="nso_issue_open"
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissue",
            constraint=models.CheckConstraint(
                condition=models.Q(("state__in", ("open", "resolved", "superseded", "acknowledged"))),
                name="nso_issue_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissue",
            constraint=models.CheckConstraint(
                condition=models.Q(("stall_class__in", ("invalid_evidence", "evidence_invariant"))),
                name="nso_issue_stall_class",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissue",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("cursor_after", models.F("settle_seq")),
                        ("position", "next_feed"),
                        ("settle_seq__gt", models.F("cursor_before")),
                    ),
                    models.Q(
                        ("cursor_after", models.F("cursor_before")),
                        ("position", "historical"),
                        ("settle_seq__lte", models.F("cursor_before")),
                    ),
                    _connector="OR",
                ),
                name="nso_issue_cursor_rule",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissue",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("contract_fingerprint__regex", "^[0-9a-f]{64}$"),
                    ("evidence_digest__regex", "^[0-9a-f]{64}$"),
                    ("impact_digest__regex", "^[0-9a-f]{64}$"),
                    ("job_id__gt", 0),
                    ("revision_digest__regex", "^[0-9a-f]{64}$"),
                    ("settle_seq__gt", 0),
                    models.Q(("store_incarnation", ""), _negated=True),
                    models.Q(("stall_reason", ""), _negated=True),
                ),
                name="nso_issue_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissue",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        models.Q(("closed_at__isnull", True), ("state", "open")),
                        models.Q(models.Q(("state", "open"), _negated=True), ("closed_at__isnull", False)),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(
                            ("acknowledged_at__isnull", False),
                            ("committed_effects__isnull", False),
                            ("state", "acknowledged"),
                            ("actor_id__isnull", False),
                            models.Q(("actor_label", ""), _negated=True),
                            models.Q(("acknowledgement_reason", ""), _negated=True),
                        ),
                        models.Q(
                            models.Q(("state", "acknowledged"), _negated=True),
                            ("acknowledged_at__isnull", True),
                            ("acknowledgement_reason", ""),
                            ("actor_id__isnull", True),
                            ("actor_label", ""),
                            ("committed_effects__isnull", True),
                        ),
                        _connector="OR",
                    ),
                ),
                name="nso_issue_audit",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissueowner",
            constraint=models.UniqueConstraint(fields=("issue", "model_label", "overlay_pk"), name="nso_issue_owner"),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissueowner",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "model_label__in",
                        (
                            "netbox_nso_plugin.nsointerfacestate",
                            "netbox_nso_plugin.nsointerfaceipstate",
                            "netbox_nso_plugin.nsosnmpcommunitystate",
                            "netbox_nso_plugin.nsosnmpv3userstate",
                            "netbox_nso_plugin.nsosnmphoststate",
                            "netbox_nso_plugin.nsosnmpsysteminfostate",
                            "netbox_nso_plugin.nsologginghoststate",
                            "netbox_nso_plugin.nsologginglevelstate",
                            "netbox_nso_plugin.nsosvistate",
                            "netbox_nso_plugin.nsosubinterfacestate",
                            "netbox_nso_plugin.nsointerfacemtustate",
                            "netbox_nso_plugin.nsovlanstate",
                            "netbox_nso_plugin.nsobfdinterfacestate",
                            "netbox_nso_plugin.nsostaticroutestate",
                            "netbox_nso_plugin.nsol2sapstate",
                            "netbox_nso_plugin.nsoisisinstancestate",
                            "netbox_nso_plugin.nsoisisinterfacestate",
                            "netbox_nso_plugin.nsoisisflexalgostate",
                            "netbox_nso_plugin.nsobgprouterstate",
                            "netbox_nso_plugin.nsobgppeerstate",
                            "netbox_nso_plugin.nsoredistributionstate",
                            "netbox_nso_plugin.nsoospfinstancestate",
                            "netbox_nso_plugin.nsoospfinterfacestate",
                            "netbox_nso_plugin.nsoroutepolicystate",
                            "netbox_nso_plugin.nsolacpbundlestate",
                            "netbox_nso_plugin.nsolacpmemberstate",
                            "netbox_nso_plugin.nsoswitchportstate",
                        ),
                    )
                ),
                name="nso_issue_owner_model",
            ),
        ),
        migrations.AddConstraint(
            model_name="nsosettlementissueowner",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("proposed_effect__in", ("unavailable", "superseded", "missing_owner")),
                    models.Q(
                        ("committed_effect__isnull", True),
                        ("committed_effect", models.F("proposed_effect")),
                        _connector="OR",
                    ),
                ),
                name="nso_issue_owner_effect",
            ),
        ),
        migrations.RunSQL(sql=_FREEZE_SQL, reverse_sql=_THAW_SQL),
    ]
