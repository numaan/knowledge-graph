import type { NextConfig } from "next";

// Standalone output for the container image; no runtime CDNs, so the CSP allows only 'self'.
const csp = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'", // theme bootstrap script and Next.js inline runtime
  "style-src 'self' 'unsafe-inline'", // Shiki and Cytoscape set inline styles
  "img-src 'self' data:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
].join("; ");

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default config;
