/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The first-party static assets are plain browser scripts (IIFEs assigning onto
 * window), so tests run under jsdom and load them for their side effects.
 * vendor/** (Tabulator, diff2html) is not ours and is not under test. */
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["netbox_nso_plugin/tests/js/**/*.test.js"],
  },
});
