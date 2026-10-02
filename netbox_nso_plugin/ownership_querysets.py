# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep set-based ORM writes inside the ownership acquisition protocol."""

import copy

from django.db import transaction
from utilities.querysets import RestrictedQuerySet


class OwnershipQuerySet(RestrictedQuerySet):
    def update(self, **kwargs):
        from .intent_state import IntentMutationProtocolError
        from .ownership_grants import validate_acquisition
        from .renderer_writer import active_renderer_writer

        if "status" not in kwargs:
            return super().update(**kwargs)
        if not isinstance(kwargs["status"], str):
            raise IntentMutationProtocolError(f"{self.model._meta.label_lower} status requires an exact literal value")
        with transaction.atomic(using=self.db):
            rows = tuple(self.select_for_update(of=("self",)).order_by("pk"))
            if not rows:
                return 0
            writer = active_renderer_writer()
            if writer is not None and not writer.queryset_update_is_authorized(self, kwargs, rows):
                raise IntentMutationProtocolError("set-based acquisition bypassed the active renderer writer")
            for before in rows:
                after = copy.copy(before)
                after.status = kwargs["status"]
                validate_acquisition(before, after, writer.grant if writer is not None else None)
            selected = self.filter(pk__in=tuple(row.pk for row in rows))
            changed = super(OwnershipQuerySet, selected).update(**kwargs)
            from .models import NSOOwnershipAcquisition
            from .status_machine import is_owned

            if not is_owned(kwargs["status"]):
                NSOOwnershipAcquisition.objects.using(self.db).filter(
                    state_model_label=self.model._meta.label_lower,
                    state_id__in=tuple(row.pk for row in rows),
                ).delete()
            return changed

    def bulk_create(self, objs, **kwargs):
        from .ownership_grants import validate_acquisition

        objs = tuple(objs)
        for obj in objs:
            validate_acquisition(None, obj, None)
        return super().bulk_create(objs, **kwargs)
