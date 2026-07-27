/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Compact LACP bundle grid with coordinated bundle/member inline editing. */
(function () {
  "use strict";

  var G = window.NSOGrid;

  function badge(text, classes, title) {
    var item = document.createElement("span");
    item.className = "badge " + classes;
    item.textContent = text;
    if (title) item.title = title;
    return item;
  }

  function fmtBundle(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    var link = document.createElement("a");
    link.href = row.bundle.url;
    link.textContent = row.bundle.name;
    wrap.appendChild(link);
    var id = document.createElement("div");
    id.className = "small text-muted";
    id.textContent = row.lag_id == null ? "LAG ID —" : "LAG " + row.lag_id;
    wrap.appendChild(id);
    return wrap;
  }

  function fmtParameters(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-1 flex-wrap";
    if (row.min_links != null) wrap.appendChild(badge("min " + row.min_links, "text-bg-light border"));
    if (row.system_priority != null) {
      wrap.appendChild(badge("priority " + row.system_priority, "text-bg-light border"));
    }
    if (row.timer) wrap.appendChild(badge(row.timer, "text-bg-info"));
    if (row.admin_key != null) wrap.appendChild(badge("key " + row.admin_key, "text-bg-secondary"));
    if (row.system_id) wrap.appendChild(badge(row.system_id, "text-bg-light border", "LACP system ID (read-only)"));
    if (!wrap.childNodes.length) wrap.appendChild(document.createTextNode("—"));

    if (row.edit_url) {
      var edit = document.createElement("a");
      edit.href = "#";
      edit.className = "nso-popedit text-secondary";
      edit.title = "Edit LACP parameters (editing owns the bundle and its members)";
      edit.setAttribute("aria-label", "Edit LACP bundle " + row.bundle.name);
      edit.dataset.peUrl = row.edit_url;
      edit.dataset.peTitle = row.bundle.name + " LACP";
      edit.dataset.peFields =
        "min_links:text:Min links,system_priority:text:System priority,timer:select:Timer,admin_key:text:Admin key";
      edit.setAttribute("data-pe-v-min_links", row.min_links == null ? "" : row.min_links);
      edit.setAttribute("data-pe-v-system_priority", row.system_priority == null ? "" : row.system_priority);
      edit.setAttribute("data-pe-v-timer", row.timer || "");
      edit.setAttribute("data-pe-v-admin_key", row.admin_key == null ? "" : row.admin_key);
      edit.setAttribute(
        "data-pe-o-timer",
        JSON.stringify([
          { value: "", label: "Default" },
          { value: "fast", label: "Fast" },
          { value: "slow", label: "Slow" },
        ]),
      );
      edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
      wrap.appendChild(edit);
    }
    return wrap;
  }

  function memberEditor(member) {
    if (!member.edit_url) return null;
    var edit = document.createElement("a");
    edit.href = "#";
    edit.className = "nso-popedit text-secondary";
    edit.title = "Edit member LACP mode and priority";
    edit.setAttribute("aria-label", "Edit LACP member " + member.interface.name);
    edit.dataset.peUrl = member.edit_url;
    edit.dataset.peTitle = member.interface.name + " LACP";
    edit.dataset.peFields = "mode:select:Mode,port_priority:text:Port priority";
    edit.setAttribute("data-pe-v-mode", member.mode || "");
    edit.setAttribute("data-pe-v-port_priority", member.port_priority == null ? "" : member.port_priority);
    edit.setAttribute(
      "data-pe-o-mode",
      JSON.stringify([
        { value: "", label: "Default" },
        { value: "active", label: "Active" },
        { value: "passive", label: "Passive" },
        { value: "on", label: "On" },
      ]),
    );
    edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
    return edit;
  }

  function fmtMembers(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    (row.members || []).forEach(function (member) {
      var line = document.createElement("div");
      line.className = "d-flex align-items-center gap-1 flex-wrap";
      var link = document.createElement("a");
      link.href = member.interface.url;
      link.className = "font-monospace text-truncate d-inline-block";
      link.style.maxWidth = "120px";
      link.title = member.interface.name;
      link.textContent = member.interface.name;
      line.appendChild(link);
      if (member.mode) line.appendChild(badge(member.mode, "text-bg-light border"));
      if (member.port_priority != null) {
        line.appendChild(badge("pri " + member.port_priority, "text-bg-light border"));
      }
      var edit = memberEditor(member);
      if (edit) line.appendChild(edit);
      wrap.appendChild(line);
    });
    if (!wrap.childNodes.length) {
      var empty = document.createElement("span");
      empty.className = "text-muted";
      empty.textContent = "—";
      wrap.appendChild(empty);
    }
    return wrap;
  }

  function mount(root) {
    var payloadEl = document.getElementById("nso-lacp-data");
    if (!root || !payloadEl || !G) return;
    return G.mount(root, {
      key: "lacp",
      payload: JSON.parse(payloadEl.textContent),
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: "No LACP bundles.",
      flatten: function (rows) {
        return rows.map(function (row) {
          row._bundle = [row.bundle.name, row.lag_id == null ? "" : row.lag_id].join(" ");
          row._parameters = [row.min_links, row.system_priority, row.system_id, row.timer, row.admin_key].join(" ");
          row._members = (row.members || []).map(function (member) {
            return [member.interface.name, member.mode, member.port_priority].join(" ");
          }).join(" ");
          return row;
        });
      },
      columns: [
        {
          title: "Bundle",
          field: "_bundle",
          formatter: fmtBundle,
          sorter: "alphanum",
          widthGrow: 0.8,
          minWidth: 110,
          headerFilter: "input",
          headerFilterPlaceholder: "bundle…",
        },
        { title: "LACP parameters", field: "_parameters", formatter: fmtParameters, widthGrow: 1.4, minWidth: 160 },
        {
          title: "Members",
          field: "_members",
          formatter: fmtMembers,
          widthGrow: 1.5,
          minWidth: 180,
          headerFilter: "input",
          headerFilterPlaceholder: "member…",
        },
        G.stateColumn({ widthGrow: 0.5, minWidth: 80 }),
        G.lastSyncColumn({ widthGrow: 0.6, minWidth: 90 }),
        G.acceptColumn({ widthGrow: 0.3, minWidth: 45 }),
      ],
    });
  }

  window.NSOGridLACP = { mount: mount };
})();
