# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Show Intend selections in operator terms."""

from django import template
from django.apps import apps
from django.utils.text import capfirst

register = template.Library()


@register.filter
def pairs(mapping):
    """Show a state key or option mapping as name=value pairs, or none."""
    return ", ".join(f"{name}={value}" for name, value in mapping.items()) or "none"


@register.filter
def model_name(label):
    """Show an app_label.model label as the model's verbose name."""
    return capfirst(apps.get_model(label)._meta.verbose_name)
