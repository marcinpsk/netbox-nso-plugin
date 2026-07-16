/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* nso-popedit — tiny hand-rolled popover editor (no Bootstrap-JS / Popper dep).
 *
 * Anchor contract (all data attributes, values HTML-escaped by Django):
 *   class="nso-popedit"
 *   data-pe-url    POST target (the overlay_field_edit endpoint)
 *   data-pe-title  popover heading
 *   data-pe-fields "name:type:Label" CSV — type ∈ text|number|select (multi-field allowed,
 *                  e.g. "l2_mtu:number:L2 MTU,ip_mtu:number:IP MTU")
 *   data-pe-v-<name>  current value for each field ("" for unset)
 *   data-pe-o-<name>  JSON [{value, label}] options for select fields
 *
 * Saving POSTs the fields form-encoded with CSRF, then fires a bubbling
 * "nso:popedit-saved" event from the anchor. Containers that self-refresh (the
 * interfaces grid) listen for that; for everything else this falls back to the
 * tab-wide "nso:refresh-categories" so open category fragments re-fetch.
 */
(function () {
  "use strict";
  var open = null; // { card, anchor }

  function csrfToken() {
    var el = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (el && el.value) return el.value;
    return (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  }

  function close() {
    if (!open) return;
    open.card.remove();
    open.anchor.classList.remove("nso-popedit-open");
    open = null;
  }

  function parseFields(anchor) {
    return (anchor.dataset.peFields || "").split(",").filter(Boolean).map(function (part) {
      var bits = part.split(":");
      var name = bits[0].trim();
      return {
        name: name,
        type: (bits[1] || "text").trim(),
        label: (bits[2] || name).trim(),
        value: anchor.getAttribute("data-pe-v-" + name) || "",
        options: (function () {
          try {
            var parsed = JSON.parse(anchor.getAttribute("data-pe-o-" + name) || "[]");
            return Array.isArray(parsed) ? parsed : [];
          } catch (_err) {
            return [];
          }
        })(),
      };
    });
  }

  function build(anchor) {
    close();
    var fields = parseFields(anchor);
    if (!fields.length) return;

    var card = document.createElement("div");
    card.className = "card nso-popedit-card shadow";
    // Django Debug Toolbar uses z-index 100000000 and otherwise intercepts the
    // popover's Save/Cancel buttons in the development UI. Elevate only when that
    // toolbar is present; production keeps the normal modal stacking from nso.css.
    if (document.getElementById("djDebugRoot")) card.style.zIndex = "100000001";
    var head = document.createElement("div");
    head.className = "card-header py-1 px-2 small fw-semibold";
    head.textContent = anchor.dataset.peTitle || "Edit";
    card.appendChild(head);

    var body = document.createElement("div");
    body.className = "card-body p-2";
    var inputs = {};
    fields.forEach(function (f) {
      var wrap = document.createElement("div");
      wrap.className = "mb-2";
      if (fields.length > 1) {
        var lab = document.createElement("label");
        lab.className = "form-label small text-muted mb-0";
        lab.textContent = f.label;
        wrap.appendChild(lab);
      }
      var input;
      if (f.type === "select") {
        input = document.createElement("select");
        input.className = "form-select form-select-sm";
        f.options.forEach(function (choice) {
          var option = document.createElement("option");
          option.value = choice.value;
          option.textContent = choice.label;
          input.appendChild(option);
        });
      } else {
        input = document.createElement("input");
        input.type = f.type === "number" ? "number" : "text";
        input.className = "form-control form-control-sm";
      }
      input.value = f.value;
      input.setAttribute("aria-label", f.label);
      inputs[f.name] = input;
      wrap.appendChild(input);
      var err = document.createElement("div");
      err.className = "text-danger small nso-popedit-err d-none";
      err.dataset.field = f.name;
      wrap.appendChild(err);
      body.appendChild(wrap);
    });

    var btnRow = document.createElement("div");
    btnRow.className = "d-flex gap-1 justify-content-end";
    var saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn btn-sm btn-primary";
    saveBtn.innerHTML = '<span class="mdi mdi-check"></span> Save';
    var cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-sm btn-outline-secondary";
    cancelBtn.textContent = "Cancel";
    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(saveBtn);
    body.appendChild(btnRow);
    card.appendChild(body);
    document.body.appendChild(card);

    // Position under the anchor, clamped to the viewport's right edge.
    var r = anchor.getBoundingClientRect();
    var left = Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - card.offsetWidth - 12);
    var top = r.bottom + window.scrollY + 4;
    var viewportBottom = window.scrollY + document.documentElement.clientHeight;
    if (top + card.offsetHeight > viewportBottom - 8) {
      top = Math.max(window.scrollY + 8, r.top + window.scrollY - card.offsetHeight - 4);
    }
    card.style.top = top + "px";
    card.style.left = Math.max(left, window.scrollX + 8) + "px";

    open = { card: card, anchor: anchor };
    anchor.classList.add("nso-popedit-open");
    var first = inputs[fields[0].name];
    first.focus();
    if (typeof first.select === "function") first.select();

    function showErrors(errs) {
      card.querySelectorAll(".nso-popedit-err").forEach(function (el) {
        var msgs = errs && errs[el.dataset.field];
        el.textContent = msgs ? msgs.join(" ") : "";
        el.classList.toggle("d-none", !msgs);
        var input = inputs[el.dataset.field];
        if (input) input.classList.toggle("is-invalid", !!msgs);
      });
    }

    function save() {
      // A type=number input the browser cannot parse ("12a", "1.2.3") reports value === ""
      // together with validity.badInput. On the wire that is indistinguishable from a
      // deliberately emptied field — which the edit view maps to an explicit clear-to-NULL,
      // and the next push RETRACTS the value from the live device. So a typo would silently
      // remove config. Refuse to submit a bad-input field; an intentional clear is empty AND
      // valid, and still goes through.
      var bad = fields.filter(function (f) {
        var el = inputs[f.name];
        return el.validity && el.validity.badInput;
      });
      if (bad.length) {
        var badErrs = {};
        bad.forEach(function (f) {
          badErrs[f.name] = ["Enter a valid number, or clear the field to remove the value."];
        });
        showErrors(badErrs);
        return;
      }
      saveBtn.disabled = true;
      var params = new URLSearchParams();
      fields.forEach(function (f) { params.set(f.name, inputs[f.name].value.trim()); });
      params.set("csrfmiddlewaretoken", csrfToken());
      fetch(anchor.dataset.peUrl, {
        method: "POST",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "X-CSRFToken": csrfToken(),
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: params,
      })
        .then(function (resp) {
          return resp.json().catch(function () { return {}; }).then(function (d) { return { ok: resp.ok, data: d }; });
        })
        .then(function (res) {
          if (!res.ok) {
            saveBtn.disabled = false;
            showErrors(res.data && res.data.errors);
            if (res.data && res.data.message && !res.data.errors) {
              head.textContent = res.data.message;
              head.classList.add("text-danger");
            }
            return;
          }
          var target = anchor;
          close();
          target.dispatchEvent(new CustomEvent("nso:popedit-saved", { bubbles: true }));
          // Self-refreshing containers (any nso-grid mount) reload themselves off the
          // event above; server-rendered fragments re-fetch via the tab-wide hook.
          // Scoped to .nso-grid — the harness class every grid has — not .nso-ifg,
          // which only the interfaces panel kept: a save inside any other grid would
          // reload its own grid AND re-fetch every open category.
          if (!target.closest(".nso-grid")) {
            document.dispatchEvent(new CustomEvent("nso:refresh-categories"));
          }
        })
        .catch(function () {
          saveBtn.disabled = false;
          head.textContent = "Save failed — network error";
          head.classList.add("text-danger");
        });
    }

    saveBtn.addEventListener("click", save);
    cancelBtn.addEventListener("click", close);
    card.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && fields.length === 1) { e.preventDefault(); save(); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
  }

  document.addEventListener("click", function (e) {
    var anchor = e.target.closest(".nso-popedit");
    if (anchor) {
      e.preventDefault();
      if (open && open.anchor === anchor) { close(); return; }
      build(anchor);
      return;
    }
    if (open && !e.target.closest(".nso-popedit-card")) close();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") close();
  });
})();
