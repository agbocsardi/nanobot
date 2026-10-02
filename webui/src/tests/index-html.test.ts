import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("index.html", () => {
  it("keeps browser zoom available", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const viewport = html.match(/<meta\s+name="viewport"\s+content="([^"]+)"/i)?.[1];

    expect(viewport).toContain("width=device-width");
    expect(viewport).not.toContain("user-scalable=no");
    expect(viewport).not.toMatch(/maximum-scale\s*=\s*1(?:\.0)?(?:,|$)/);
  });

  it("extends the layout under the status bar so the top edge can be painted", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const viewport = html.match(/<meta\s+name="viewport"\s+content="([^"]+)"/i)?.[1];

    // iOS 26+ renders installed PWAs fullscreen regardless of viewport-fit, so the
    // app shell pads itself back down with env(safe-area-inset-top) instead.
    expect(viewport).toContain("viewport-fit=cover");
    expect(viewport).not.toContain("viewport-fit=auto");
  });

  it("paints a solid status bar backdrop for installed iOS PWAs", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const document = new DOMParser().parseFromString(html, "text/html");
    const backdrop = document.querySelector(".pwa-status-bar-backdrop");

    expect(backdrop).not.toBeNull();
    expect(backdrop?.getAttribute("aria-hidden")).toBe("true");

    // iOS 27 blurs the top band of an installed PWA unless a fixed, opaque element
    // sits within 4px of the top edge and is at least 6px tall.
    const backdropRules = html.match(/\.pwa-status-bar-backdrop\s*{[^}]*}/gs) ?? [];
    expect(
      backdropRules.some(
        (rule) =>
          rule.includes("position: fixed") &&
          rule.includes("top: 0") &&
          rule.includes("height: max(6px, env(safe-area-inset-top))"),
      ),
    ).toBe(true);
    expect(html).toContain("@media (display-mode: standalone)");
    expect(html).toContain("html.dark .pwa-status-bar-backdrop");
  });

  it("provides light and dark PWA chrome colors", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const manifest = JSON.parse(
      readFileSync(resolve(process.cwd(), "public/manifest.json"), "utf8"),
    ) as {
      background_color?: string;
      theme_color?: string;
      color_scheme_dark?: { background_color?: string; theme_color?: string };
    };
    const document = new DOMParser().parseFromString(html, "text/html");
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    const lightBodyBackground = html.match(/body\s*{[^}]*background:\s*([^;]+);/s)?.[1]?.trim();
    const darkBodyBackground = html.match(
      /html\.dark body\s*{[^}]*background:\s*([^;]+);/s,
    )?.[1]?.trim();

    expect(themeColor?.content).toBe("#ffffff");
    expect(themeColor?.dataset.themeColorLight).toBe("#ffffff");
    expect(themeColor?.dataset.themeColorDark).toBe("#303030");
    expect(lightBodyBackground).toBe("#ffffff");
    expect(darkBodyBackground).toBe("#303030");
    expect(manifest.background_color).toBe("#ffffff");
    expect(manifest.theme_color).toBe("#ffffff");
    expect(manifest.color_scheme_dark?.background_color).toBe("#303030");
    expect(manifest.color_scheme_dark?.theme_color).toBe("#303030");
  });
});
