# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Positive and negative examples for the custom review checks."""

import contextlib
import re
import django.db.models.signals
from unittest.mock import patch
from threading import BrokenBarrierError
from django.db.models import signals as model_signals
from django.db.models.signals import post_delete as post_delete_signal
from django.db.models.signals import post_save, pre_save
from ipam.models import VLANGroup
from netbox_nso_plugin.signals import suppress_intent_push, _schedule_intent_push
from netbox_nso_plugin import signals


def vlan_identity_from_name(candidate, native, interface):
    # ruleid: nso-vlan-identity-from-interface-name
    candidate.dot1q_vlan = int(native.name.rsplit(".", 1)[-1])
    # ruleid: nso-vlan-identity-from-interface-name
    dot1q_vlan = int(native.name.rsplit(".", 1)[-1])
    suffix = native.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix) if suffix.isdigit() else None
    tail = interface.name.partition(".")[2]
    # ruleid: nso-vlan-identity-flow
    vid = int(tail)
    # ruleid: nso-vlan-identity-from-interface-name
    vid = int(interface.name.rsplit(".", 1)[-1])
    # ruleid: nso-vlan-identity-from-interface-name
    vid = int(interface.name.split(".")[-1])
    # ruleid: nso-vlan-identity-from-interface-name
    vlan = int(interface.name.rsplit(".", 1)[-1])
    vid_tail = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    vid = int(vid_tail)
    vid_part = interface.name.split(".")[1]
    # ruleid: nso-vlan-identity-flow
    vid = int(vid_part)
    vid_stripped = interface.name.removeprefix("irb.")
    # ruleid: nso-vlan-identity-flow
    vid = int(vid_stripped)
    vid_field = interface.cf["vlan"]
    # ok: nso-vlan-identity-from-interface-name
    vid = int(vid_field)
    match = re.match(r".*\.(\d+)$", interface.name)
    # ruleid: nso-vlan-identity-flow
    vlan = int(match.group(1))
    # ruleid: nso-vlan-identity-flow
    vid = int(match.group(1))
    # ok: nso-vlan-identity-from-interface-name
    unit = int(interface.name.rsplit(".", 1)[-1])
    # ruleid: nso-vlan-identity-from-interface-name
    NSOSubinterfaceState(dot1q_vlan=int(native.name.rsplit(".", 1)[-1]))
    # ruleid: nso-vlan-identity-from-interface-name
    payload = {"unit": 7, "dot1q_vlan": int(native.name.split(".")[-1])}
    suffix = native.name.removeprefix("ae99.")
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    # ruleid: nso-vlan-identity-flow
    dot1q_vlan = int(suffix)
    # ruleid: nso-vlan-identity-flow
    NSOSubinterfaceState(dot1q_vlan=int(suffix))
    partitioned = native.name.partition(".")[2]
    # ruleid: nso-vlan-identity-flow
    payload = {"dot1q_vlan": int(partitioned)}
    matched = re.match(r".*\.(\d+)$", native.name)
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(matched.group(1))
    # ruleid: nso-vlan-identity-flow
    NSOSubinterfaceState(dot1q_vlan=int(matched.group(1)))
    # ruleid: nso-vlan-identity-flow
    payload = {"dot1q_vlan": int(matched.group(1))}
    searched = re.search(r"\.(\d+)$", native.name)
    # ruleid: nso-vlan-identity-flow
    NSOSubinterfaceState(dot1q_vlan=int(searched.group(1)), status="accepted")
    # ok: nso-vlan-identity-from-interface-name
    NSOSubinterfaceState(unit=int(native.name.rsplit(".", 1)[-1]), dot1q_vlan=100)
    # ok: nso-vlan-identity-from-interface-name
    payload = {"unit": int(native.name.split(".")[-1]), "dot1q_vlan": 100}
    # ruleid: nso-vlan-identity-from-interface-name
    vid = int(interface.name[4:])
    # ruleid: nso-vlan-identity-from-interface-name
    vlan = int(interface.name[4:])
    name = interface.name.lower()
    # ruleid: nso-vlan-identity-flow
    return "svi", int(name[4:])
    return candidate, vid, vlan, unit


def irb_vlan_identity_from_name(interface):
    name = interface.name.lower()
    # ruleid: nso-vlan-identity-flow
    return "irb", int(name[4:])


def native_svi_definition_from_name(interface):
    name = (interface.name or "").lower()
    if name.startswith("vlan") and name[4:].isdigit():
        # ruleid: nso-vlan-identity-flow
        return "svi", int(name[4:])
    if name.startswith("irb.") and name[4:].isdigit():
        # ruleid: nso-vlan-identity-flow
        return "irb", int(name[4:])
    return None


def svi_vlan_after_placeholder(interface):
    name = ""
    name = interface.name.lower()
    # ruleid: nso-vlan-identity-flow
    return "svi", int(name[4:])


def vlan_suffix_transformed_in_branch(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = suffix.strip()
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_augmented_from_name(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    suffix += "0"
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_augmented_from_explicit_field(candidate, interface):
    suffix = interface.cf["vlan"]
    suffix += "0"
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_augmented_with_name(candidate, interface):
    suffix = "1"
    suffix += interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_augmented_repeatedly(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    suffix *= 2
    suffix += "0"
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_through_walrus(candidate, interface):
    suffix = interface.name.split(".")[-1]
    if suffix := suffix.strip():
        # ruleid: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


def vlan_explicit_suffix_through_walrus(candidate, interface):
    if suffix := interface.cf["vlan"]:
        # ok: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


def vlan_suffix_retained_after_short_circuit_walrus(interface, flag):
    suffix = interface.name.split(".")[-1]
    flag and (suffix := interface.cf["vlan"])
    # ruleid: nso-vlan-identity-flow
    vid = int(suffix)


def vlan_suffix_retained_after_conditional_walrus(interface, flag):
    suffix = interface.name.split(".")[-1]
    (suffix := interface.cf["vlan"]) if flag else None
    # ruleid: nso-vlan-identity-flow
    vid = int(suffix)


def vlan_suffix_retained_after_empty_comprehension(interface):
    suffix = interface.name.split(".")[-1]
    [(suffix := interface.cf["vlan"]) for _ in []]
    # ruleid: nso-vlan-identity-flow
    vid = int(suffix)


def vlan_suffix_from_reused_match_capture(candidate, interface):
    suffix = interface.name.split(".")[-1]
    match suffix:
        case suffix:
            # ruleid: nso-vlan-identity-flow
            candidate.dot1q_vlan = int(suffix)


def vlan_suffix_retained_after_empty_loop(candidate, interface):
    suffix = interface.name.split(".")[-1]
    for suffix in []:
        pass
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_retained_after_context_capture(candidate, interface):
    suffix = interface.name.split(".")[-1]
    with contextlib.nullcontext(interface.cf["vlan"]) as suffix:
        pass
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_retained_after_exception_capture(candidate, interface):
    suffix = interface.name.split(".")[-1]
    try:
        int(interface.cf["vlan"])
    except ValueError as suffix:
        pass
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_retained_after_import(candidate, interface):
    suffix = interface.name.split(".")[-1]
    from placeholder import suffix

    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_retained_after_try_overwrite(candidate, interface):
    suffix = interface.name.split(".")[-1]
    try:
        suffix = interface.cf["vlan"]
    except KeyError:
        pass
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_carried_from_later_loop_binding(candidate, interface, flags):
    suffix = interface.cf["vlan"]
    for flag in flags:
        # ruleid: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)
        suffix = interface.name.split(".")[-1]


def vlan_suffix_overwritten_before_nested_loop_use(candidate, interface, flags):
    suffix = interface.name.split(".")[-1]
    for flag in flags:
        suffix = interface.cf["vlan"]
        if flag:
            # ok: nso-vlan-identity-flow
            candidate.dot1q_vlan = int(suffix)


def vlan_loop_target_overwritten_before_use(candidate, interface):
    for suffix in interface.name.split("."):
        suffix = interface.cf["vlan"]
        # ok: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


def vlan_match_capture_overwritten_before_use(candidate, interface):
    match interface.name.split(".")[-1]:
        case suffix:
            suffix = interface.cf["vlan"]
            # ok: nso-vlan-identity-flow
            candidate.dot1q_vlan = int(suffix)


def vlan_suffix_from_loop_target(candidate, interface):
    for suffix in [interface.name.rsplit(".", 1)[-1]]:
        # ruleid: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


async def vlan_suffix_from_async_loop_target(candidate, interface):
    async for suffix in interface.name.rsplit(".", 1):
        # ruleid: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


def vlan_suffix_from_context_target(candidate, interface):
    with contextlib.nullcontext(interface.name.split(".")[-1]) as suffix:
        # ruleid: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


def vlan_explicit_suffix_from_context_target(candidate, interface):
    with contextlib.nullcontext(interface.name.split(".")[-1]) as other:
        with contextlib.nullcontext(interface.cf["vlan"]) as suffix:
            # ok: nso-vlan-identity-flow
            candidate.dot1q_vlan = int(suffix)


def vlan_suffix_from_match_capture(candidate, interface):
    match interface.name.split(".")[-1]:
        case suffix:
            # ruleid: nso-vlan-identity-flow
            candidate.dot1q_vlan = int(suffix)


def vlan_suffix_from_parameter(candidate, suffix=interface.name.split(".")[-1]):
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_from_exception_name(candidate, interface):
    try:
        int(interface.name.split(".")[-1])
    except ValueError as suffix:
        # ok: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


def vlan_suffix_from_import_alias(candidate):
    from placeholder import suffix

    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_through_tuple(candidate, interface, flag):
    suffix = interface.name.split(".")[-1]
    if flag:
        suffix, other = interface.cf["vlan"], "x"
    else:
        suffix, other = interface.cf["vlan"], "x"
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_through_list(candidate, interface, flag):
    suffix = interface.name.split(".")[-1]
    if flag:
        [suffix, *other] = [interface.cf["vlan"], "x"]
    else:
        [suffix, *other] = [interface.cf["vlan"], "x"]
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_in_both_branches(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = interface.cf["vlan"]
    else:
        suffix = interface.cf["vlan"]
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_in_all_branches(candidate, interface, flag, other):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = interface.cf["vlan"]
    elif other:
        suffix = interface.cf["vlan"]
    else:
        suffix = interface.cf["vlan"]
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_rederived_in_one_branch(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = interface.cf["vlan"]
    else:
        suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_without_else(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_transformed_in_both_branches(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = suffix.strip()
    else:
        suffix = interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_augmented_in_both_branches(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix += "0"
    else:
        suffix += "1"
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_in_nested_branches(candidate, interface, flag, other):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        if other:
            suffix = interface.cf["vlan"]
        else:
            suffix = interface.cf["vlan"]
    else:
        suffix: str = interface.cf["vlan"]
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_in_incomplete_nested_branch(candidate, interface, flag, other):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        if other:
            suffix = interface.cf["vlan"]
    else:
        suffix = interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_annotated_in_one_branch(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix: str
    else:
        suffix = interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_in_loop_branches(candidate, interface, flags):
    suffix = interface.name.rsplit(".", 1)[-1]
    for flag in flags:
        if flag:
            suffix = interface.cf["vlan"]
        else:
            suffix = interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_overwritten_or_returned(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = interface.cf["vlan"]
    else:
        return
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_used_before_conditional_overwrite(candidate, interface, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        # ruleid: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)
        suffix = interface.cf["vlan"]
    else:
        suffix = interface.cf["vlan"]
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_used_in_loop(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    for _ in range(1):
        # ruleid: nso-vlan-identity-flow
        candidate.dot1q_vlan = int(suffix)


def irb_vlan_after_strip(interface):
    name = interface.name.lower()
    name = name.strip()
    # ruleid: nso-vlan-identity-flow
    return "irb", int(name[4:])


def vlan_suffix_rederived_from_name(candidate, native):
    suffix = native.name.rsplit(".", 1)[-1]
    suffix = native.name.removeprefix("ae99.")
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_through_expressions(candidate, interface, values):
    suffix = interface.name.rsplit(".", 1)[-1]
    suffix = suffix + "0"
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = suffix * 2
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = f"{suffix}0"
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = str(suffix)
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = "{}".format(suffix)
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = values[suffix]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = suffix == "100"
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = "100" if suffix else "200"
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_suffix_concatenated_from_clean_value(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    other = interface.cf["vlan"]
    suffix = other + "0"
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_tuple_suffix_from_explicit_field(interface):
    tail, suffix = interface.name.rsplit(".", 1)[-1], interface.cf["vlan"]
    # ok: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_tuple_suffix_from_name(interface):
    suffix, tail = interface.name.rsplit(".", 1)[-1], interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_list_suffix_from_explicit_field(interface):
    [tail, suffix] = [interface.name.rsplit(".", 1)[-1], interface.cf["vlan"]]
    # ok: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_list_suffix_from_name(interface):
    [suffix, tail] = [interface.name.rsplit(".", 1)[-1], interface.cf["vlan"]]
    # ruleid: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_suffix_from_nonliteral_unpacking(interface):
    values = interface.name.rsplit(".", 1)[-1], interface.cf["vlan"]
    tail, suffix = values
    # ruleid: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_suffix_from_starred_target(interface):
    *tail, suffix = interface.name.rsplit(".", 1)[-1], interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_suffix_from_starred_value(interface):
    tail, suffix = (*interface.name.rsplit(".", 1), interface.cf["vlan"])
    # ruleid: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_suffix_from_mismatched_unpacking(interface):
    tail, suffix = interface.name.rsplit(".", 1)[-1], interface.cf["vlan"], "100"
    # ruleid: nso-vlan-identity-flow
    return dict(dot1q_vlan=int(suffix))


def vlan_suffix_from_comprehension_unpacking(interface):
    # ruleid: nso-vlan-identity-flow
    return [dict(dot1q_vlan=int(suffix)) for tail, suffix in [(interface.name.rsplit(".", 1)[-1], interface.cf["vlan"])]]


def vlan_suffix_in_comprehensions(interface, candidates):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in candidates]


def vlan_clean_suffix_in_comprehension(interface, candidates):
    suffix = interface.cf["vlan"]
    # ok: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in candidates]


def vlan_suffix_in_generator(interface, candidates):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    return (NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in candidates)


def vlan_suffix_in_lambda(interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    return lambda: NSOSubinterfaceState(dot1q_vlan=int(suffix))


def vlan_lambda_argument_shadows_suffix(interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ok: nso-vlan-identity-flow
    return lambda suffix: NSOSubinterfaceState(dot1q_vlan=int(suffix))


def vlan_comprehension_target_from_name(interface):
    suffixes = interface.name.rsplit(".", 1)
    # ruleid: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for suffix in suffixes]


def vlan_comprehension_target_shadows_suffix(interface, candidates):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ok: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for suffix in candidates]


def vlan_comprehension_target_reuses_suffix(interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for suffix in [suffix]]


def vlan_comprehension_target_overwritten_by_clean_iterable(interface, candidates):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ok: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(value)) for value in [suffix] for value in candidates]


def vlan_comprehension_filter_uses_current_target(interface, candidates):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    return [value for value in [suffix] if NSOSubinterfaceState(dot1q_vlan=int(value)) for value in candidates]


def vlan_comprehension_unpacking_from_name(interface):
    suffixes = [interface.name.rsplit(".", 1)]
    # ruleid: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for _, suffix in suffixes]


def vlan_suffix_in_nested_comprehension(interface, candidates):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    return [[NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in group] for group in candidates]


def vlan_comprehension_uses_current_suffix(interface, candidates):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    states = [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in candidates]
    suffix = interface.cf["vlan"]
    return states


def vlan_comprehension_precedes_name_source(interface, candidates):
    suffix = interface.cf["vlan"]
    # ok: nso-vlan-identity-flow
    states = [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in candidates]
    suffix = interface.name.rsplit(".", 1)[-1]
    return states


def vlan_comprehension_follows_complete_overwrite(interface, candidates, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = interface.cf["vlan"]
    else:
        suffix = "100"
    # ok: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in candidates]


def vlan_comprehension_follows_incomplete_overwrite(interface, candidates, flag):
    suffix = interface.name.rsplit(".", 1)[-1]
    if flag:
        suffix = interface.cf["vlan"]
    # ruleid: nso-vlan-identity-flow
    return [NSOSubinterfaceState(dot1q_vlan=int(suffix)) for candidate in candidates]


def vlan_suffix_from_explicit_field(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    suffix = interface.cf["vlan"]
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    dot1q_vlan = int(suffix)
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix) if suffix.isdigit() else None
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    vid = int(suffix)
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    NSOSubinterfaceState(dot1q_vlan=int(suffix))
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    payload = {"dot1q_vlan": int(suffix)}


def stripped_vlan_suffix_from_explicit_field(candidate, interface):
    suffix = interface.name.removeprefix("irb.")
    suffix = interface.cf["vlan"]
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_match_from_explicit_field(candidate, interface):
    match = re.match(r".*\.(\d+)$", interface.name)
    match = re.match(r"(\d+)$", interface.cf["vlan"])
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    vlan = int(match.group(1))
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    vid = int(match.group(1))
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    candidate.dot1q_vlan = int(match.group(1))
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    NSOSubinterfaceState(dot1q_vlan=int(match.group(1)))
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    payload = {"dot1q_vlan": int(match.group(1))}


def vlan_search_from_explicit_field(candidate, interface):
    match = re.search(r"\.(\d+)$", interface.name)
    match = re.search(r"(\d+)$", interface.cf["vlan"])
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    candidate.dot1q_vlan = int(match.group(1))


def vlan_fullmatch_from_explicit_field(candidate, interface):
    match = re.fullmatch(r".*\.(\d+)$", interface.name)
    match = re.fullmatch(r"(\d+)$", interface.cf["vlan"])
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    candidate.dot1q_vlan = int(match.group(1))


def svi_vlan_from_explicit_field(interface):
    name = interface.name.lower()
    name = interface.cf["vlan"]
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    return "svi", int(name[4:])


def irb_vlan_from_explicit_field(interface):
    name = (interface.name or "").lower()
    name = interface.cf["vlan"]
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    return "irb", int(name[4:])


def svi_vlan_reassigned_to_literal(interface):
    name = interface.name.lower()
    name = "vlan100"
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    return "svi", int(name[4:])


def annotated_vlan_flow_sources(candidate, interface):
    suffix: str = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    match: object = re.match(r".*\.(\d+)$", interface.name)
    # ruleid: nso-vlan-identity-flow
    vid = int(match.group(1))
    name: str = interface.name.lower()
    # ruleid: nso-vlan-identity-flow
    return "svi", int(name[4:])


def annotated_vlan_flow_targets(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    vid: int = int(suffix)
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan: int = int(suffix)
    suffix: str = interface.cf["vlan"]
    # ok: nso-vlan-identity-from-interface-name, nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_flow_through_type_declaration(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    suffix: str
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)
    suffix = interface.cf["vlan"]
    suffix: str
    # ok: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(suffix)


def vlan_flow_target_limits(candidate, interface):
    suffix = interface.name.rsplit(".", 1)[-1]
    # ruleid: nso-vlan-identity-flow
    vlan = int(suffix)
    # ok: nso-vlan-identity-flow
    unit = int(suffix)
    # ok: nso-vlan-identity-flow
    NSOSubinterfaceState(unit=int(suffix), dot1q_vlan=100)
    # ok: nso-vlan-identity-flow
    payload = {"unit": int(suffix), "dot1q_vlan": 100}
    match = re.match(r".*\.(\d+)$", interface.name)
    # ruleid: nso-vlan-identity-flow
    dot1q_vlan = int(match.group(1))
    # ruleid: nso-vlan-identity-flow
    candidate.dot1q_vlan = int(match.group(1)) if match else None
    name = interface.name.lower()
    # ok: nso-vlan-identity-flow
    vid = int(name)
    # ok: nso-vlan-identity-flow
    return "svi", int(name[3:])


def vlan_slice_targets(candidate, native):
    # ruleid: nso-vlan-identity-from-interface-name
    candidate.dot1q_vlan = int(native.name[4:])
    # ruleid: nso-vlan-identity-from-interface-name
    dot1q_vlan = int(native.name[4:])
    # ruleid: nso-vlan-identity-from-interface-name
    NSOSubinterfaceState(dot1q_vlan=int(native.name[4:]))
    # ruleid: nso-vlan-identity-from-interface-name
    payload = {"dot1q_vlan": int(native.name[4:])}
    # ok: nso-vlan-identity-from-interface-name
    unit = int(native.name[4:])
    # ok: nso-vlan-identity-from-interface-name
    NSOSubinterfaceState(unit=int(native.name[4:]), dot1q_vlan=100)
    # ok: nso-vlan-identity-from-interface-name
    payload = {"unit": int(native.name[4:]), "dot1q_vlan": 100}


def switching_client_ownership(client, device_id, roots):
    # ruleid: nso-switching-client-single-owner
    client.apply_lag_config(device_id, [], deleted_roots=roots, source_revision=1)
    # ruleid: nso-switching-client-single-owner
    client.apply_switchport_config(device_id, [], deleted_roots=roots, source_revision=1)
    # ruleid: nso-switching-client-single-owner
    apply_lag_config(device_id, [], deleted_roots=roots, source_revision=1)
    # ruleid: nso-switching-client-single-owner
    apply_switchport_config(device_id, [], deleted_roots=roots, source_revision=1)
    # ok: nso-switching-client-single-owner
    client.trigger_apply(device_id, "attempt", {})

# ruleid: nso-retired-push-builder
from netbox_nso_plugin.delivery import _push_vlan_intent_for_device
# ruleid: nso-retired-push-builder
from netbox_nso_plugin.delivery import _push_interface_intent_for_device as push_interface
# ruleid: nso-retired-push-builder
import delivery._push_static_route_intent_for_device
# ruleid: nso-retired-push-builder
import _push_isis_intent_for_device
# ruleid: nso-retired-push-builder
import _push_bgp_intent_for_device as push_bgp
# ruleid: nso-retired-push-builder
from _push_vlan_intent_for_device import *

# ruleid: nso-retired-coalescer-state
from netbox_nso_plugin.signals import _pending_pushes
# ruleid: nso-retired-coalescer-state
from netbox_nso_plugin.signals import _last_pushed_hashes as pushed_hashes
# ruleid: nso-retired-coalescer-state
import signals._pending_pushes
# ruleid: nso-retired-coalescer-state
import _pending_pushes
# ruleid: nso-retired-coalescer-state
import _last_pushed_hashes as last_pushed_hashes
# ruleid: nso-retired-coalescer-state
from _pending_pushes import *

# ruleid: nso-retired-renderer-writer-symbol
from netbox_nso_plugin.signals import _IMPLICIT_PERMITS
# ruleid: nso-retired-renderer-writer-symbol
from netbox_nso_plugin.signals import _authorize_dml as authorize_dml
# ruleid: nso-retired-renderer-writer-symbol
import renderer_writer._begin_delete_implicit
# ruleid: nso-retired-renderer-writer-symbol
import _begin_implicit
# ruleid: nso-retired-renderer-writer-symbol
import _begin_m2m_implicit as begin_m2m_implicit
# ruleid: nso-retired-renderer-writer-symbol
from _install_guard import *

# ruleid: nso-retired-push-builder
_push_vlan_intent_for_device: object
# ruleid: nso-retired-coalescer-state
_pending_pushes: dict
# ruleid: nso-retired-renderer-writer-symbol
_parse_dml_target: object


# ruleid: nso-retired-renderer-writer-symbol
def _create_greenfield_subif_state():
    return None


# ruleid: nso-retired-renderer-writer-symbol
async def _transition_static_route_content():
    return None


def suppressed_push(device):
    with suppress_intent_push():
        # ruleid: nso-push-inside-suppression
        _schedule_intent_push(device, "ip")
    with suppress_intent_push(), patch("netbox_nso_plugin.signals.logger"):
        # ruleid: nso-push-inside-suppression
        _schedule_intent_push(device, "ip")
    with signals.suppress_intent_push():
        # ruleid: nso-push-inside-suppression
        signals._schedule_intent_push(device, "ip")
    with signals.suppress_intent_push(), patch("netbox_nso_plugin.signals.logger"):
        # ruleid: nso-push-inside-suppression
        _schedule_intent_push(device, "ip")
    # ok: nso-push-inside-suppression
    signals._schedule_intent_push(device, "ip")
    # ok: nso-push-inside-suppression
    _schedule_intent_push(device, "ip")


def tautological_assertions(self, value, read):
    # ruleid: nso-tautological-assertion
    self.assertEqual(value, value)
    # ruleid: nso-tautological-assertion
    assert value == value
    # ok: nso-tautological-assertion
    self.assertEqual(value, "accepted")
    # ok: nso-tautological-assertion
    assert value == "accepted"
    # ok: nso-tautological-assertion
    self.assertEqual(read(), read())


class ResetMixinWithoutChain(IntentPushResetMixin, TransactionTestCase):
    # ruleid: nso-intent-push-reset-setup-chain
    def setUp(self):
        self.device = make_device()


class ResetMixinWithChain(IntentPushResetMixin, TransactionTestCase):
    # ok: nso-intent-push-reset-setup-chain
    def setUp(self):
        super().setUp()
        self.device = make_device()


class PlainCaseWithoutChain(TransactionTestCase):
    # ok: nso-intent-push-reset-setup-chain
    def setUp(self):
        self.device = make_device()


def patch_clock():
    # ruleid: nso-global-monotonic-patch
    patch("time.monotonic", return_value=0)
    # ruleid: nso-global-monotonic-patch
    patch("netbox_nso_plugin.renderer_audit.time.monotonic", return_value=0)
    # ok: nso-global-monotonic-patch
    patch("netbox_nso_plugin.renderer_audit._monotonic", return_value=0)


def swallowed_barrier(barrier):
    # ruleid: nso-swallowed-barrier-failure
    try:
        barrier.wait(timeout=2)
    except BrokenBarrierError:
        pass
    # ruleid: nso-swallowed-barrier-failure
    try:
        barrier.wait(timeout=2)
    except BrokenBarrierError:
        return


def unbounded_thread_join_shapes(worker, options, timeout):
    # ruleid: nso-unbounded-thread-join
    worker.join()
    # ruleid: nso-unbounded-thread-join
    worker.join(None)
    # ruleid: nso-unbounded-thread-join
    worker.join((None))
    # ruleid: nso-unbounded-thread-join
    worker.join(timeout=None)
    # ruleid: nso-unbounded-thread-join
    worker.join(**options)
    # ruleid: nso-unbounded-thread-join
    worker.join(timeout=None, **options)
    # ruleid: nso-unbounded-thread-join
    worker.join(None, **options)
    # ruleid: nso-unbounded-thread-join
    worker.join(None, other=timeout)
    # ruleid: nso-unbounded-thread-join
    worker.join(5, timeout=None)
    # ruleid: nso-unbounded-thread-join
    worker.join(other=timeout)
    # ok: nso-unbounded-thread-join
    worker.join(5)
    # ok: nso-unbounded-thread-join
    worker.join(timeout=5)
    # ok: nso-unbounded-thread-join
    worker.join(timeout=timeout)
    # ok: nso-unbounded-thread-join
    worker.join(timeout=timeout, **options)
    # ruleid: nso-unbounded-thread-join
    worker.join(*options)
    # ok: nso-unbounded-thread-join
    worker.join(*options, timeout=5)
    # ok: nso-unbounded-thread-join
    ",".join(items)


def unpacked_thread_join_shapes(worker, a, b, x):
    # ruleid: nso-unbounded-thread-join
    worker.join(**a, **b)
    # ruleid: nso-unbounded-thread-join
    worker.join(**a, other=x)
    # ok: nso-unbounded-thread-join
    worker.join(**a, timeout=5)


def retired_push_builder_shapes(module):
    # ruleid: nso-push-builder-definition-outside-signals
    def _push_vlan_intent_for_device():
        pass

    # ruleid: nso-retired-push-builder
    _push_vlan_intent_for_device()
    # ruleid: nso-retired-push-builder
    module._push_interface_intent_for_device()
    # ruleid: nso-retired-push-builder
    builder = _push_ospf_intent_for_device
    # ruleid: nso-retired-push-builder
    builder = module._push_bgp_intent_for_device
    # ok: nso-retired-push-builder
    builder = _push_intent_registry
    return builder


def retired_coalescer_state_shapes(module):
    # ruleid: nso-retired-coalescer-state
    _pending_pushes()
    # ruleid: nso-retired-coalescer-state
    module._last_pushed_hashes()
    # ruleid: nso-retired-coalescer-state
    state = _last_pushed_hashes
    # ruleid: nso-retired-coalescer-state
    state = module._pending_pushes
    # ok: nso-retired-coalescer-state
    state = pending_pushes
    return state


def retired_renderer_writer_symbol_shapes(module, factory):
    # ruleid: nso-retired-renderer-writer-symbol
    _discard_rolled_back_implicit_permit()
    # ruleid: nso-retired-renderer-writer-symbol
    module._dml_guard()
    # ruleid: nso-retired-renderer-writer-symbol
    _end_implicit = factory
    # ruleid: nso-retired-renderer-writer-symbol
    callback = _end_m2m_implicit
    # ruleid: nso-retired-renderer-writer-symbol
    callback = module._on_routing_static_route_pre_save
    # ok: nso-retired-renderer-writer-symbol
    callback = module._on_static_route_pre_save
    return callback


def retired_interface_config_literal():
    # ruleid: nso-retired-interface-config-literal
    double_quoted = "interface_config"
    # ruleid: nso-retired-interface-config-literal
    single_quoted = 'interface_config'
    # ruleid: nso-retired-interface-config-literal
    bytes_literal = b"interface_config"
    # ruleid: nso-retired-interface-config-literal
    concatenated_literal = "interface_" + "config"
    # ok: nso-retired-interface-config-literal
    different_literal = "interface_state"


def retired_push_builder_match_capture(value):
    match value:
        # ruleid: nso-retired-push-builder
        case _push_vlan_intent_for_device:
            return None


def retired_coalescer_state_match_capture(value):
    match value:
        # ruleid: nso-retired-coalescer-state
        case _pending_pushes:
            return None


def threading_import_before(worker):
    import threading

    # ruleid: nso-unbounded-thread-join
    worker.join()


def aliased_threading_import_before(worker):
    import threading as thread_module

    # ruleid: nso-unbounded-thread-join
    worker.join()


def threading_symbol_import_before(worker):
    from threading import Thread

    # ruleid: nso-unbounded-thread-join
    worker.join()


def aliased_threading_symbol_import_before(worker):
    from threading import Thread as WorkerThread

    # ruleid: nso-unbounded-thread-join
    worker.join()


def threading_import_after(worker):
    # ruleid: nso-unbounded-thread-join
    worker.join()

    import threading


def aliased_threading_import_after(worker):
    # ruleid: nso-unbounded-thread-join
    worker.join()

    import threading as thread_module


def threading_symbol_import_after(worker):
    # ruleid: nso-unbounded-thread-join
    worker.join()

    from threading import Thread


def aliased_threading_symbol_import_after(worker):
    # ruleid: nso-unbounded-thread-join
    worker.join()

    from threading import Thread as WorkerThread


def visible_barrier_failure(barrier):
    # ok: nso-swallowed-barrier-failure
    try:
        barrier.wait(timeout=2)
    except BrokenBarrierError:
        raise AssertionError("The concurrent writer did not reach the barrier")


def vlan_group_identity():
    # ruleid: nso-vlan-group-mutable-lookup
    VLANGroup.objects.get_or_create(slug="nso-group", name="display name")
    # ok: nso-vlan-group-mutable-lookup
    VLANGroup.objects.get_or_create(slug="nso-group", defaults={"name": "display name"})


def write_set_cardinality(self, plan, expected):
    # ruleid: nso-write-set-cardinality-assertion
    self.assertEqual({write.model_label for write in plan.write_set}, expected)
    # ruleid: nso-write-set-cardinality-assertion
    self.assertEqual(
        {write.pk for write in plan.write_set if write.operation == "save"},
        expected,
    )
    # ruleid: nso-write-set-cardinality-assertion
    self.assertEqual(expected, {write.model_label for write in plan.write_set})
    # ruleid: nso-write-set-cardinality-assertion
    self.assertEqual(
        expected,
        {write.pk for write in plan.write_set if write.operation == "save"},
    )
    # ok: nso-write-set-cardinality-assertion
    self.assertCountEqual([write.model_label for write in plan.write_set], expected)
    # ok: nso-write-set-cardinality-assertion
    self.assertTrue(expected <= {write.model_label for write in plan.write_set})
    # ok: nso-write-set-cardinality-assertion
    self.assertEqual({item.model_label for item in expected}, {"model"})


def text_file_encodings(path, data, archive):
    # ruleid: nso-implicit-text-encoding
    path.read_text()
    # ruleid: nso-implicit-text-encoding
    path.write_text(data)
    # ruleid: nso-implicit-text-encoding
    path.open()
    # ruleid: nso-implicit-text-encoding
    path.open("w")
    # ruleid: nso-implicit-text-encoding
    open(path)
    # ruleid: nso-implicit-text-encoding
    open(path, "w")
    # ok: nso-implicit-text-encoding
    path.read_text(encoding="utf-8")
    # ok: nso-implicit-text-encoding
    path.write_text(data, encoding="utf-8")
    # ok: nso-implicit-text-encoding
    path.open("w", encoding="utf-8")
    # ok: nso-implicit-text-encoding
    open(path, "w", encoding="utf-8")
    # ok: nso-implicit-text-encoding
    path.open("wb")
    # ok: nso-implicit-text-encoding
    path.open(mode="rb")
    # ok: nso-implicit-text-encoding
    open(path, "rb")
    # ok: nso-implicit-text-encoding
    open(path, mode="rb")
    # A module-level open takes the file first and the mode second.
    # ok: nso-implicit-text-encoding
    archive.open(path, "rb")


def silent_adapter_duplicates(payload, normalized, entries):
    seen = set()
    for entry in payload.get("entries") or []:
        # ruleid: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])

    seen = set()
    for entry in normalized:
        # ruleid: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])

    for entry in entries:
        # ruleid: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])


def visible_adapter_duplicates(payload, normalized, entries, states):
    seen = set()
    for entry in payload.get("entries") or []:
        # ok: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            raise ValueError("duplicate entry")
        seen.add(entry["id"])

    for entry in normalized:
        # ok: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            raise ValueError("duplicate entry")
        seen.add(entry["id"])

    for entry in entries:
        # ok: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            raise ValueError("duplicate entry")
        seen.add(entry["id"])

    for key, state in states.items():
        # ok: nso-silent-duplicate-adapter-entry
        if key in seen:
            continue
        state.mark_stale()


def wire_signals(handler, sender, custom_signal):
    # ruleid: nso-signal-connect-without-dispatch-uid
    pre_save.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.post_save.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.pre_delete.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    post_delete_signal.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.m2m_changed.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.pre_init.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.post_init.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.pre_migrate.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.post_migrate.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.class_prepared.connect(handler, sender=sender)
    # ok: nso-signal-connect-without-dispatch-uid
    post_save.connect(handler, sender=sender, dispatch_uid="nso_plugin_example")
    # ok: nso-signal-connect-without-dispatch-uid
    model_signals.post_save.connect(handler, sender=sender, dispatch_uid="nso_plugin_example")
    # ok: nso-signal-connect-without-dispatch-uid
    custom_signal.connect(handler, sender=sender)


def wire_delete_signals(sender):
    # ruleid: nso-delete-signal-without-delete-origin
    model_signals.pre_delete.connect(_on_delete, sender=sender, dispatch_uid="pre_delete")
    # ruleid: nso-delete-signal-without-delete-origin
    post_delete_signal.connect(_on_post_delete, sender=sender, dispatch_uid="post_delete")
    # ruleid: nso-delete-signal-without-delete-origin
    django.db.models.signals.post_delete.connect(_on_qualified_delete, sender=sender, dispatch_uid="qualified")
    # ok: nso-delete-signal-without-delete-origin
    model_signals.pre_delete.connect(_as_delete_origin(_on_delete), sender=sender, dispatch_uid="wrapped")
    # ok: nso-delete-signal-without-delete-origin
    model_signals.post_delete.connect(_as_delete_origin(_on_post_delete), sender=sender, dispatch_uid="wrapped_post")
    # ok: nso-delete-signal-without-delete-origin
    model_signals.post_delete.connect(_on_lacp_state_save, sender=sender, dispatch_uid="switching_root")
    # ok: nso-delete-signal-without-delete-origin
    model_signals.post_delete.connect(_on_switchport_state_save, sender=sender, dispatch_uid="switching_port")
    # ok: nso-delete-signal-without-delete-origin
    model_signals.pre_delete.connect(_validate_explicit_delete, sender=sender, dispatch_uid="validator")
    # ok: nso-delete-signal-without-delete-origin
    model_signals.post_save.connect(_on_save, sender=sender, dispatch_uid="save")


def unchecked_request_body_shapes(request, self):
    # ruleid: nso-unchecked-request-body
    body = request.data
    body.get("device_id")
    # ruleid: nso-unchecked-request-body
    body = self.request.data
    body.get("device_id")
    # ruleid: nso-unchecked-request-body
    request.data.get("device_id")
    # ok: nso-unchecked-request-body
    Serializer(data=request.data)
    # ok: nso-unchecked-request-body
    Serializer(data=self.request.data, many=True)


def _request_body(request):
    # ok: nso-unchecked-request-body
    return request.data.get("device_id")


def unguarded_overlay_signature(state_key, planner):
    # ruleid: nso-unguarded-overlay-signature
    signature = _qualifying_overlay_signature("device", "model", 1, "state", state_key)
    # ruleid: nso-unguarded-overlay-signature
    planner._qualifying_overlay_signature("device", "model", 1, "state", state_key)
    # ruleid: nso-unguarded-overlay-signature
    resolver = _qualifying_overlay_signature
    return signature, resolver


def _valid_overlay_signature(state_key):
    # ok: nso-unguarded-overlay-signature
    return _qualifying_overlay_signature("device", "model", 1, "state", state_key)


def _qualifying_overlay_signatures(state_key):
    # ok: nso-unguarded-overlay-signature
    return _qualifying_overlay_signature("device", "model", 1, "state", state_key)


# ruleid: nso-renderer-writer-single-resolver
class RendererWriter:
    def render(self):
        return None


# ruleid: nso-renderer-writer-single-resolver
class RendererWriter:
    def _resolve_reference(self, reference):
        return reference

    def _resolve_reference(self, reference):
        return reference


# ok: nso-renderer-writer-single-resolver
class RendererWriter:
    def _resolve_reference(self, reference):
        return reference

    def helper(self):
        def _resolve_reference(reference):
            return reference

        return _resolve_reference


# ok: nso-renderer-writer-single-resolver
class RendererWriter:
    def _resolve_reference(self, reference):
        return reference

    class RendererWriter:
        pass


# ok: nso-renderer-writer-single-resolver
class ＲendererWriter:
    def render(self):
        return None


# ruleid: nso-renderer-writer-single-resolver
class RendererWriter:
    def _ｒesolve_reference(self, reference):
        return reference


# ok: nso-renderer-writer-single-resolver
class Writer:
    pass


RendererWriter = Writer


def resume_qualified_sites(self):
    import netbox_nso_plugin.deployment

    try:
        # ast-clean: nso-resume-failure-guidance
        netbox_nso_plugin.deployment.resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    netbox_nso_plugin.deployment.resume()


def resume_module_alias_sites(self):
    import netbox_nso_plugin.deployment as deployment_module

    try:
        # ast-clean: nso-resume-failure-guidance
        deployment_module.resume()
    except BaseException as exc:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    f"Intent work may remain quiesced: {exc}. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    deployment_module.resume()


def resume_package_alias_sites(self):
    import netbox_nso_plugin as plugin_package

    try:
        # ast-clean: nso-resume-failure-guidance
        plugin_package.deployment.resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    plugin_package.deployment.resume()


def resume_package_module_sites(self):
    from netbox_nso_plugin import deployment
    from contextlib import suppress

    try:
        # ast-clean: nso-resume-failure-guidance
        deployment.resume()
    except BaseException:
        with suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    deployment.resume()


def resume_relative_module_sites(self):
    from ... import deployment

    try:
        # ast-clean: nso-resume-failure-guidance
        deployment.resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    deployment.resume()


def resume_symbol_sites(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-clean: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    resume()


def resume_symbol_alias_sites(self):
    from netbox_nso_plugin.deployment import resume as restart

    try:
        # ast-clean: nso-resume-failure-guidance
        restart()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    restart()


def resume_relative_symbol_sites(self):
    from ...deployment import resume

    try:
        # ast-clean: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    resume()


def resume_relative_symbol_alias_sites(self):
    from ...deployment import resume as relative_restart

    try:
        # ast-clean: nso-resume-failure-guidance
        relative_restart()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    # ast-finding: nso-resume-failure-guidance
    relative_restart()


def resume_relative_sibling_module_site():
    from . import deployment

    # ast-finding: nso-resume-failure-guidance
    deployment.resume()


def resume_relative_parent_module_site():
    from .. import deployment

    # ast-finding: nso-resume-failure-guidance
    deployment.resume()


def resume_relative_sibling_module_alias_site():
    from . import deployment as sibling_gate

    # ast-finding: nso-resume-failure-guidance
    sibling_gate.resume()


def resume_relative_parent_module_alias_site():
    from .. import deployment as parent_gate

    # ast-finding: nso-resume-failure-guidance
    parent_gate.resume()


def resume_relative_package_module_alias_site():
    from ... import deployment as package_gate

    # ast-finding: nso-resume-failure-guidance
    package_gate.resume()


def resume_relative_sibling_symbol_site():
    from .deployment import resume

    # ast-finding: nso-resume-failure-guidance
    resume()


def resume_relative_parent_symbol_site():
    from ..deployment import resume

    # ast-finding: nso-resume-failure-guidance
    resume()


def resume_relative_parent_symbol_alias_site():
    from ..deployment import resume as parent_restart

    # ast-finding: nso-resume-failure-guidance
    parent_restart()


def resume_relative_sibling_symbol_alias_site():
    from .deployment import resume as sibling_restart

    # ast-finding: nso-resume-failure-guidance
    sibling_restart()


def resume_report_with_cleanup(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-clean: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    finally:
        self.close_connection()


def resume_report_with_write_options(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-clean: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                ),
                ending="",
            )
        raise


def resume_report_with_contextlib_alias(self):
    import contextlib as gate_contextlib

    from netbox_nso_plugin.deployment import resume

    try:
        # ast-clean: nso-resume-failure-guidance
        resume()
    except BaseException:
        with gate_contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_report_in_nested_try(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        try:
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        except Exception:
            pass
        raise


def resume_report_in_nested_definition(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:

        def report():
            with contextlib.suppress(Exception):
                self.stderr.write(
                    self.style.ERROR(
                        "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                    )
                )

        raise


def resume_report_in_condition(self, should_report):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        if should_report:
            with contextlib.suppress(Exception):
                self.stderr.write(
                    self.style.ERROR(
                        "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                    )
                )
        raise


def resume_report_with_second_statement(self, note_reported):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
            note_reported()
        raise


def resume_report_with_early_exit(self, skip_reraise):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        if skip_reraise:
            return
        raise


def resume_report_after_bare_raise(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        raise
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_report_after_explicit_raise(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        raise RuntimeError("resume failed")
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_report_suppressing_os_error(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(OSError):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_report_suppressing_base_exception(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(BaseException):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_report_in_null_context(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.nullcontext(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_report_without_suppression(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        self.stderr.write(
            self.style.ERROR(
                "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
            )
        )
        raise


def resume_report_without_recovery_command(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(self.style.ERROR("Intent work may remain quiesced."))
        raise


def resume_report_from_bare_variable(self, message):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(self.style.ERROR(message))
        raise


def resume_cleanup_under_handler(self, run):
    from netbox_nso_plugin.deployment import resume

    try:
        run()
    except BaseException:
        # ast-finding: nso-resume-failure-guidance
        resume()
        raise


class DeploymentGateCommand:
    def handle(self, *args, **options):
        from netbox_nso_plugin.deployment import resume

        if options["abort"]:
            # ast-clean: nso-resume-failure-guidance
            resume()
            self.stdout.write("Deployment gate aborted")
            return
        # ast-finding: nso-resume-failure-guidance
        resume()


class DeploymentGateAlternateBranchCommand:
    def handle(self, *args, **options):
        from netbox_nso_plugin.deployment import resume

        if options["abort"]:
            self.stdout.write("Deployment gate aborted")
        elif options["retry"]:
            # ast-finding: nso-resume-failure-guidance
            resume()
        else:
            # ast-finding: nso-resume-failure-guidance
            resume()


class DeploymentGateNestedAbortDefinitionCommand:
    def handle(self, *args, **options):
        from netbox_nso_plugin.deployment import resume

        if options["abort"]:

            def deferred():
                # ast-finding: nso-resume-failure-guidance
                resume()

            return deferred


class DeploymentGateNestedAbortLambdaCommand:
    def handle(self, *args, **options):
        from netbox_nso_plugin.deployment import resume

        if options["abort"]:
            # ast-finding: nso-resume-failure-guidance
            deferred = lambda: resume()
            return deferred


class DeploymentGateNestedAbortGeneratorCommand:
    def handle(self, *args, **options):
        from netbox_nso_plugin.deployment import resume

        if options["abort"]:
            # ast-finding: nso-resume-failure-guidance
            return (resume() for _ in [1])


def resume_on_unbound_receiver(receiver):
    # ast-clean: nso-resume-failure-guidance
    receiver.resume()


def resume_with_a_second_handler(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError:
        pass
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_with_a_second_handler_and_cleanup(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError:
        pass
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    finally:
        self.close_connection()


def resume_with_a_bound_second_handler(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError as error:
        del error
    except BaseException as exc:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    f"Intent work may remain quiesced: {exc}. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_with_a_bound_base_handler(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError:
        pass
    except BaseException as exc:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    f"Intent work may remain quiesced: {exc}. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_with_a_bound_base_handler_and_cleanup(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError:
        pass
    except BaseException as exc:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    f"Intent work may remain quiesced: {exc}. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    finally:
        self.close_connection()


def resume_with_a_bound_first_handler(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError as error:
        del error
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise


def resume_with_a_bound_first_handler_and_cleanup(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError as error:
        del error
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    finally:
        self.close_connection()


def resume_with_a_bound_second_handler_and_cleanup(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-finding: nso-resume-failure-guidance
        resume()
    except ValueError as error:
        del error
    except BaseException as exc:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    f"Intent work may remain quiesced: {exc}. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    finally:
        self.close_connection()


def resume_in_cleanup_of_a_guarded_call(self, other):
    from netbox_nso_plugin.deployment import resume

    try:
        other()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    finally:
        # ast-finding: nso-resume-failure-guidance
        resume()


def resume_in_cleanup_of_the_same_guarded_call(self):
    from netbox_nso_plugin.deployment import resume

    try:
        # ast-clean: nso-resume-failure-guidance
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
    finally:
        # ast-finding: nso-resume-failure-guidance
        resume()


class DeploymentGateSameCallAlternateBranchCommand:
    def handle(self, *args, **options):
        from netbox_nso_plugin.deployment import resume

        if options["abort"]:
            # ast-clean: nso-resume-failure-guidance
            resume()
        else:
            # ast-finding: nso-resume-failure-guidance
            resume()


def adapter_error_fully_qualified(items):
    import netbox_nso_plugin.adapter_client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise netbox_nso_plugin.adapter_client.AdapterError("invalid")


def adapter_error_package_alias(items):
    import netbox_nso_plugin as package

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise package.adapter_client.AdapterError("invalid")


def adapter_error_module_alias(items):
    import netbox_nso_plugin.adapter_client as client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise client.AdapterError("invalid")


def adapter_error_absolute_module_alias(items):
    from netbox_nso_plugin import adapter_client as client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise client.AdapterError("invalid")


def adapter_error_relative_module_alias(items):
    from . import adapter_client as client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise client.AdapterError("invalid")


def adapter_error_parent_module_alias(items):
    from .. import adapter_client as client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise client.AdapterError("invalid")


def adapter_error_grandparent_module_alias(items):
    from ... import adapter_client as client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise client.AdapterError("invalid")


def vlan_lambda_default_identity(interface):
    suffix = interface.name.split(".")[-1]
    # ruleid: nso-vlan-identity-flow
    (lambda suffix=dict(dot1q_vlan=int(suffix)): suffix)()
    # ok: nso-vlan-identity-flow
    (lambda suffix=interface.cf["vlan"]: int(suffix))()
    # ok: nso-vlan-identity-flow
    (lambda suffix=interface.cf["vlan"]: dict(dot1q_vlan=int(suffix)))()


def vlan_function_default_identity(interface):
    suffix = interface.name.split(".")[-1]

    # ruleid: nso-vlan-identity-flow
    def nested(suffix=dict(dot1q_vlan=int(suffix))):
        return suffix

    # ruleid: nso-vlan-identity-flow
    def keyword_default(*, suffix=dict(dot1q_vlan=int(suffix))):
        return suffix

    # ruleid: nso-vlan-identity-flow
    @decorate(dict(dot1q_vlan=int(suffix)))
    def decorated(suffix):
        return suffix


def vlan_defaults_after_clean_overwrite(interface):
    suffix = interface.name.split(".")[-1]
    suffix = interface.cf["vlan"]

    # ok: nso-vlan-identity-flow
    def nested(suffix=dict(dot1q_vlan=int(suffix))):
        return suffix

    # ok: nso-vlan-identity-flow
    def keyword_default(*, suffix=dict(dot1q_vlan=int(suffix))):
        return suffix

    # ok: nso-vlan-identity-flow
    (lambda suffix=dict(dot1q_vlan=int(suffix)): suffix)()
    # ok: nso-vlan-identity-flow
    (lambda *, suffix=dict(dot1q_vlan=int(suffix)): suffix)()

    # ok: nso-vlan-identity-flow
    @decorate(dict(dot1q_vlan=int(suffix)))
    def decorated(suffix):
        return suffix


def vlan_composed_default_identity(interface):
    suffix = interface.name.split(".")[-1]

    # ruleid: nso-vlan-identity-flow
    def nested(x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]):
        return x

    # ruleid: nso-vlan-identity-flow
    def keyword_default(*, x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]):
        return x

    # ruleid: nso-vlan-identity-flow
    def invoked_lambda(x=(lambda: dict(dot1q_vlan=int(suffix)))()):
        return x

    # ruleid: nso-vlan-identity-flow
    @decorate([dict(dot1q_vlan=int(suffix)) for _ in [1]])
    def decorated(x):
        return x

    # ruleid: nso-vlan-identity-flow
    @decorate((lambda: dict(dot1q_vlan=int(suffix)))())
    def lambda_decorated(x):
        return x

    # ruleid: nso-vlan-identity-flow
    (lambda x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]: x)()
    # ruleid: nso-vlan-identity-flow
    (lambda *, x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]: x)()
    # ruleid: nso-vlan-identity-flow
    [(lambda x=dict(dot1q_vlan=int(suffix)): x)() for _ in [1]]
    # ruleid: nso-vlan-identity-flow
    [suffix for suffix in (lambda x=dict(dot1q_vlan=int(suffix)): [x])()]


def vlan_composed_defaults_after_clean_overwrite(interface):
    suffix = interface.name.split(".")[-1]
    suffix = interface.cf["vlan"]

    # ok: nso-vlan-identity-flow
    def nested(x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]):
        return x

    # ok: nso-vlan-identity-flow
    def keyword_default(*, x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]):
        return x

    # ok: nso-vlan-identity-flow
    def invoked_lambda(x=(lambda: dict(dot1q_vlan=int(suffix)))()):
        return x

    # ok: nso-vlan-identity-flow
    @decorate([dict(dot1q_vlan=int(suffix)) for _ in [1]])
    def decorated(x):
        return x

    # ok: nso-vlan-identity-flow
    @decorate((lambda: dict(dot1q_vlan=int(suffix)))())
    def lambda_decorated(x):
        return x

    # ok: nso-vlan-identity-flow
    (lambda x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]: x)()
    # ok: nso-vlan-identity-flow
    (lambda *, x=[dict(dot1q_vlan=int(suffix)) for _ in [1]]: x)()
    # ok: nso-vlan-identity-flow
    [(lambda x=dict(dot1q_vlan=int(suffix)): x)() for _ in [1]]
    # ok: nso-vlan-identity-flow
    [suffix for suffix in (lambda x=dict(dot1q_vlan=int(suffix)): [x])()]


def vlan_flow_cyclic_bindings(interface, items):
    for item in items:
        left = right
        right = left
        if item:
            right = interface.name.split(".")[-1]
        # ruleid: nso-vlan-identity-flow
        vid = int(left)
        # ruleid: nso-vlan-identity-flow
        dot1q_vlan = int(right)


def vlan_flow_clean_cyclic_bindings(interface, items):
    for item in items:
        left = right
        right = left
        if item:
            right = interface.cf["vlan"]
        # ok: nso-vlan-identity-flow
        vid = int(left)
        # ok: nso-vlan-identity-flow
        dot1q_vlan = int(right)


def adapter_error_absolute_module(items):
    from netbox_nso_plugin import adapter_client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise adapter_client.AdapterError("invalid")


def adapter_error_relative_module(items):
    from . import adapter_client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise adapter_client.AdapterError("invalid")


def adapter_error_parent_module(items):
    from .. import adapter_client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise adapter_client.AdapterError("invalid")


def adapter_error_grandparent_module(items):
    from ... import adapter_client

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise adapter_client.AdapterError("invalid")


def adapter_error_absolute_symbol(items):
    from netbox_nso_plugin.adapter_client import AdapterError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise AdapterError("invalid")


def adapter_error_relative_symbol(items):
    from .adapter_client import AdapterError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise AdapterError("invalid")


def adapter_error_parent_symbol(items):
    from ..adapter_client import AdapterError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise AdapterError("invalid")


def adapter_error_grandparent_symbol(items):
    from ...adapter_client import AdapterError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise AdapterError("invalid")


def adapter_error_absolute_symbol_alias(items):
    from netbox_nso_plugin.adapter_client import AdapterError as PayloadError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise PayloadError("invalid")


def adapter_error_relative_symbol_alias(items):
    from .adapter_client import AdapterError as PayloadError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise PayloadError("invalid")


def adapter_error_parent_symbol_alias(items):
    from ..adapter_client import AdapterError as PayloadError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise PayloadError("invalid")


def adapter_error_grandparent_symbol_alias(items):
    from ...adapter_client import AdapterError as PayloadError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise PayloadError("invalid")


def adapter_error_before_the_continue(items):
    from .adapter_client import AdapterError

    for item in items:
        if not item:
            # ast-finding: nso-adapter-error-after-continue
            raise AdapterError("invalid")
        continue


def adapter_error_over_a_nested_continue(items):
    from .adapter_client import AdapterError

    for item in items:
        for part in item:
            if part:
                continue
        # ast-clean: nso-adapter-error-after-continue
        raise AdapterError("invalid")


async def adapter_error_in_an_async_loop(items):
    from .adapter_client import AdapterError

    async for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise AdapterError("invalid")


def adapter_error_chained_to_a_cause(items, cause):
    from .adapter_client import AdapterError

    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise AdapterError("invalid") from cause


# The import binding survives a later rebinding, so this alias is an accepted false positive.
def adapter_error_rebound_alias(items):
    from .adapter_client import AdapterError as PayloadError

    PayloadError = ValueError
    for item in items:
        if not item:
            continue
        # ast-finding: nso-adapter-error-after-continue
        raise PayloadError("invalid")


def adapter_error_validated_before_the_loop(items):
    from .adapter_client import AdapterError

    if not all(items):
        # ast-clean: nso-adapter-error-after-continue
        raise AdapterError("invalid")
    for item in items:
        if not item:
            continue


def adapter_error_in_a_loop_without_continue(items):
    from .adapter_client import AdapterError

    for item in items:
        # ast-clean: nso-adapter-error-after-continue
        raise AdapterError("invalid")


def adapter_error_shadowed_symbol(items, AdapterError):
    from .adapter_client import AdapterError as _reserved

    for item in items:
        if not item:
            continue
        # ast-clean: nso-adapter-error-after-continue
        raise AdapterError("invalid")


# The parameter shadow outlasts the import that rebinds it, so this is an accepted false negative.
def adapter_error_shadowed_alias(items, PayloadError):
    from .adapter_client import AdapterError as PayloadError

    for item in items:
        if not item:
            continue
        # ast-clean: nso-adapter-error-after-continue
        raise PayloadError("invalid")


# The parameter shadow outlasts the import that rebinds it, so this is an accepted false negative.
def adapter_error_shadowed_module(items, adapter_client):
    from . import adapter_client as adapter_client

    for item in items:
        if not item:
            continue
        # ast-clean: nso-adapter-error-after-continue
        raise adapter_client.AdapterError("invalid")


def adapter_error_unimported_symbol(items):
    for item in items:
        if not item:
            continue
        # ast-clean: nso-adapter-error-after-continue
        raise AdapterError("invalid")


def adapter_error_unrelated_exception(items):
    from .adapter_client import AdapterError as _reserved

    for item in items:
        if not item:
            continue
        # ast-clean: nso-adapter-error-after-continue
        raise ValueError("invalid")


# Ownership grants use a literal operation from the registry.
def ownership_grants(kind):
    from netbox_nso_plugin.ownership_grants import OwnershipGrant
    from netbox_nso_plugin.ownership_grants import OwnershipGrant as Grant
    import netbox_nso_plugin.ownership_grants as grants

    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant(kind)
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    Grant("description_generation")
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    grants.OwnershipGrant(kind)
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant("accept")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    Grant("create")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    grants.OwnershipGrant("operator_edit")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant("intend")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant("autoassign")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant("link_role")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive
    OwnershipGrant("manifest_reown", manifest_pk=1)
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant(kind=kind)
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    Grant(kind="description_generation")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    grants.OwnershipGrant(kind="operator_edit")
    # ok: nso-ownership-grant-literal, nso-ownership-grant-planner
    # ruleid: nso-ownership-grant-passive
    OwnershipGrant(kind="manifest_reown", manifest_pk=1)


def forbidden_ownership_grants():
    from netbox_nso_plugin.ownership_grants import OwnershipGrant

    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant("accept")
    # ruleid: nso-ownership-grant-planner, nso-ownership-grant-passive
    OwnershipGrant("create")
    # ok: nso-ownership-grant-planner
    # ruleid: nso-ownership-grant-passive
    OwnershipGrant("manifest_reown", manifest_pk=1)


def relative_ownership_grants(kind):
    from .ownership_grants import OwnershipGrant
    from .ownership_grants import OwnershipGrant as RelativeGrant
    from . import ownership_grants as relative_grants

    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    OwnershipGrant(kind)
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    RelativeGrant(kind=kind)
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    relative_grants.OwnershipGrant("unclassified")
    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    RelativeGrant("create")
    # ok: nso-ownership-grant-literal, nso-ownership-grant-planner
    # ruleid: nso-ownership-grant-passive
    OwnershipGrant(kind="manifest_reown", manifest_pk=1)


def relative_ownership_grant_module(kind):
    from . import ownership_grants

    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    ownership_grants.OwnershipGrant(kind)
    # ok: nso-ownership-grant-literal, nso-ownership-grant-planner
    # ruleid: nso-ownership-grant-passive
    ownership_grants.OwnershipGrant("manifest_reown", manifest_pk=1)


def nested_ownership_grants(writer, row, kind):
    from .ownership_grants import OwnershipGrant
    from .ownership_grants import OwnershipGrant as RelativeGrant
    from . import ownership_grants as relative_grants
    from . import ownership_grants

    # ok: nso-ownership-grant-literal
    # ruleid: nso-ownership-grant-passive, nso-ownership-grant-planner
    writer.execute(row, grant=OwnershipGrant("accept"))
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    writer.execute(row, grant=OwnershipGrant(kind=kind))
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    RelativeGrant(kind)
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    relative_grants.OwnershipGrant(kind=kind)
    # ruleid: nso-ownership-grant-literal, nso-ownership-grant-passive, nso-ownership-grant-planner
    ownership_grants.OwnershipGrant(kind=kind)


def raw_overlay_dml(cursor, model, value):
    from django.db.models.expressions import RawSQL

    # ruleid: nso-overlay-raw-sql-dml
    cursor.execute("INSERT INTO netbox_nso_plugin_nsointerfacestate (status) VALUES (%s)", [value])
    # ruleid: nso-overlay-raw-sql-dml
    cursor.execute('UPDATE "netbox_nso_plugin_nsovlanstate" SET status = %s', [value])
    # ruleid: nso-overlay-raw-sql-dml
    model.objects.raw('DELETE FROM public.netbox_nso_plugin_nsol2sapstate WHERE id = %s', [1])
    # ruleid: nso-overlay-raw-sql-dml
    RawSQL('UPDATE netbox_nso_plugin_nsoospfinterfacestate SET status = %s', [value])
    # ruleid: nso-overlay-raw-sql-dml
    expressions.RawSQL('DELETE FROM "public"."netbox_nso_plugin_nsosnmpv3userstate"', [])
    sql = "UPDATE netbox_nso_plugin_nsologginglevelstate SET status = %s"
    # ruleid: nso-overlay-raw-sql-dml
    cursor.execute(sql, [value])
    # ruleid: nso-overlay-raw-sql-dml
    model.objects.raw(sql, [value])
    # ruleid: nso-overlay-raw-sql-dml
    RawSQL(sql, [value])
    # ruleid: nso-overlay-raw-sql-dml
    expressions.RawSQL(sql, [value])
    # ruleid: nso-overlay-raw-sql-dml
    cursor.executemany("UPDATE netbox_nso_plugin_nsointerfacestate SET status=%s WHERE id=%s", [(value, 1)])
    # ruleid: nso-overlay-raw-sql-dml
    cursor.executemany('INSERT INTO "public"."netbox_nso_plugin_nsovlanstate" (status) VALUES (%s)', [(value,)])
    # ruleid: nso-overlay-raw-sql-dml
    cursor.executemany(sql, [(value,)])
    delete_sql = "DELETE FROM netbox_nso_plugin_nsologginglevelstate WHERE id = %s"
    # ruleid: nso-overlay-raw-sql-dml
    cursor.executemany(delete_sql, [(1,)])
    # ok: nso-overlay-raw-sql-dml
    cursor.executemany("SELECT status FROM netbox_nso_plugin_nsointerfacestate WHERE id=%s", [(1,)])
    # ok: nso-overlay-raw-sql-dml
    cursor.executemany("UPDATE ipam_vlan SET name=%s WHERE id=%s", [("placeholder", 1)])
    select_sql = "SELECT status FROM netbox_nso_plugin_nsointerfacestate WHERE id=%s"
    # ok: nso-overlay-raw-sql-dml
    cursor.executemany(select_sql, [(1,)])
    native_sql = "UPDATE ipam_vlan SET name=%s WHERE id=%s"
    # ok: nso-overlay-raw-sql-dml
    cursor.executemany(native_sql, [("placeholder", 1)])
    # ruleid: nso-overlay-raw-sql-dml
    cursor.execute("""insert into
        netbox_nso_plugin_nsointerfaceipstate (status) values ('accepted')""")
    # ok: nso-overlay-raw-sql-dml
    cursor.execute('SELECT status FROM netbox_nso_plugin_nsointerfacestate')
    # ok: nso-overlay-raw-sql-dml
    model.objects.raw('SELECT * FROM netbox_nso_plugin_nsovlanstate')
    # ok: nso-overlay-raw-sql-dml
    RawSQL('SELECT status FROM netbox_nso_plugin_nsointerfacestate', [])
    # ok: nso-overlay-raw-sql-dml
    cursor.execute('UPDATE ipam_vlan SET name = %s', ['placeholder'])


def lacp_observation_is_not_intent(bundle, interface):
    # ruleid: nso-lacp-observation-is-not-intent
    names = bundle.observed_members
    # ruleid: nso-lacp-observation-is-not-intent
    names = getattr(bundle, "observed_members", [])
    # ok: nso-lacp-observation-is-not-intent
    members = interface.member_interfaces.all()


def ownership_refusal_diagnostics():
    try:
        acquire()
    except OwnershipNotQualified as refusal:
        # ruleid: nso-ownership-refusal-exception-diagnostic
        response = str(refusal)
        # ruleid: nso-ownership-refusal-exception-diagnostic
        response = repr(refusal)
        # ruleid: nso-ownership-refusal-exception-diagnostic
        response = refusal.args
        # ok: nso-ownership-refusal-exception-diagnostic
        response = refusal.public_message
    try:
        acquire()
    except ownership.OwnershipNotQualified as refusal:
        # ruleid: nso-ownership-refusal-exception-diagnostic
        response = str(refusal)
        # ruleid: nso-ownership-refusal-exception-diagnostic
        response = repr(refusal)
        # ruleid: nso-ownership-refusal-exception-diagnostic
        response = refusal.args[0]
        # ok: nso-ownership-refusal-exception-diagnostic
        response = refusal.public_message
    try:
        acquire()
    except ValueError as error:
        # ok: nso-ownership-refusal-exception-diagnostic
        response = str(error)
    return response
