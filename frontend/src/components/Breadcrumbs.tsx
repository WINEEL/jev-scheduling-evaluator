/**
 * Where you are, and how to get back. Two levels at most in this release.
 *
 * Rendered as an ordered list inside a labelled `nav`, so a screen reader
 * announces it as navigation rather than as stray links.
 */

import Link from "next/link";

export interface Crumb {
  readonly label: string;
  /** Omitted for the current page, which is not a link. */
  readonly href?: string;
}

export function Breadcrumbs({ trail }: { trail: readonly Crumb[] }) {
  return (
    <nav className="breadcrumbs" aria-label="Breadcrumb">
      <ol>
        {trail.map((crumb, index) => {
          const isCurrent = index === trail.length - 1;
          return (
            <li key={`${crumb.label}-${index}`}>
              {crumb.href !== undefined && !isCurrent ? (
                <Link href={crumb.href}>{crumb.label}</Link>
              ) : (
                <span aria-current={isCurrent ? "page" : undefined}>{crumb.label}</span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
