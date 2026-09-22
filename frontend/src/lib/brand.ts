/**
 * The name this pilot runs under, in one place.
 *
 * The application itself is generic -- it names no ministry, no role and no
 * person anywhere in its code. The *church* it is being piloted for is a
 * different kind of fact: it belongs on the header of every screen, and it
 * changes for exactly one reason (a different church), so it lives here as
 * two constants rather than being typed into a layout.
 *
 * There is **no logo asset in this repository**. The header is therefore set
 * as type, in the pilot palette; if a logo file is added later, the header is
 * the only place that has to learn about it.
 */

export const BRAND_CHURCH_NAME = "Community Scheduling Demo";

/** What this product is, as distinct from whose it is. */
export const BRAND_APP_NAME = "Volunteer Scheduling";

/** Browser tab and bookmark title. */
export const BRAND_DOCUMENT_TITLE = `${BRAND_APP_NAME} · ${BRAND_CHURCH_NAME}`;
